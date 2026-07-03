"""Runtime cleanup helpers for daily paper and paper-reader workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CleanupSummary:
    root: str
    applied: bool
    keep_markdown: bool
    removed_files: int = 0
    removed_bytes: int = 0
    pruned_dirs: int = 0
    kept_markdown_files: int = 0
    errors: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _normalise_roots(roots: list[Path | str | None] | None) -> list[Path]:
    out: list[Path] = []
    for root in roots or []:
        if not root:
            continue
        try:
            out.append(Path(root).expanduser().resolve())
        except Exception:
            continue
    return out


def cleanup_mineru_non_markdown(
    target_root: Path | str | None,
    *,
    keep_markdown: bool = True,
    apply: bool = False,
    allowed_roots: list[Path | str | None] | None = None,
    max_listed_paths: int = 40,
) -> CleanupSummary:
    """Delete MinerU cache artifacts while optionally preserving Markdown.

    The caller must pass an explicit target directory. Deletion is refused unless
    the target is under one of the allowed roots. This prevents a bad config or a
    malformed record from turning cleanup into a broad filesystem operation.
    """
    root_text = str(target_root or "").strip()
    summary = CleanupSummary(root=root_text, applied=apply, keep_markdown=keep_markdown)
    if not root_text:
        return summary

    root = Path(root_text).expanduser()
    try:
        root_resolved = root.resolve()
    except Exception as exc:
        summary.errors.append(f"resolve failed: {exc}")
        return summary
    summary.root = str(root_resolved)

    if not root_resolved.exists():
        return summary
    if not root_resolved.is_dir():
        summary.errors.append("target is not a directory")
        return summary

    safe_roots = _normalise_roots(allowed_roots) or [root_resolved]
    if not any(_is_relative_to(root_resolved, safe_root) for safe_root in safe_roots):
        summary.errors.append("target is outside allowed cleanup roots")
        return summary

    files = sorted((p for p in root_resolved.rglob("*") if p.is_file()), key=lambda p: str(p).lower())
    for path in files:
        if keep_markdown and path.suffix.lower() == ".md":
            summary.kept_markdown_files += 1
            continue
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
        summary.removed_files += 1
        summary.removed_bytes += size
        if len(summary.removed_paths) < max_listed_paths:
            summary.removed_paths.append(str(path))
        if apply:
            try:
                path.unlink()
            except Exception as exc:
                summary.errors.append(f"failed to delete {path}: {exc}")

    if apply:
        dirs = sorted((p for p in root_resolved.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
        for directory in dirs:
            try:
                directory.rmdir()
                summary.pruned_dirs += 1
            except OSError:
                pass
            except Exception as exc:
                summary.errors.append(f"failed to prune {directory}: {exc}")

    return summary


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_cleanup_summary(summary: CleanupSummary) -> str:
    action = "removed" if summary.applied else "would remove"
    kept = f", kept {summary.kept_markdown_files} Markdown file(s)" if summary.keep_markdown else ""
    pruned = f", pruned {summary.pruned_dirs} empty dir(s)" if summary.applied and summary.pruned_dirs else ""
    errors = f", errors={len(summary.errors)}" if summary.errors else ""
    return (
        f"MinerU cleanup {action} {summary.removed_files} non-kept file(s) "
        f"({format_bytes(summary.removed_bytes)}) under {summary.root}{kept}{pruned}{errors}"
    )
