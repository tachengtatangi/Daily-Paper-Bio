#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))


def is_backup_note(rel: Path) -> bool:
    return any(".bak-" in part or part.endswith(".bak") for part in rel.parts)


from user_config import paper_notes_dir


def main() -> int:
    notes_root = paper_notes_dir()
    notes_root.mkdir(parents=True, exist_ok=True)
    out_path = notes_root / "PaperNotes.md"

    notes = []
    if notes_root.exists():
        for path in sorted(notes_root.rglob("*.md")):
            rel = path.relative_to(notes_root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if is_backup_note(rel):
                continue
            if rel.name == "PaperNotes.md":
                continue
            if rel.parts and rel.parts[0] == "_concepts":
                continue
            notes.append(rel)

    lines = [
        "---",
        "tags: [MOC, auto-generated]",
        "generated_by: dailypaper-skills",
        "---",
        "",
        "# 论文索引",
        "",
        f"- 根目录: `{notes_root}`",
        f"- 总笔记数: `{len(notes)}`",
        "",
    ]
    if notes:
        for rel in notes:
            target = (notes_root / rel).relative_to(notes_root.parent).with_suffix("").as_posix()
            lines.append(f"- [[{target}|{rel.stem}]]")
    else:
        lines.append("- 暂无论文笔记")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(out_path), "indexed_notes": len(notes)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
