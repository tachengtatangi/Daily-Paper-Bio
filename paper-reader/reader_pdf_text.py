"""PDF text extraction helpers for paper-reader."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from reader_text import normalize_whitespace, safe_filename

FindTool = Callable[[str], str | None]


def extract_pdf_text_with_pdftotext(path: Path, find_tool_func: FindTool | None = None) -> str:
    finder = find_tool_func or shutil.which
    pdftotext = finder("pdftotext.exe") or finder("pdftotext")
    if not pdftotext:
        return ""
    out_txt = path.with_suffix(path.suffix + ".txt")
    try:
        result = subprocess.run(
            [pdftotext, "-layout", str(path), str(out_txt)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0 or not out_txt.exists():
            return ""
        text = out_txt.read_text(encoding="utf-8", errors="replace")
        return normalize_whitespace(text)[:45000]
    except Exception:
        return ""
    finally:
        try:
            out_txt.unlink(missing_ok=True)
        except Exception:
            pass


def paper_reader_pdf_text_engine() -> str:
    return normalize_whitespace(os.environ.get("PAPER_READER_PDF_TEXT_ENGINE", "mineru_first")).lower()


def mineru_enabled() -> bool:
    engine = paper_reader_pdf_text_engine()
    return engine not in {"legacy", "pdftotext", "pypdf", "pymupdf", "off", "disabled", "0", "false"}


def mineru_timeout_seconds() -> int:
    raw = normalize_whitespace(os.environ.get("PAPER_READER_MINERU_TIMEOUT", "300"))
    try:
        return max(30, min(1800, int(raw)))
    except Exception:
        return 300


def paper_reader_mineru_cleanup_enabled() -> bool:
    raw = normalize_whitespace(os.environ.get("PAPER_READER_KEEP_MINERU_ARTIFACTS", ""))
    return raw.lower() not in {"1", "true", "yes", "on"}


def strip_mineru_markdown_noise(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return normalize_whitespace(text)


def extract_pdf_text_with_mineru(
    pdf_path: Path,
    identifier: str = "",
    *,
    find_tool_func: FindTool | None = None,
    pdf_save_dir: Path | None = None,
) -> dict:
    """Return MinerU markdown-derived text for a PDF, or {} on any failure."""
    if not mineru_enabled():
        return {}
    try:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
            return {}
        finder = find_tool_func or shutil.which
        mineru = finder("mineru.exe") or finder("mineru") or shutil.which("mineru")
        if not mineru:
            return {}
        safe_ident = safe_filename(identifier or pdf_path.stem or "paper")
        if pdf_save_dir:
            out_root = pdf_save_dir / "mineru" / safe_ident
        else:
            out_root = Path(tempfile.mkdtemp(prefix="paper_reader_mineru_")) / safe_ident
        out_root.mkdir(parents=True, exist_ok=True)
        method = normalize_whitespace(os.environ.get("PAPER_READER_MINERU_METHOD", "auto")) or "auto"
        backend = normalize_whitespace(os.environ.get("PAPER_READER_MINERU_BACKEND", "pipeline")) or "pipeline"
        cmd = [mineru, "-p", str(pdf_path), "-o", str(out_root), "--method", method, "--backend", backend]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=mineru_timeout_seconds(),
        )
        if result.returncode != 0:
            if normalize_whitespace(os.environ.get("PAPER_READER_MINERU_DEBUG", "")):
                print(f"[paper-reader] MinerU failed for {pdf_path.name}: {result.stderr[-500:]}", file=sys.stderr)
            return {}
        md_files = sorted(out_root.rglob("*.md"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
        for md_path in md_files:
            raw = md_path.read_text(encoding="utf-8", errors="replace")
            parsed = strip_mineru_markdown_noise(raw)
            if len(parsed) >= 1500 or ("abstract" in parsed.lower() and len(parsed) >= 800):
                return {
                    "text": parsed[:60000],
                    "markdown_path": str(md_path),
                    "output_dir": str(out_root),
                    "summary_mode": "?? MinerU PDF->Markdown ????",
                    "acquisition_path": "MinerU PDF->Markdown text + legacy/publisher figures",
                }
    except subprocess.TimeoutExpired:
        if normalize_whitespace(os.environ.get("PAPER_READER_MINERU_DEBUG", "")):
            print(f"[paper-reader] MinerU timed out for {pdf_path}", file=sys.stderr)
    except Exception as exc:
        if normalize_whitespace(os.environ.get("PAPER_READER_MINERU_DEBUG", "")):
            print(f"[paper-reader] MinerU error for {pdf_path}: {exc}", file=sys.stderr)
    return {}


def upgrade_record_with_mineru_text(
    record: dict,
    identifier: str = "",
    *,
    find_tool_func: FindTool | None = None,
    pdf_save_dir: Path | None = None,
) -> dict:
    pdf_value = normalize_whitespace(str(
        record.get("downloaded_pdf", "")
        or record.get("publisher_pdf", "")
        or record.get("local_pdf", "")
        or record.get("pdf_path", "")
        or ""
    ))
    if not pdf_value:
        return record
    pdf_path = Path(pdf_value)
    if not pdf_path.is_absolute() and pdf_save_dir:
        candidate = pdf_save_dir.parent / pdf_path
        if candidate.exists():
            pdf_path = candidate
    info = extract_pdf_text_with_mineru(
        pdf_path,
        identifier or record.get("doi", "") or record.get("title", ""),
        find_tool_func=find_tool_func,
        pdf_save_dir=pdf_save_dir,
    )
    if not info.get("text"):
        return record
    upgraded = dict(record)
    upgraded["full_text"] = info["text"]
    upgraded["summary_mode"] = info.get("summary_mode") or upgraded.get("summary_mode", "")
    upgraded["acquisition_path"] = info.get("acquisition_path") or upgraded.get("acquisition_path", "")
    upgraded["mineru_markdown"] = info.get("markdown_path", "")
    upgraded["mineru_output_dir"] = info.get("output_dir", "")
    return upgraded


def extract_pdf_text_from_bytes(
    data: bytes,
    temp_pdf_path: Path | None = None,
    *,
    pdftotext_func: Callable[[Path], str] | None = None,
) -> str:
    if temp_pdf_path is not None and pdftotext_func is not None:
        parsed = pdftotext_func(temp_pdf_path)
        if parsed:
            return parsed
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:20]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        parsed = normalize_whitespace("\n".join(pages))
        if parsed:
            return parsed[:45000]
    except Exception:
        pass

    chunks = []
    for match in re.finditer(rb"stream\r\n(.*?)endstream", data, re.DOTALL):
        chunk = match.group(1).strip(b"\r\n")
        candidates = [chunk]
        try:
            candidates.append(zlib.decompress(chunk))
        except Exception:
            pass
        for candidate in candidates:
            decoded = candidate.decode("latin-1", errors="ignore")
            paren_text = re.findall(r"\(([^()]{3,500})\)", decoded)
            if paren_text:
                chunks.extend(paren_text)
            else:
                chunks.extend(re.findall(r"[A-Za-z][A-Za-z0-9 ,.;:%()/_\-]{20,}", decoded))
    return normalize_whitespace(" ".join(chunks))[:25000]
