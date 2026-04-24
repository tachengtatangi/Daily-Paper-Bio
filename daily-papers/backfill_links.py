#!/usr/bin/env python3
"""Backfill generated note links into the daily recommendation file.

After paper-reader generates notes, this script:
1. Scans {NOTES_PATH}/ for all .md files
2. Matches each recommendation entry to its note by paper ID / title
3. Inserts/updates  - 📒 **笔记**: [[NoteFileName]]  lines

Usage:
    python backfill_links.py YYYY-MM-DD
    python backfill_links.py  # defaults to today
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from user_config import daily_papers_dir, paper_notes_dir

REPORT_SUFFIX = "论文推荐.md"
NOTE_LINK_LABEL = "笔记"
NOTE_LINK_RE = re.compile(r"^\s*-\s*📒\s*\*\*笔记\*\*", re.IGNORECASE)
ID_LINE_RE       = re.compile(r"^\s*-\s*ID:\s*`([^`]+)`")
LINKS_LINE_RE    = re.compile(r"PubMed\]\(https://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/\)")
DOI_LINE_RE      = re.compile(r"(?:doi\.org|DOI)[:/]\s*(10\.\d{4,}/\S+)", re.IGNORECASE)
BIORXIV_DOI_RE   = re.compile(r"10\.\d{4,}/(\d{4}\.\d{2}\.\d{2}\.\d+)")


def _build_note_index(notes_root: Path) -> dict[str, str]:
    """Return mapping: key → stem (filename without .md).

    Keys indexed per note file:
      - stem.lower()                         full filename (case-insensitive)
      - PMID   (≥7 consecutive digits)       PubMed papers
      - bioRxiv DOI suffix  10.1101/XXXXXXX  e.g. "2024.03.15.585432"
      - Short numeric suffix  YYYYMMDD…      from bioRxiv-style dates in name
    """
    if not notes_root.exists():
        return {}
    index: dict[str, str] = {}
    for path in notes_root.rglob("*.md"):
        stem = path.stem
        index[stem.lower()] = stem

        # PMID: 7+ consecutive digits
        nums = re.findall(r"\d{7,}", stem)
        for n in nums:
            index[n] = stem

        # bioRxiv DOI suffix pattern: YYYY.MM.DD.NNNNNN  (dots-separated date+id)
        biorxiv_m = re.search(r"(\d{4}\.\d{2}\.\d{2}\.\d+)", stem)
        if biorxiv_m:
            suffix = biorxiv_m.group(1)
            index[suffix] = stem                       # "2024.03.15.585432"
            index[f"10.1101/{suffix}"] = stem          # full DOI form
            index[suffix.replace(".", "")] = stem      # digits-only fallback

    return index


def _section_blocks(content: str) -> list[tuple[int, int, str]]:
    """Return (start, end, header) for each ### N. block in content."""
    lines = content.split("\n")
    blocks: list[tuple[int, int, str]] = []
    pat = re.compile(r"^###\s+\d+\.")
    starts = [i for i, line in enumerate(lines) if pat.match(line)]
    for j, start in enumerate(starts):
        end = starts[j + 1] - 1 if j + 1 < len(starts) else len(lines) - 1
        blocks.append((start, end, lines[start]))
    return blocks


def backfill(report_date: str) -> int:
    """Backfill note links into the recommendation file for report_date.

    Returns number of links inserted.
    """
    daily_dir  = daily_papers_dir()
    report_path = daily_dir / f"{report_date}-{REPORT_SUFFIX}"
    if not report_path.exists():
        print(f"[backfill] Report not found: {report_path}", file=sys.stderr)
        return 0

    notes_root = paper_notes_dir()
    note_index = _build_note_index(notes_root)
    if not note_index:
        print("[backfill] No notes found, nothing to backfill.", file=sys.stderr)
        return 0

    content = report_path.read_text(encoding="utf-8-sig")
    lines   = content.split("\n")
    inserted = 0

    blocks = _section_blocks(content)
    for start, end, header in blocks:
        # Check if note link already present in this block
        block_lines = lines[start : end + 1]
        if any(NOTE_LINK_RE.match(ln) for ln in block_lines):
            continue  # already has note link

        # Extract paper ID from  - ID: `xxx`  line
        paper_id = ""
        doi_id = ""
        for ln in block_lines:
            m = ID_LINE_RE.match(ln)
            if m:
                paper_id = m.group(1).strip()
                break
        # Also try extracting PMID from PubMed link
        if not paper_id:
            for ln in block_lines:
                m = LINKS_LINE_RE.search(ln)
                if m:
                    paper_id = m.group(1)
                    break
        # Try extracting DOI (covers bioRxiv and Elsevier papers)
        for ln in block_lines:
            m = DOI_LINE_RE.search(ln)
            if m:
                doi_id = m.group(1).rstrip(")")
                break

        if not paper_id and not doi_id:
            continue

        # Try to find a matching note — PMID first, then DOI variants
        note_stem = (
            note_index.get(paper_id.lower())
            or note_index.get(paper_id)
            or note_index.get(re.sub(r"\D", "", paper_id))  # numeric only
        )
        if not note_stem and doi_id:
            # bioRxiv suffix match: "2024.03.15.585432"
            bm = BIORXIV_DOI_RE.search(doi_id)
            if bm:
                suffix = bm.group(1)
                note_stem = (
                    note_index.get(suffix)
                    or note_index.get(f"10.1101/{suffix}")
                    or note_index.get(suffix.replace(".", ""))
                )
            if not note_stem:
                # Generic DOI key lookup
                note_stem = note_index.get(doi_id.lower())
        if not note_stem:
            continue

        # Insert after the  - 链接:  line, or after  - ID:  line
        insert_after = -1
        for rel_idx, ln in enumerate(block_lines):
            if re.match(r"^\s*-\s*\*?\*?链接\*?\*?:", ln):
                insert_after = start + rel_idx
                break
        if insert_after < 0:
            for rel_idx, ln in enumerate(block_lines):
                if ID_LINE_RE.match(ln):
                    insert_after = start + rel_idx
                    break
        if insert_after < 0:
            insert_after = start  # fallback: insert right after header

        new_line = f"- 📒 **{NOTE_LINK_LABEL}**: [[{note_stem}]]"
        lines.insert(insert_after + 1, new_line)
        # Update all subsequent block offsets
        for k in range(len(blocks)):
            bs, be, bh = blocks[k]
            if bs > insert_after:
                blocks[k] = (bs + 1, be + 1, bh)
        inserted += 1
        print(f"  [backfill] {paper_id} → [[{note_stem}]]")

    if inserted > 0:
        report_path.write_text("\n".join(lines), encoding="utf-8-sig")
        print(f"[backfill] Inserted {inserted} note links into {report_path.name}")
    else:
        print("[backfill] No new links to insert.")
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill note links into the daily recommendation file."
    )
    parser.add_argument(
        "--date", default=date.today().isoformat(),
        help="Report date (YYYY-MM-DD), default: today"
    )
    args = parser.parse_args()
    n = backfill(args.date)
    print(json.dumps({"date": args.date, "inserted": n}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
