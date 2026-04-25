#!/usr/bin/env python3
"""Deprecated wrapper for the canonical daily-papers/update_history.py entry."""

from __future__ import annotations

import runpy
from pathlib import Path


CANONICAL = Path(__file__).resolve().parent.parent / "daily-papers" / "update_history.py"


if __name__ == "__main__":
    runpy.run_path(str(CANONICAL), run_name="__main__")
