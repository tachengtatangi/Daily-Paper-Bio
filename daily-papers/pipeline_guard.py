#!/usr/bin/env python3
"""Guardrails for the daily-papers multi-step pipeline.

These checks make stale temp files impossible to use accidentally after a
network failure. They are intentionally small and dependency-free so fetch,
enrich, and review scripts can share the same success criteria.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PipelineGuardError(RuntimeError):
    """Raised when pipeline state is not fresh enough to continue."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PipelineGuardError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineGuardError(f"invalid JSON in {path}: {exc}") from exc


def require_fetch_success(
    *,
    status_path: Path,
    expected_date: str | None = None,
    expected_days: int | None = None,
) -> dict[str, Any]:
    status = load_json(status_path)
    if not isinstance(status, dict):
        raise PipelineGuardError(f"fetch status must be an object: {status_path}")
    if status.get("status") != "success":
        detail = status.get("error") or "fetch did not report success"
        raise PipelineGuardError(f"fetch status is not success: {detail}")
    if expected_date and status.get("window_end") != expected_date and status.get("target_date") != expected_date:
        raise PipelineGuardError(
            f"fetch status date mismatch: expected {expected_date}, "
            f"got target={status.get('target_date')} window_end={status.get('window_end')}"
        )
    if expected_days is not None and int(status.get("days", -1)) != int(expected_days):
        raise PipelineGuardError(
            f"fetch status days mismatch: expected {expected_days}, got {status.get('days')}"
        )
    if int(status.get("source_fetch_error_count") or 0) > 0:
        raise PipelineGuardError("fetch status contains source fetch errors")
    return status


def require_json_list(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    if not isinstance(data, list):
        raise PipelineGuardError(f"expected JSON array: {path}")
    return data


def require_fresh_file(path: Path, *, reference_path: Path, max_skew_seconds: int = 300) -> None:
    try:
        path_mtime = path.stat().st_mtime
        reference_mtime = reference_path.stat().st_mtime
    except FileNotFoundError as exc:
        raise PipelineGuardError(f"missing file for freshness check: {exc.filename}") from exc
    if path_mtime + max_skew_seconds < reference_mtime:
        raise PipelineGuardError(
            f"stale file: {path} is older than {reference_path} by more than {max_skew_seconds}s"
        )


def require_top30_ready(
    *,
    top30_path: Path,
    status_path: Path,
    expected_date: str | None = None,
    expected_days: int | None = None,
) -> list[dict[str, Any]]:
    status = require_fetch_success(
        status_path=status_path,
        expected_date=expected_date,
        expected_days=expected_days,
    )
    papers = require_json_list(top30_path)
    expected_count = int(status.get("top_count") or 0)
    if len(papers) != expected_count:
        raise PipelineGuardError(
            f"top30 count mismatch: status top_count={expected_count}, file count={len(papers)}"
        )
    require_fresh_file(top30_path, reference_path=status_path)
    return papers


def require_enriched_ready(
    *,
    enriched_path: Path,
    top30_path: Path,
    status_path: Path,
    expected_date: str | None = None,
    expected_days: int | None = None,
) -> list[dict[str, Any]]:
    top = require_top30_ready(
        top30_path=top30_path,
        status_path=status_path,
        expected_date=expected_date,
        expected_days=expected_days,
    )
    enriched = require_json_list(enriched_path)
    if len(enriched) != len(top):
        raise PipelineGuardError(
            f"enriched count mismatch: top30 count={len(top)}, enriched count={len(enriched)}"
        )
    require_fresh_file(enriched_path, reference_path=status_path)
    require_fresh_file(enriched_path, reference_path=top30_path)
    return enriched
