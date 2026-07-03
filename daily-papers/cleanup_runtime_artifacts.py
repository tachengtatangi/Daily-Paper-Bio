#!/usr/bin/env python3
"""Clean DailyPapers runtime artifacts after notes finish.

Default behavior is preview-only. Use --apply in automation after must-read notes,
backfill, and MOC refresh have completed. MinerU Markdown is preserved by default;
only PDFs, JSON, cropped JPGs, and other non-Markdown cache files are removed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_DIR = SCRIPT_DIR.parent / "_shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from runtime_cleanup import cleanup_mineru_non_markdown, format_cleanup_summary
from user_config import pdf_picture_root_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean daily-papers runtime artifacts.")
    parser.add_argument("--apply", action="store_true", help="Actually delete files. Omit for preview/dry-run.")
    parser.add_argument(
        "--delete-mineru-markdown",
        action="store_true",
        help="Also delete MinerU .md files. Default keeps Markdown as text evidence.",
    )
    parser.add_argument(
        "--mineru-root",
        default="",
        help="Override MinerU cache root. Defaults to <pdf_figure_folder>/mineru.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    args = parser.parse_args()

    mineru_root = Path(args.mineru_root).expanduser() if args.mineru_root else pdf_picture_root_dir() / "mineru"
    summary = cleanup_mineru_non_markdown(
        mineru_root,
        keep_markdown=not args.delete_mineru_markdown,
        apply=args.apply,
        allowed_roots=[mineru_root],
        max_listed_paths=80,
    )

    if args.json:
        print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    else:
        print(format_cleanup_summary(summary))
        if not args.apply:
            print("Preview only. Re-run with --apply to delete non-kept MinerU cache files.")
        if summary.errors:
            print("Errors:", file=sys.stderr)
            for error in summary.errors:
                print(f"- {error}", file=sys.stderr)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
