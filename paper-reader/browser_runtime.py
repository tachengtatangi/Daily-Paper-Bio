"""Browser runtime helpers for publisher PDF/Figure fetching."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from pathlib import Path

_CHROME_CANDIDATES = [
    str(Path(os.environ.get("ProgramFiles",      r"C:\Program Files"))
        / "Google" / "Chrome" / "Application" / "chrome.exe"),
    str(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google" / "Chrome" / "Application" / "chrome.exe"),
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome"
        / "Application" / "chrome.exe"),
]



def _find_chrome() -> str:
    for p in _CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                k = winreg.OpenKey(root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                p, _ = winreg.QueryValueEx(k, "")
                if os.path.isfile(p):
                    return p
            except FileNotFoundError:
                pass
    except ImportError:
        pass
    return os.environ.get("CHROME_EXE", "")

def _find_free_port(start: int = 9240) -> int:
    for port in range(start, start + 30):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("No free CDP port found")

def _existing_cdp_url() -> str:
    """Return base URL of an already-running CDP-enabled Chrome, or empty string.

    Checks PAPER_READER_CDP_URL / CHROME_CDP_URL / CDP_URL env vars first,
    then probes common localhost ports (9222, 9223, 9240).  Returns the first
    responsive base URL, e.g. 'http://127.0.0.1:9222'.
    """
    configured = (
        os.environ.get("PAPER_READER_CDP_URL")
        or os.environ.get("CHROME_CDP_URL")
        or os.environ.get("CDP_URL")
        or ""
    ).strip().rstrip("/")
    candidates = [configured] if configured else []
    candidates.extend(f"http://127.0.0.1:{port}" for port in (9222, 9223, 9240))
    import urllib.request as _ur
    for base in candidates:
        if not base:
            continue
        try:
            with _ur.urlopen(f"{base}/json/version", timeout=1) as resp:
                if resp.status == 200:
                    return base
        except Exception:
            continue
    return ""

def _chrome_process_running() -> bool:
    """Return True if any Chrome process is currently running.

    Chrome on Windows does NOT reliably create a 'SingletonLock' file in the
    user data directory (that mechanism is Linux/Mac-only).  On Windows, Chrome
    uses a named pipe/mutex for singleton detection, so os.path.exists('SingletonLock')
    always returns False even when Chrome is open.  This function uses the
    tasklist command as the authoritative check.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=5,
        )
        return b"chrome.exe" in (result.stdout or b"")
    except Exception:
        # Fall back to SingletonLock as last resort if tasklist fails
        return False

def _copy_chrome_cookies(src_profile_dir: str, dst_profile_dir: str) -> bool:
    """Copy Chrome cookies from a locked real profile to a temp profile dir.

    Chrome stores cookies as a DPAPI-encrypted SQLite database.  The AES key
    is in 'Local State' (itself DPAPI-protected, user-bound).  Copying both
    files lets a second Chrome process running as the *same Windows user*
    decrypt and use all institutional session cookies — no re-login needed.

    Returns True if at least one Cookies file was copied successfully.
    """
    import shutil
    copied = False
    try:
        # 1. Copy Local State (holds the encrypted AES cookie key)
        src_ls = os.path.join(src_profile_dir, "Local State")
        dst_ls = os.path.join(dst_profile_dir, "Local State")
        if os.path.isfile(src_ls):
            shutil.copy2(src_ls, dst_ls)

        # 2. Copy cookie database for the Default profile.
        #    Chrome 96+ stores it in Default/Network/Cookies; older Chrome uses
        #    Default/Cookies.  Try Network first, then fall back.
        dst_default = os.path.join(dst_profile_dir, "Default")
        os.makedirs(dst_default, exist_ok=True)

        cookie_candidates = [
            # (src_path, dst_path)
            (
                os.path.join(src_profile_dir, "Default", "Network", "Cookies"),
                os.path.join(dst_default, "Network", "Cookies"),
            ),
            (
                os.path.join(src_profile_dir, "Default", "Cookies"),
                os.path.join(dst_default, "Cookies"),
            ),
        ]
        for src_db, dst_db in cookie_candidates:
            if not os.path.isfile(src_db):
                continue
            os.makedirs(os.path.dirname(dst_db), exist_ok=True)
            # Copy SQLite main file + WAL / SHM journal files
            for suffix in ("", "-wal", "-shm"):
                src_f, dst_f = src_db + suffix, dst_db + suffix
                if os.path.isfile(src_f):
                    try:
                        shutil.copy2(src_f, dst_f)
                        copied = True
                    except Exception:
                        pass
            if copied:
                break   # found and copied; no need to try older path

    except Exception:
        pass
    return copied

async def _wait_for_cdp(port: int, max_wait: int = 25) -> bool:
    import urllib.request
    for _ in range(max_wait):
        try:
            urllib.request.urlopen(
                f"http://localhost:{port}/json/version", timeout=1)
            return True
        except Exception:
            await asyncio.sleep(1)
    return False
