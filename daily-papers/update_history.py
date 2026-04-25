#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
import sys
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from user_config import daily_papers_dir, history_days_to_keep
from date_window import parse_date


def history_path() -> Path:
    return daily_papers_dir() / ".history.json"


def load_history() -> list[dict]:
    path = history_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(entries: list[dict]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _paper_id(paper: dict) -> str:
    return str(paper.get("id") or paper.get("doi") or paper.get("url") or "").strip()


def update_history(papers: list[dict], today: str) -> int:
    existing = load_history()
    by_id = {str(item.get("id") or "").strip(): item for item in existing if str(item.get("id") or "").strip()}
    added = 0
    for paper in papers:
        pid = _paper_id(paper)
        if not pid:
            continue
        if pid not in by_id:
            by_id[pid] = {
                "id": pid,
                "date": today,
                "title": paper.get("title", ""),
                "source": paper.get("source", ""),
            }
            added += 1
        else:
            old_date = str(by_id[pid].get("date") or "")
            if old_date and old_date > today:
                by_id[pid]["date"] = today

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=history_days_to_keep())).strftime("%Y-%m-%d")
    pruned = [item for item in by_id.values() if str(item.get("date") or "") >= cutoff]
    pruned.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("title") or ""), str(x.get("id") or "")))
    save_history(pruned)
    return added


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    report_date = parse_date(args.date).isoformat()
    papers = json.loads(args.input_json.read_text(encoding="utf-8-sig"))
    added = update_history(papers, report_date)
    print(json.dumps({"history_path": str(history_path()), "date": report_date, "added": added}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
