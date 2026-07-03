#!/usr/bin/env python3
"""Run the DailyPapers pipeline up to draft generation.

This script is a staging/debug entrypoint. It fetches, enriches, and writes
temporary artifacts, but it does not publish the final DailyPapers review into
the vault. The final Markdown should only be written after the review agent
completes the commentary pass.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_DIR = SCRIPT_DIR.parent / "_shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from user_config import (
    auto_refresh_indexes_enabled,
    concepts_dir,
    daily_papers_dir,
    ensure_vault_dirs,
    git_commit_enabled,
    git_push_enabled,
    notes_parallelism,
    obsidian_vault_path,
    paper_notes_dir,
    pdf_picture_root_dir,
    set_daily_papers_profile_update_flag,
    temp_file_path,
)
from date_window import parse_window

# Ensure all vault output directories exist before any pipeline stage runs.
ensure_vault_dirs()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


FETCH = load_module("daily_fetch", SCRIPT_DIR / "fetch_and_score.py")
ENRICH = load_module("daily_enrich", SCRIPT_DIR / "enrich_papers.py")
REVIEW = load_module("daily_review", SCRIPT_DIR / "build_review.py")
HISTORY = load_module("daily_history", SCRIPT_DIR / "update_history.py")
GEN_PAPER_MOC = load_module("generate_paper_mocs", SHARED_DIR / "generate_paper_mocs.py")
GEN_CONCEPT_MOC = load_module("generate_concept_mocs", SHARED_DIR / "generate_concept_mocs.py")
PAPER_READER_SCRIPT = SCRIPT_DIR.parent / "paper-reader" / "run_reader.py"
PIPELINE_CONFIG = FETCH._CONFIG


def parse_keywords_override(raw: str) -> list[str]:
    if not raw:
        return []
    return FETCH.parse_keywords_override(raw)


def _write_non_oa_placeholder_note(paper: dict) -> str | None:
    """Write a minimal placeholder note for non-OA papers that lack PMC full text.

    Instead of running run_reader.py (which would yield an abstract-only note
    with weak analysis), we write a concise note that honestly states the paper
    is behind a paywall and links to the correct identifiers.
    """
    notes_dir = paper_notes_dir()
    if not notes_dir:
        return None
    inbox = Path(notes_dir) / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    pmid = str(paper.get("id") or paper.get("pmid") or "").strip()
    title = str(paper.get("title") or "Untitled").strip()
    doi = str(paper.get("doi") or "").strip()
    journal = str(paper.get("journal") or paper.get("source") or "").strip()
    year = str(paper.get("year") or date.today().year)
    authors_raw = paper.get("authors") or []
    authors = authors_raw if isinstance(authors_raw, list) else [str(authors_raw)]
    authors_yaml = json.dumps(authors, ensure_ascii=False)
    pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    doi_url = f"https://doi.org/{doi}" if doi else ""
    safe_title = title[:90].replace("/", " ").replace("\\", " ").replace(":", " ")
    stem = f"{pmid} - {safe_title}" if pmid else safe_title
    out_path = inbox / f"{stem}.md"

    today_str = date.today().isoformat()
    note = f"""---
title: {json.dumps(title, ensure_ascii=False)}
authors: {authors_yaml}
year: {year}
tags: [paper-note, paper-reader, standard, pubmed, non-oa]
journal: {json.dumps(journal, ensure_ascii=False)}
pmid: "{pmid}"
doi: "{doi}"
pubmed_url: "{pubmed_url}"
doi_url: "{doi_url}"
web_url: ""
local_pdf: ""
pdf_path: ""
downloaded_pdf: ""
acquisition_path: "PubMed metadata only — non-OA, full text unavailable"
summary_mode: "仅 PubMed 元数据（非 OA 文章，无全文访问权限）"
analysis_mode: "standard"
date: "{today_str}"
---

# {title}

## Metadata

| Key | Value |
| --- | --- |
| Authors | {', '.join(authors)} |
| Journal | {journal} |
| Year | {year} |
| PMID | {pmid} |
| DOI | {doi} |
| PubMed | [{pubmed_url}]({pubmed_url}) |
| DOI page | [{doi_url}]({doi_url}) |

## Sources

- PubMed: [{pubmed_url}]({pubmed_url})
- DOI: [{doi_url}]({doi_url})

## 访问说明

> 此文章发表于订阅制期刊，暂无 PMC 开放全文。无法自动生成分析笔记。
>
> 建议通过机构网络、VPN 或图书馆访问获取全文，然后手动补充以下各分析区块。
"""
    try:
        out_path.write_text(note, encoding="utf-8")
        return str(out_path)
    except Exception:
        return None


def run_paper_reader_for_pubmed(paper: dict) -> str | None:
    # Non-OA shortcut: if the paper has no PMC ID and is not a preprint or
    # Elsevier paper (both have full-text API access), it is likely behind a
    # paywall.  Write a minimal placeholder rather than a weak abstract-only note.
    pmc_id = str(paper.get("pmc_id") or "").strip()
    doi = str(paper.get("doi") or "").strip()
    is_preprint = (
        doi.startswith("10.1101/") or doi.startswith("10.64898/")
        or "biorxiv" in str(paper.get("url") or "").lower()
        or "medrxiv" in str(paper.get("url") or "").lower()
    )
    is_elsevier = doi.startswith("10.1016/")  # Elsevier API can retrieve full text
    if not pmc_id and not is_preprint and not is_elsevier:
        return _write_non_oa_placeholder_note(paper)

    source = paper.get("url") or (f"https://doi.org/{paper.get('doi', '')}" if paper.get("doi") else "")
    if not source:
        return None
    today = date.today().isoformat()
    pdf_dir = pdf_picture_root_dir()
    cmd = [
        sys.executable,
        str(PAPER_READER_SCRIPT),
        source,
        "--mode",
        "standard",
        "--date",
        today,
        "--prefer-visible-browser",
        "--pdf-dir",
        str(pdf_dir),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if result.returncode != 0:
        return None
    decoder = json.JSONDecoder()
    raw = result.stdout or ""
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{") or "note_path" not in line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("note_path"):
            return payload["note_path"]
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("note_path"):
            return payload["note_path"]
    return None


def _infer_publisher_key(paper: dict) -> str:
    """Return a stable key identifying which patchright profile this paper will use.

    patchright profiles are keyed by publisher domain.  Papers with the same
    key will contend for the same Chromium profile lock and must run sequentially.

    Priority:
      1. PMC papers (has pmc_id) → fixed key "pmc.ncbi.nlm.nih.gov"
      2. DOI present → DOI registrant prefix, e.g. "10.1093" (identifies publisher)
      3. Fallback → URL netloc, or "unknown"
    """
    if paper.get("pmc_id"):
        return "pmc.ncbi.nlm.nih.gov"
    doi = (paper.get("doi") or "").strip()
    if doi:
        return doi.split("/")[0]
    url = (paper.get("url") or "").strip()
    if url:
        try:
            netloc = urlparse(url).netloc
            if netloc:
                return netloc
        except Exception:
            pass
    return "unknown"


def generate_notes_publisher_aware(must_reads: list[dict], note_limit: int) -> list[str]:
    """Generate notes with publisher-aware parallelism.

    Papers from the same publisher share one patchright persistent-context
    profile directory and must run sequentially (Chromium locks the profile at
    OS level).  Papers from different publishers use different profiles and can
    run in parallel safely.

    Strategy:
      - Group papers by publisher key (_infer_publisher_key).
      - Within each group: run sequentially (for-loop).
      - Across groups: run in parallel (ThreadPoolExecutor, max_workers from
        notes_parallelism config).
    """
    import concurrent.futures
    from collections import defaultdict

    if note_limit <= 0:
        return []

    limited = must_reads[:note_limit]

    groups: dict[str, list[dict]] = defaultdict(list)
    for paper in limited:
        groups[_infer_publisher_key(paper)].append(paper)

    def _process_group(papers: list[dict]) -> list[str]:
        paths: list[str] = []
        for paper in papers:
            try:
                note_path = run_paper_reader_for_pubmed(paper)
            except Exception:
                note_path = None
            if note_path:
                paths.append(note_path)
        return paths

    if len(groups) == 1:
        return _process_group(limited)

    max_workers = min(len(groups), max(1, notes_parallelism()))
    note_paths: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_process_group, g) for g in groups.values()]
        for fut in concurrent.futures.as_completed(futures):
            try:
                note_paths.extend(fut.result())
            except Exception:
                pass
    return note_paths


def maybe_refresh_indexes() -> None:
    if not auto_refresh_indexes_enabled():
        return
    GEN_PAPER_MOC.main()
    GEN_CONCEPT_MOC.main()


def maybe_git_automation(target_date: str, paths: list[str]) -> None:
    if not git_commit_enabled():
        return
    repo_dir = obsidian_vault_path()
    try:
        probe = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except Exception:
        return
    if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
        return

    existing = [str(Path(path)) for path in paths if path and Path(path).exists()]
    if not existing:
        return
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "--"] + existing,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if status.returncode == 0:
        return
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"daily papers: update {target_date}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if git_push_enabled():
        subprocess.run(
            ["git", "-C", str(repo_dir), "push"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--notes-limit", type=int, default=-1)
    parser.add_argument("--refresh-profile", action="store_true")
    args = parser.parse_args()

    window = parse_window(args.date, args.days)
    target_date = window.end
    days = window.days
    start_date = window.start
    report_date = window.report_date

    override_keywords = parse_keywords_override(args.keywords)
    should_auto_reset_profile_flag = bool(FETCH._CONFIG.get("update_profile_from_pdf_library", False))
    if hasattr(FETCH, "reset_filter_audit"):
        FETCH.reset_filter_audit()
    FETCH.apply_library_profile(refresh=args.refresh_profile or should_auto_reset_profile_flag)
    if override_keywords:
        FETCH.KEYWORDS = override_keywords

    papers: list[dict] = []
    if bool(FETCH._CONFIG.get("pubmed_enabled", True)):
        papers.extend(FETCH.fetch_pubmed_papers(start_date, target_date, days))
    if hasattr(FETCH, "fetch_biorxiv_papers") and bool(FETCH._CONFIG.get("biorxiv_enabled", True)):
        papers.extend(FETCH.fetch_biorxiv_papers(start_date, target_date, days))

    selected = FETCH.merge_and_dedup(papers, target_date, days=days, top_n=FETCH.TOP_N * days)
    if hasattr(FETCH, "raise_if_fetch_failed_without_candidates"):
        try:
            FETCH.raise_if_fetch_failed_without_candidates(selected, stage="run_pipeline")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    enriched = (
        ENRICH.enrich_papers(selected)
        if hasattr(ENRICH, "enrich_papers")
        else [ENRICH.enrich_paper(paper) for paper in selected]
    )

    top_json_path = temp_file_path("daily_papers_top30.json")
    top_json_path.parent.mkdir(parents=True, exist_ok=True)
    top_json_path.write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    filter_audit = FETCH.filter_audit_snapshot() if hasattr(FETCH, "filter_audit_snapshot") else {}
    filter_audit_path = temp_file_path("daily_papers_filter_audit.json")
    filter_audit_path.write_text(json.dumps(filter_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    enriched_json_path = temp_file_path("daily_papers_enriched.json")
    enriched_json_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    window_json_path = temp_file_path("daily_papers_window.json")
    window_json_path.write_text(
        json.dumps(
            {
                "report_date": report_date,
                "window_start": window.start_date,
                "window_end": window.end_date,
                "days": days,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    draft_suffix = getattr(REVIEW, "DRAFT_SUFFIX", "论文推荐.draft.md")
    draft_path = temp_file_path(f"{report_date}-{draft_suffix}")
    draft_path.write_text(REVIEW.build_markdown(enriched, report_date), encoding="utf-8-sig")

    summary = {
        "draft_path": str(draft_path),
        "top_json_path": str(top_json_path),
        "enriched_json_path": str(enriched_json_path),
        "filter_audit_path": str(filter_audit_path),
        "window_json_path": str(window_json_path),
        "report_date": report_date,
        "window_start": window.start_date,
        "window_end": window.end_date,
        "days": days,
        "candidate_count": len(enriched),
        "keywords": override_keywords or FETCH.KEYWORDS,
        "profile_boost_keywords": getattr(FETCH, "PROFILE_BOOST_KEYWORDS", []),
        "preferred_journals": getattr(FETCH, "PROFILE_PREFERRED_JOURNALS", []),
        "next_step": "Use daily-papers-review to finalize the draft before writing the final Markdown or updating history.",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if should_auto_reset_profile_flag:
        set_daily_papers_profile_update_flag(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
