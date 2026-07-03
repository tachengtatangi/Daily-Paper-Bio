#!/usr/bin/env python3
"""Compatibility wrapper for the legacy Zotero batch paper daemon."""

from __future__ import annotations

import os
import runpy
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "_shared"
LEGACY = HERE / "legacy" / "paper_daemon.py"
STATE_DIR = Path(tempfile.gettempdir()) / "paper_daemon_state"

if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

os.environ.setdefault("PAPER_DAEMON_STATE_DIR", str(STATE_DIR))
STATE_DIR.mkdir(parents=True, exist_ok=True)

if __name__ == "__main__":
    runpy.run_path(str(LEGACY), run_name="__main__")