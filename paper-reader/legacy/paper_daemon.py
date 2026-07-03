#!/usr/bin/env python3
"""Batch paper reader helper for Zotero collections.

This daemon is optional. Direct `paper-reader` usage does not depend on it.
It exists for batch processing a Zotero collection and asking Codex to read
each paper into the user's Obsidian vault.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_DIR = SCRIPT_DIR.parent / "_shared"
if str(SHARED_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SHARED_DIR))

from user_config import (  # noqa: E402
    concepts_dir,
    obsidian_vault_path,
    paper_notes_dir,
    zotero_db_path,
    zotero_storage_dir,
)


ZOTERO_DB = zotero_db_path()
ZOTERO_STORAGE = zotero_storage_dir()
OBSIDIAN_VAULT = obsidian_vault_path()
PAPER_NOTES_ROOT = paper_notes_dir()
CONCEPTS_ROOT = concepts_dir()

STATE_DIR = Path(os.environ.get("PAPER_DAEMON_STATE_DIR", Path.home() / ".codex")).expanduser()
CODEX_BIN = os.environ.get("PAPER_DAEMON_CODEX_BIN", "codex")
CODEX_WORKDIR = os.environ.get("PAPER_DAEMON_CODEX_WORKDIR", str(OBSIDIAN_VAULT))
CODEX_MODEL = os.environ.get("PAPER_DAEMON_CODEX_MODEL", "").strip()
CODEX_EXTRA_ARGS = os.environ.get("PAPER_DAEMON_CODEX_ARGS", "")

PROGRESS_FILE = STATE_DIR / "paper_daemon_progress.json"
LOG_FILE = STATE_DIR / "paper_daemon.log"
PID_FILE = STATE_DIR / "paper_daemon.pid"

INITIAL_WAIT = 60
MAX_WAIT = 21600
WAIT_MULTIPLIER = 2
BETWEEN_PAPERS_WAIT = 5
QUOTA_WAIT_TIME = 1800

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")
GREEK_REPLACEMENTS = {
    "π": "pi",
    "ϕ": "phi",
    "φ": "phi",
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
}


def acquire_lock() -> bool:
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
            return False
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def wait_for_quota_reset(wait_seconds: Optional[int] = None) -> None:
    seconds = wait_seconds or QUOTA_WAIT_TIME
    logger.info("Quota limited, waiting %s minutes", max(1, seconds // 60))
    time.sleep(seconds)


def detect_limit_error(output: str) -> Optional[str]:
    text = output.lower()
    if "rate limit" in text or "too many requests" in text:
        return "RATE_LIMIT"
    if "hit your limit" in text or "usage limit" in text or "resets" in text:
        return "QUOTA_LIMIT"
    return None


def parse_reset_wait_seconds(message: str) -> Optional[int]:
    match = re.search(
        r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
        message,
        re.IGNORECASE,
    )
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").lower()
    tz_name = match.group(4) or "Asia/Shanghai"

    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None

    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def copy_zotero_db() -> Path:
    if not str(ZOTERO_DB) or not ZOTERO_DB.exists():
        raise FileNotFoundError(f"Zotero database not found: {ZOTERO_DB}")
    tmp_db = Path(tempfile.gettempdir()) / "zotero_readonly.sqlite"
    shutil.copy2(ZOTERO_DB, tmp_db)
    return tmp_db


def get_collection_id_and_path(db_path: Path, collection_name: str) -> tuple[Optional[int], Optional[str]]:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
    collections = {row[0]: {"name": row[1], "parent": row[2]} for row in cursor.fetchall()}
    conn.close()

    def build_path(collection_id: int) -> str:
        parts = []
        current = collection_id
        while current:
            info = collections.get(current)
            if not info:
                break
            parts.insert(0, info["name"])
            current = info["parent"]
        return "/".join(parts)

    wanted = collection_name.lower()
    for collection_id, info in collections.items():
        name = info["name"].lower()
        if name == wanted or wanted in name:
            return collection_id, build_path(collection_id)

    return None, None


def get_all_child_collections(db_path: Path, collection_id: int) -> list[int]:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT collectionID, parentCollectionID FROM collections")
    rows = cursor.fetchall()
    conn.close()

    children_map: dict[Optional[int], list[int]] = {}
    for cid, parent_id in rows:
        children_map.setdefault(parent_id, []).append(cid)

    result = [collection_id]

    def walk(cid: int) -> None:
        for child_id in children_map.get(cid, []):
            result.append(child_id)
            walk(child_id)

    walk(collection_id)
    return result


def get_papers_in_collection(db_path: Path, collection_id: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    collection_ids = get_all_child_collections(db_path, collection_id)
    placeholders = ",".join("?" * len(collection_ids))
    query = f"""
        SELECT DISTINCT i.itemID, idv.value AS title
        FROM items i
        JOIN collectionItems ci ON i.itemID = ci.itemID
        JOIN itemData id ON i.itemID = id.itemID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        JOIN fields f ON id.fieldID = f.fieldID
        WHERE ci.collectionID IN ({placeholders})
          AND f.fieldName = 'title'
          AND i.itemTypeID != 14
    """
    cursor.execute(query, collection_ids)
    rows = cursor.fetchall()
    conn.close()

    logger.info("Recursive collection query across %s collections", len(collection_ids))
    return [{"item_id": row[0], "title": row[1]} for row in rows]


def get_pdf_path(db_path: Path, item_id: int) -> Optional[Path]:
    if not str(ZOTERO_STORAGE):
        return None

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ia.path, items.key
        FROM itemAttachments ia
        JOIN items ON ia.itemID = items.itemID
        WHERE ia.parentItemID = ? AND ia.contentType = 'application/pdf'
        """,
        (item_id,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    path_value, key = row
    if path_value and path_value.startswith("storage:"):
        filename = path_value.replace("storage:", "", 1)
        candidate = ZOTERO_STORAGE / key / filename
        if candidate.exists():
            return candidate
    return None


def get_paper_online_source(db_path: Path, item_id: int) -> Optional[dict]:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT f.fieldName, idv.value
        FROM itemData id
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        WHERE id.itemID = ?
        """,
        (item_id,),
    )
    fields = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()

    result: dict[str, str] = {}

    extra = fields.get("extra", "")
    arxiv_match = re.search(r"arXiv[:\s]+(\d{4}\.\d{4,5})", extra, re.IGNORECASE)
    if arxiv_match:
        result["arxiv_id"] = arxiv_match.group(1)

    doi = fields.get("DOI", "").strip()
    if doi:
        result["doi"] = doi
        result["doi_url"] = f"https://doi.org/{doi}"

    url = fields.get("url", "").strip()
    if url:
        result["url"] = url
        pmid_match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?", url)
        if pmid_match:
            result["pubmed_url"] = url
            result["pmid"] = pmid_match.group(1)
        if "arxiv.org" in url and "arxiv_id" not in result:
            arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
            if arxiv_match:
                result["arxiv_id"] = arxiv_match.group(1)

    return result or None


def normalize_method_name(value: str) -> str:
    normalized = value.strip().lower().translate(SUBSCRIPT_TRANSLATION).replace("&", "and")
    for source, target in GREEK_REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    return re.sub(r"[^a-z0-9]+", "", normalized)


def extract_note_method_names(stem: str) -> set[str]:
    candidates = {stem}
    match = re.match(r"^(?:19|20)\d{2}_(.+)$", stem)
    if match:
        candidates.add(match.group(1))
    return {normalize_method_name(item) for item in candidates if normalize_method_name(item)}


def get_existing_notes() -> dict[str, str]:
    existing: dict[str, str] = {}
    if not PAPER_NOTES_ROOT.exists():
        return existing

    for md_file in PAPER_NOTES_ROOT.rglob("*.md"):
        relative_parts = md_file.relative_to(PAPER_NOTES_ROOT).parts
        if any(part.startswith("_") for part in relative_parts):
            continue
        if md_file.parent.name == md_file.stem:
            continue
        for method_name in extract_note_method_names(md_file.stem):
            existing[method_name] = str(md_file)
    return existing


def title_matches_note(title: str, existing_notes: dict[str, str]) -> bool:
    if not title:
        return False

    candidates = {
        normalize_method_name(title.strip()),
        normalize_method_name(title.split(":", 1)[0].strip()),
    }
    for candidate in candidates:
        if not candidate:
            continue
        for note_method in existing_notes:
            if candidate == note_method:
                return True
            if len(note_method) > 3 and note_method in candidate and len(note_method) >= len(candidate) * 0.5:
                return True
    return False


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {"completed": [], "failed": [], "current": None, "started_at": None}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_codex_prompt(paper_source: dict, collection_path: str, item_id: int) -> str:
    source_lines = []
    if paper_source.get("title"):
        source_lines.append(f"Title: {paper_source['title']}")
    if paper_source.get("pdf_path"):
        source_lines.append(f"PDF path: {paper_source['pdf_path']}")
    if paper_source.get("pubmed_url"):
        source_lines.append(f"PubMed URL: {paper_source['pubmed_url']}")
    if paper_source.get("pmid"):
        source_lines.append(f"PMID: {paper_source['pmid']}")
    if paper_source.get("doi"):
        source_lines.append(f"DOI: {paper_source['doi']}")
        source_lines.append(f"DOI page: https://doi.org/{paper_source['doi']}")
    if paper_source.get("url"):
        source_lines.append(f"Fallback URL: {paper_source['url']}")
    if paper_source.get("arxiv_id"):
        source_lines.append(f"arXiv ID: {paper_source['arxiv_id']}")

    extra = ""
    if not paper_source.get("pdf_path"):
        extra = """
No local PDF is available.

Workflow:
1. Open the DOI page first and check whether the publisher page exposes a PDF or readable full text.
2. If a PDF is available, read the PDF and summarize the paper as `基于全文`.
3. If no PDF is available, fall back to the PubMed abstract, metadata, and visible publisher-page content, and label the result as `基于摘要/元数据`.
4. Do not invent inaccessible full-text details.
"""

    return f"""Use the `paper-reader` skill workflow to read this paper and save the note into Obsidian when possible.

{os.linesep.join(source_lines)}
Zotero collection path: {collection_path}
Zotero item id: {item_id}

{extra}

Required output:
- paper topic
- research question
- core methods
- main findings
- limitations
- explicit label: `基于全文` or `基于摘要/元数据`

Save paper notes under: {PAPER_NOTES_ROOT}
Save concept notes under: {CONCEPTS_ROOT}
"""


def call_codex(paper_source: dict, collection_path: str, item_id: int) -> tuple[bool, str]:
    prompt = build_codex_prompt(paper_source, collection_path, item_id)
    cmd = [
        CODEX_BIN,
        "exec",
        "--full-auto",
        "--skip-git-repo-check",
        "-C",
        CODEX_WORKDIR,
    ]
    if CODEX_MODEL:
        cmd.extend(["--model", CODEX_MODEL])
    if CODEX_EXTRA_ARGS:
        cmd.extend(shlex.split(CODEX_EXTRA_ARGS))
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as exc:
        return False, str(exc)

    output = (result.stdout or "") + (result.stderr or "")
    limit_type = detect_limit_error(output)
    if limit_type == "RATE_LIMIT":
        return False, "RATE_LIMIT"
    if limit_type == "QUOTA_LIMIT":
        return False, f"QUOTA_LIMIT|{output[:400]}"
    if result.returncode == 0:
        return True, ""
    return False, output[:500]


def process_collection(collection_name: str, resume: bool = True) -> None:
    logger.info("Start processing Zotero collection: %s", collection_name)
    db_path = copy_zotero_db()
    collection_id, collection_path = get_collection_id_and_path(db_path, collection_name)
    if not collection_id:
        logger.error("Collection not found: %s", collection_name)
        return

    papers = get_papers_in_collection(db_path, collection_id)
    logger.info("Found %s items in collection %s", len(papers), collection_path)

    progress = load_progress() if resume else {"completed": [], "failed": [], "current": None, "started_at": None}
    if not progress["started_at"]:
        progress["started_at"] = datetime.now().isoformat()

    existing_notes = get_existing_notes()
    logger.info("Detected %s existing Obsidian paper notes", len(existing_notes))

    pending = []
    for paper in papers:
        item_id = paper["item_id"]
        title = paper["title"]
        if item_id in progress["completed"]:
            continue
        if title_matches_note(title, existing_notes):
            progress["completed"].append(item_id)
            continue

        paper_source = {"title": title}
        pdf_path = get_pdf_path(db_path, item_id)
        if pdf_path is not None:
            paper_source["pdf_path"] = str(pdf_path)
        else:
            online_source = get_paper_online_source(db_path, item_id)
            if not online_source:
                logger.warning("Skip item without PDF or online source: %s", title[:80])
                continue
            paper_source.update(online_source)

        pending.append({**paper, "source": paper_source})

    save_progress(progress)
    logger.info("Pending papers: %s", len(pending))

    wait_time = INITIAL_WAIT
    for index, paper in enumerate(pending, start=1):
        item_id = paper["item_id"]
        title = paper["title"]
        paper_source = paper["source"]
        progress["current"] = {"item_id": item_id, "title": title}
        save_progress(progress)

        logger.info("[%s/%s] Processing %s", index, len(pending), title[:80])
        success, error = call_codex(paper_source, collection_path or collection_name, item_id)

        if success:
            progress["completed"].append(item_id)
            progress["current"] = None
            save_progress(progress)
            wait_time = INITIAL_WAIT
            if index < len(pending):
                time.sleep(BETWEEN_PAPERS_WAIT)
            continue

        if error == "RATE_LIMIT":
            logger.warning("Rate limit hit, waiting %s seconds", wait_time)
            time.sleep(wait_time)
            wait_time = min(wait_time * WAIT_MULTIPLIER, MAX_WAIT)
            pending.append(paper)
            continue

        if error.startswith("QUOTA_LIMIT"):
            reset_wait = parse_reset_wait_seconds(error)
            if reset_wait:
                logger.warning("Usage limit hit, waiting %s minutes", reset_wait // 60)
                time.sleep(reset_wait)
            else:
                wait_for_quota_reset()
            pending.append(paper)
            continue

        progress["failed"].append({"item_id": item_id, "title": title, "error": error[:200]})
        progress["current"] = None
        save_progress(progress)
        logger.error("Failed to process %s: %s", title[:80], error[:160])

    progress["finished_at"] = datetime.now().isoformat()
    progress["current"] = None
    save_progress(progress)
    logger.info("Finished. Success=%s Failed=%s", len(progress["completed"]), len(progress["failed"]))


def show_status() -> None:
    progress = load_progress()
    print("=== Paper Daemon Status ===")
    print(f"Started: {progress.get('started_at', 'N/A')}")
    print(f"Finished: {progress.get('finished_at', 'running')}")
    print(f"Completed: {len(progress.get('completed', []))}")
    print(f"Failed: {len(progress.get('failed', []))}")
    current = progress.get("current")
    if current:
        print(f"Current: {current.get('title', '')[:100]}")


def list_collections() -> None:
    db_path = copy_zotero_db()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.collectionName, COUNT(ci.itemID) AS count
        FROM collections c
        LEFT JOIN collectionItems ci ON c.collectionID = ci.collectionID
        GROUP BY c.collectionID
        HAVING count > 0
        ORDER BY c.collectionName
        """
    )
    print("=== Zotero Collections ===")
    for name, count in cursor.fetchall():
        print(f"{name}: {count}")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch paper reader daemon")
    parser.add_argument("--collection", "-c", help="Zotero collection name")
    parser.add_argument("--status", "-s", action="store_true", help="Show progress status")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume previous progress")
    parser.add_argument("--list", "-l", action="store_true", help="List Zotero collections")
    args = parser.parse_args()

    if args.status:
        show_status()
        return
    if args.list:
        list_collections()
        return
    if not args.collection:
        parser.print_help()
        return

    if not acquire_lock():
        logger.error("Another paper_daemon process is already running")
        return

    try:
        process_collection(args.collection, resume=not args.no_resume)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
