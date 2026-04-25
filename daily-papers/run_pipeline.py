#!/usr/bin/env python3
"""Run the DailyPapers pipeline up to draft generation.

This script is a staging/debug entrypoint. It fetches, enriches, and writes
temporary artifacts, but it does not publish the final DailyPapers review into
the vault. The final Markdown should only be written after the review agent
completes the commentary pass.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


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


def run_paper_reader_for_pubmed(paper: dict) -> str | None:
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


def generate_notes_parallel(must_reads: list[dict], note_limit: int) -> list[str]:
    if note_limit <= 0:
        return []
    parallelism = max(1, int(PIPELINE_CONFIG.get("notes_parallelism", 1)))
    targets = must_reads[:note_limit]
    note_paths: list[str] = []
    failed_papers: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {executor.submit(run_paper_reader_for_pubmed, paper): paper for paper in targets}
        for future in concurrent.futures.as_completed(futures):
            paper = futures[future]
            try:
                note_path = future.result()
            except Exception:
                note_path = None
            if note_path:
                note_paths.append(note_path)
            else:
                failed_papers.append(paper)

    for paper in failed_papers:
        try:
            note_path = run_paper_reader_for_pubmed(paper)
        except Exception:
            note_path = None
        if note_path:
            note_paths.append(note_path)
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
