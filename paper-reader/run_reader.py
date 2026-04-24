#!/usr/bin/env python3
"""Single-entry paper reader pipeline with model-first note generation."""

from __future__ import annotations

import argparse
import ast
import base64
import html
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader
from readability import Document
try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:
    PlaywrightTimeoutError = Exception
    sync_playwright = None


SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_DIR = SCRIPT_DIR.parent / "_shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from user_config import ncbi_api_key, obsidian_vault_path, paper_notes_dir, pdf_picture_root_dir, elsevier_api_key


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GEN_PAPER_MOC = load_module("generate_paper_mocs", SHARED_DIR / "generate_paper_mocs.py")

USER_AGENT = "paper-reader/1.0"
PUBMED_FETCH_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_SEARCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_ELINK_BASE    = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
NCBI_API_KEY       = ncbi_api_key()
POWERSHELL = str(
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
POPPLER_HINTS = []
PLAYWRIGHT_SESSION = "paper-reader"
PREFER_VISIBLE_BROWSER = False
SKIP_PLAYWRIGHT = False
PDF_SAVE_DIR: Path | None = None
CHROME_CANDIDATES = [
    str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
    str(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
]
STANDARD_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)


def fetch_url(url: str, timeout: int = 60, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update({key: value for key, value in headers.items() if value})
    req = Request(url, headers=request_headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def add_ncbi_api_key(params: dict[str, str]) -> dict[str, str]:
    enriched = dict(params)
    if NCBI_API_KEY:
        enriched["api_key"] = NCBI_API_KEY
    return enriched


def find_tool(exe_name: str) -> str | None:
    found = shutil.which(exe_name)
    if found:
        return found
    dynamic_hints = list(POPPLER_HINTS)
    winget_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        dynamic_hints.extend(str(path) for path in winget_root.glob("oschwartz10612.Poppler*/poppler-*/Library/bin"))
    for base in dynamic_hints:
        candidate = Path(base) / exe_name
        if candidate.exists():
            return str(candidate)
    return None


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def unique_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items or []:
        norm = normalize_whitespace(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def clean_author_candidates(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in unique_keep_order(items):
        low = item.lower()
        if (
            "@" in item
            or "orcid.org/" in low
            or "search for more papers by this author" in low
            or "search for articles by this author" in low
            or "corresponding author" in low
            or low in {"individual login", "institutional login", "register"}
        ):
            continue
        if len(item) > 80:
            continue
        if re.search(r"\b(?:http|www\.)", low):
            continue
        if not re.search(r"[A-Za-z\u00C0-\u024F]", item):
            continue
        cleaned.append(item)
    return unique_keep_order(cleaned)


def clean_structured_text(text: object) -> str:
    if isinstance(text, (list, tuple, set)):
        parts = [clean_structured_text(item) for item in text]
        parts = [part for part in parts if part]
        if not parts:
            return ""
        return chr(10).join(f"- {part}" if not part.lstrip().startswith("-") else part for part in parts).strip()
    if isinstance(text, dict):
        parts = []
        for key, value in text.items():
            cleaned = clean_structured_text(value)
            if cleaned:
                parts.append(f"{key}: {cleaned}" if key else cleaned)
        return chr(10).join(parts).strip()
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in raw.split("\n"):
        stripped = re.sub(r"[ \t]+", " ", line).strip()
        if stripped:
            lines.append(stripped)
    return chr(10).join(lines).strip()


def text_or_empty(node) -> str:
    return node.text.strip() if node is not None and node.text else ""


def safe_filename(text: str) -> str:
    text = re.sub(r'[\\/:*""<>|]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:110].strip(" .") or "untitled"


def guess_image_extension(data: bytes, source_url: str = "") -> str:
    lower = (source_url or "").lower()
    if lower.endswith(".png") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg") or data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if lower.endswith(".webp") or (len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return ".webp"
    if lower.endswith(".gif") or data[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    return ".png"


def clean_record_title(value: str) -> str:
    title = normalize_whitespace(value or "")
    bad = {
        "",
        "[no-title]",
        "no-title",
        "untitled",
        "untitled-paper",
        "redirecting",
        "please wait",
        "loading...",
    }
    return "" if title.lower() in bad else title


def parse_doi_candidate(value: str) -> str:
    value = (value or "").strip()
    lower = value.lower()
    if "/article/doi/" in lower:
        try:
            tail = value[lower.index("/article/doi/") + len("/article/doi/") :]
            tail = tail.split('"', 1)[0].split("#", 1)[0].strip("/")
            parts = [part for part in tail.split("/") if part]
            if len(parts) >= 3 and parts[0].startswith("10.") and re.fullmatch(r"\d+", parts[-1]):
                return "/".join(parts[:-1])
        except Exception:
            pass
    if value.startswith("https://doi.org/"):
        return value.split("https://doi.org/", 1)[1].strip("/")
    if value.startswith("http://doi.org/"):
        return value.split("http://doi.org/", 1)[1].strip("/")
    if re.match(r"10\.\d{4,9}/\S+", value):
        return value
    return ""


def find_doi_in_text(text: str) -> str:
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text or "", re.IGNORECASE)
    return match.group(0).rstrip(").,;]") if match else ""


def detect_source_kind(source: str) -> tuple[str, str]:
    source = source.strip()
    if re.match(r"^https?://", source, re.IGNORECASE):
        if "pubmed.ncbi.nlm.nih.gov" in source:
            return "pubmed_url", source
        if "pmc.ncbi.nlm.nih.gov" in source:
            return "pmc_url", source
        if source.lower().endswith(".pdf"):
            return "pdf_url", source
        publisher_hosts = (
            "academic.oup.com",
            "oup.com",
            "www.pnas.org",
            "pnas.org",
            "sciencedirect.com",
            "www.sciencedirect.com",
            "www.nature.com",
            "nature.com",
            "onlinelibrary.wiley.com",
            "wiley.com",
            "link.springer.com",
            "springer.com",
        )
        if any(host in source.lower() for host in publisher_hosts):
            return "web_url", source
        doi = parse_doi_candidate(source)
        if doi:
            return "doi", doi
        return "web_url", source
    doi = parse_doi_candidate(source)
    if doi:
        return "doi", doi
    path = Path(source).expanduser()
    if path.suffix.lower() == ".pdf":
        return "local_pdf", str(path)
    return "web_url", source


def is_biorxiv_or_medrxiv_content_url(source: str) -> bool:
    low = normalize_whitespace(source).lower()
    return "biorxiv.org/content/" in low or "medrxiv.org/content/" in low


def preprint_server_name(source: str) -> str:
    low = normalize_whitespace(source).lower()
    return "medrxiv" if "medrxiv.org" in low else "biorxiv"


def extract_preprint_doi(source: str) -> str:
    source = normalize_whitespace(source)
    doi = parse_doi_candidate(source)
    if doi.startswith("10.1101/"):
        return re.sub(r"v\d+$", "", doi, flags=re.IGNORECASE)
    match = re.search(r'/content/(10\.1101/[^/"#]+)', source, re.IGNORECASE)
    if match:
        return re.sub(r"v\d+$", "", match.group(1).strip("/"), flags=re.IGNORECASE)
    return ""


def extract_preprint_version(source: str) -> str:
    source = normalize_whitespace(source)
    match = re.search(r'/content/10\.1101/[^/"#]+?(v\d+)(?:[/"#]|$)', source, re.IGNORECASE)
    if match:
        return match.group(1)
    doi = parse_doi_candidate(source)
    match = re.search(r"(v\d+)$", doi, re.IGNORECASE)
    return match.group(1) if match else ""


def split_preprint_authors(raw: str) -> list[str]:
    text = normalize_whitespace(raw)
    if not text:
        return []
    if ";" in text:
        parts = [part.strip() for part in text.split(";")]
    elif " and " in text:
        parts = [part.strip() for part in text.split(" and ")]
    else:
        parts = [text]
    return [part for part in parts if part]


def build_preprint_pdf_url(source: str, doi: str) -> str:
    source = normalize_whitespace(source)
    if is_biorxiv_or_medrxiv_content_url(source):
        return source.rstrip("/") + ".full.pdf"
    if doi:
        server = preprint_server_name(source or doi)
        version = extract_preprint_version(source or doi) or "v1"
        return f"https://www.{server}.org/content/{doi}{version}.full.pdf"
    return ""


def fetch_preprint_record(source: str) -> dict:
    doi = extract_preprint_doi(source)
    if not doi:
        return {}
    server = preprint_server_name(source)
    version_hint = extract_preprint_version(source)
    api_url = f"https://api.{server}.org/details/{server}/{quote(doi, safe='/')}"
    try:
        payload = json.loads(fetch_url(api_url).decode("utf-8", errors="replace"))
    except Exception:
        return {}

    collection = payload.get("collection") or []
    if not collection:
        return {}

    chosen = None
    if version_hint:
        chosen = next((item for item in collection if normalize_whitespace(item.get("version", "")) == version_hint.lstrip("v")), None)
    if chosen is None:
        chosen = collection[-1]

    content_url = normalize_whitespace(source) if normalize_whitespace(source) else ""
    if not content_url:
        version = normalize_whitespace(chosen.get("version", "")) or "1"
        content_url = f"https://www.{server}.org/content/{doi}v{version}"

    record = {
        "source_kind": "web_url",
        "title": normalize_whitespace(chosen.get("title", "")),
        "authors": split_preprint_authors(chosen.get("authors", "")),
        "journal": "bioRxiv" if server == "biorxiv" else "medRxiv",
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}",
        "web_url": content_url,
        "abstract": normalize_whitespace(chosen.get("abstract", "")),
        "keywords": [],
        "full_text": "",
        "summary_mode": f"基于 {'bioRxiv' if server == 'biorxiv' else 'medRxiv'} 元数据/摘要",
        "image_url": "",
        "acquisition_path": f"{server} API details",
    }

    pdf_url = build_preprint_pdf_url(content_url, doi)
    if pdf_url:
        record["pdf_url"] = pdf_url
        try:
            pdf_record = fetch_pdf_url(pdf_url, referer=content_url)
            if pdf_record.get("downloaded_pdf") or pdf_record.get("full_text"):
                merged = merge_fulltext_record(record, pdf_record)
                merged["acquisition_path"] = f"{server} API details + PDF fallback"
                merged["summary_mode"] = pdf_record.get("summary_mode", "") or "基于全文/PDF文本提取"
                return merged
        except Exception:
            pass
    return record


def first_sentence(text: str) -> str:
    clean = normalize_whitespace(text)
    if not clean:
        return ""
    return re.split(r"(?<=[.!?。！？])\s+", clean)[0].strip()


def sentence_chunks(text: str, limit: int = 6) -> list[str]:
    clean = normalize_whitespace(text)
    if not clean:
        return []
    return [item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", clean) if item.strip()][:limit]


def markdown_bullets(items: list[str], limit: int | None = None, fallback: str = "No evidence available.") -> str:
    cleaned = [normalize_whitespace(item) for item in items if normalize_whitespace(item)]
    if limit is not None:
        cleaned = cleaned[:limit]
    if not cleaned:
        return f"- {fallback}"
    return "\n".join(f"- {item}" for item in cleaned)


def ensure_question(text: str) -> str:
    clean = normalize_whitespace(text)
    if not clean:
        return "What core question does this study ask?"
    clean = re.sub(r"[.?!]+$", "", clean)
    if clean.endswith("?"):
        return clean
    return f"{clean}?"


def article_sentences(text: str, limit: int = 5) -> list[str]:
    candidates = []
    for chunk in sentence_chunks(text, limit=limit * 2):
        normalized = normalize_whitespace(chunk)
        low = normalized.lower()
        if not normalized:
            continue
        if len(normalized) < 18 and low in {
            "abstract",
            "introduction",
            "background",
            "methods",
            "materials and methods",
            "results",
            "discussion",
            "conclusion",
            "highlights",
            "graphical abstract",
            "references",
        }:
            continue
        if re.fullmatch(r"[A-Z0-9 \-,:;()]+", normalized) and len(normalized) <= 40:
            continue
        candidates.append(normalized)
        if len(candidates) >= limit:
            break
    return candidates


def figure_takeaways_from_record(record: dict) -> str:
    items = record.get("figure_items", []) or []
    paths = record.get("figure_paths", []) or []
    takeaways: list[str] = []
    seen = set()

    for item in items:
        caption = normalize_whitespace(item.get("caption", "") or item.get("alt", ""))
        if not caption or caption in seen:
            continue
        seen.add(caption)
        takeaways.append(caption)
        if len(takeaways) >= 3:
            break

    if takeaways:
        return markdown_bullets(takeaways, limit=3)

    if paths:
        count = len(paths)
        return markdown_bullets(
            [
                f"已提取到 {count} 张图像，但当前没有可直接解释的图注或 alt 文本。",
                "如果需要图级解读，建议回看原文图注或正文上下文。",
            ]
        )

    return "- 当前没有可解释的图注或稳定图像，暂时无法给出图级 takeaway。"


def split_structured_section_line(line: str) -> tuple[str, str]:
    stripped = normalize_whitespace(line)
    stripped = re.sub(r"^[\-*•\d.\s]+", "", stripped)
    match = re.match(r"^(finding|basis|path|caption|takeaway|figure)\s*[:：]\s*(.+)$", stripped, re.IGNORECASE)
    if match:
        return match.group(1).lower(), normalize_whitespace(match.group(2))
    return "", stripped


def looks_like_internal_dump_line(line: str) -> bool:
    stripped = normalize_whitespace(line)
    if not stripped:
        return False
    lowered = stripped.lower()
    if re.match(
        r'^(summary_mode|source|title|authors|journal|doi|image_url|web_url|doi_url|pubmed_url|pdf_path|downloaded_pdf|local_pdf|figure_paths|figure_items|acquisition_path|abstract_available|full_text_excerpt_available|figure_count|evidence_note|source url|pdf downloaded|figure paths extracted|figure items extracted|evidence level|full text/body|full text available|images|has abstract|has full text excerpt|has figures|access issue)\s*[:：]',
        lowered,
    ):
        return True
    return False


def sanitize_section_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        candidate = normalize_whitespace(line)
        if not candidate or looks_like_internal_dump_line(candidate):
            continue
        cleaned.append(candidate)
    return unique_keep_order(cleaned)


def rewrite_internal_field_terms(text: str) -> str:
    rewritten = normalize_whitespace(text)
    if not rewritten:
        return ""
    rewritten = rewritten.replace("`", "")
    replacements = [
        (r"\bsummary_mode\b", "证据层级"),
        (r"\bfull_text_excerpt\b", "正文摘录"),
        (r"\bfigure_items\b", "图注材料"),
        (r"\bfigure_paths\b", "图像文件"),
        (r"\bmaterials\.json\b", "当前材料"),
        (r"\bweb_url\b", "网页落地页"),
        (r"\bpdf_path\b", "本地 PDF"),
        (r"\bdownloaded_pdf\b", "已下载 PDF"),
        (r"\bJSON\b", "材料"),
        (r"材料标注的证据层级为", "当前证据层级为"),
        (r"证据层级：\s*证据层级\s*为", "当前证据层级为"),
        (r"证据层级\s*标记为", "当前证据层级为"),
        (r"JSON 里的", "当前保存的"),
        (r"(\d+)\s*张本地图注材料", r"\1 张本地图像"),
        (r"没有本地 PDF，\s*本地 PDF\s*和\s*已下载 PDF\s*都为空", "当前没有拿到本地 PDF"),
    ]
    for pattern, repl in replacements:
        rewritten = re.sub(pattern, repl, rewritten, flags=re.IGNORECASE)
    return normalize_whitespace(rewritten)


def render_main_findings_text(text: str) -> str:
    lines = [normalize_whitespace(line) for line in str(text or "").splitlines() if normalize_whitespace(line)]
    findings: list[str] = []
    current_finding = ""
    current_basis = ""
    pending: list[str] = []

    for line in lines:
        label, body = split_structured_section_line(line)
        if label == "finding":
            if current_finding:
                findings.append(
                    f"{current_finding}；依据是 {current_basis}" if current_basis else current_finding
                )
            current_finding = body
            current_basis = ""
            continue
        if label == "basis":
            if current_finding:
                current_basis = rewrite_internal_field_terms(body)
            elif body:
                pending.append(rewrite_internal_field_terms(body))
            continue
        if body:
            if current_finding and not current_basis:
                current_finding = f"{current_finding} {rewrite_internal_field_terms(body)}".strip()
            else:
                pending.append(rewrite_internal_field_terms(body))

    if current_finding:
        findings.append(
            f"{current_finding}；依据是 {current_basis}" if current_basis else current_finding
        )

    findings = unique_keep_order(findings)
    if findings:
        out = [f"- {item}" for item in findings[:5]]
        if pending:
            out.extend(f"- {item}" for item in unique_keep_order(pending)[:2])
        return chr(10).join(out)

    cleaned = []
    for line in lines:
        label, body = split_structured_section_line(line)
        cleaned.append(rewrite_internal_field_terms(body or line))
    cleaned = unique_keep_order(cleaned)
    if cleaned:
        return chr(10).join(f"- {item}" for item in cleaned[:5])
    return "- 当前没有可整理为自然中文的主要发现。"


def render_figure_takeaways_text(text: str) -> str:
    lines = [normalize_whitespace(line) for line in str(text or "").splitlines() if normalize_whitespace(line)]
    figure_label = ""
    captions: list[str] = []
    takeaways: list[str] = []

    for line in lines:
        label, body = split_structured_section_line(line)
        if label == "figure":
            figure_label = rewrite_internal_field_terms(body)
            continue
        if label == "caption":
            captions.append(rewrite_internal_field_terms(body))
            continue
        if label == "path":
            continue
        if label == "takeaway":
            takeaways.append(rewrite_internal_field_terms(body))
            continue
        takeaways.append(rewrite_internal_field_terms(body))

    takeaways = unique_keep_order(takeaways)
    if takeaways:
        if figure_label and not takeaways[0].startswith(figure_label):
            takeaways[0] = f"{figure_label}：{takeaways[0]}"
        return chr(10).join(f"- {item}" for item in takeaways[:3])

    if captions:
        caption = unique_keep_order(captions)[0]
        if count_cjk_chars(caption) < 10 and count_latin_tokens(caption) > 6:
            prefix = f"{figure_label}：" if figure_label else ""
            return f"- {prefix}这张图对应论文中的关键图像，图注细节需要结合正文进一步核对。"
        prefix = f"{figure_label}：" if figure_label else ""
        return f"- {prefix}图注要点：{caption}"

    return "- 当前没有可解释的图注或稳定图像，暂时无法给出图级 takeaway。"


def sanitize_figure_summary_text(text: str, record: dict) -> str:
    summary = normalize_whitespace(text)
    if not summary:
        return ""
    figure_count = len(record.get("figure_paths", []) or [])
    replacements = [
        (r"materials\s*里有\s*\d+\s*个本地\s*figure_paths", f"当前本地保留了 {figure_count} 张图像文件" if figure_count else "当前已保留本地图像文件"),
        (r"figure_paths\s*中实际只提取到\s*\d+\s*张本地\s*PNG", f"当前本地保留了 {figure_count} 张图像文件" if figure_count else "当前本地只保留了少量图像文件"),
        (r"figure_items\s*的\s*caption\s*为空", "当前没有同步到可直接引用的图注文本"),
        (r"材料里有\s*\d+\s*个图像文件", f"当前本地保留了 {figure_count} 张图像文件" if figure_count else "当前已保留本地图像文件"),
        (r"JSON 里的\s*图注材料", "当前保存的图注材料"),
        (r"materials\s*里", "当前材料中"),
        (r"figure_paths", "本地图像"),
        (r"figure_items", "图注材料"),
    ]
    for pattern, repl in replacements:
        summary = re.sub(pattern, repl, summary, flags=re.IGNORECASE)
    summary = re.sub(r"\bPNG\b", "图片", summary, flags=re.IGNORECASE)
    summary = normalize_whitespace(summary.replace("中文说明：", ""))
    return summary


def render_note_section_text(section_key: str, text: str) -> str:
    cleaned = clean_structured_text(text)
    if section_key == "main_findings":
        return render_main_findings_text(cleaned)
    if section_key == "figure_takeaways":
        return render_figure_takeaways_text(cleaned)
    if section_key == "data_materials":
        lines = [normalize_whitespace(line) for line in cleaned.splitlines() if normalize_whitespace(line)]
        lines = [rewrite_internal_field_terms(split_structured_section_line(line)[1] or line) for line in lines]
        lines = sanitize_section_lines(lines)
        rendered = []
        for line in lines:
            if line.startswith("- "):
                rendered.append(line)
            else:
                rendered.append(f"- {line}")
        return chr(10).join(unique_keep_order(rendered)[:6]) if rendered else "- 当前未能稳定提取出可直接复述的数据与材料细节。"
    if section_key in {"strengths", "limitations", "quick_reference"}:
        # LLM sometimes writes multiple bullets inline on one line, separated by
        # " - " (space-dash-space) after a sentence-ending punctuation mark.
        # Normalise those to proper newlines first, then apply standard bullet
        # rendering (deduplicated, max 8 items, each prefixed with "- ").
        expanded = re.sub(r"(?<=[.。!！?？])\s+-\s+", "\n", cleaned)
        lines = [normalize_whitespace(line) for line in expanded.splitlines() if normalize_whitespace(line)]
        lines = [rewrite_internal_field_terms(split_structured_section_line(line)[1] or line) for line in lines]
        lines = sanitize_section_lines(lines)
        result = []
        for item in unique_keep_order(lines)[:8]:
            result.append(item if item.startswith("- ") else f"- {item}")
        return chr(10).join(result) if result else cleaned
    if section_key in {"core_methods", "background_context", "critical_analysis", "notes"}:
        lines = [normalize_whitespace(line) for line in cleaned.splitlines() if normalize_whitespace(line)]
        lines = [rewrite_internal_field_terms(split_structured_section_line(line)[1] or line) for line in lines]
        lines = sanitize_section_lines(lines)
        if section_key in {"notes", "critical_analysis"}:
            return chr(10).join(f"- {item}" for item in unique_keep_order(lines)[:6]) if lines else cleaned
        return chr(10).join(lines) if lines else cleaned
    return rewrite_internal_field_terms(cleaned)


def merge_records(base: dict, extra: dict, prefer_new: bool = True) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if key in {"authors", "affiliations", "keywords"}:
            current = merged.get(key) or []
            incoming = value or []
            if prefer_new:
                if incoming:
                    merged[key] = incoming
            else:
                if not current and incoming:
                    merged[key] = incoming
            continue
        if prefer_new:
            if value:
                merged[key] = value
        else:
            if not merged.get(key) and value:
                merged[key] = value
    return merged


def merge_fulltext_record(base: dict, extra: dict) -> dict:
    merged = merge_records(base, extra, prefer_new=False)
    for key in [
        "full_text",
        "summary_mode",
        "downloaded_pdf",
        "pdf_url",
        "figure_paths",
        "figure_items",
        "image_url",
        "web_url",
        "doi_url",
    ]:
        value = extra.get(key)
        if value:
            merged[key] = value
    return merged


def summary_mode_indicates_fulltext(record: dict | str) -> bool:
    if isinstance(record, dict):
        summary_mode = normalize_whitespace(record.get("summary_mode", ""))
        full_text_status = normalize_whitespace(record.get("full_text_status", ""))
        downloaded_pdf = normalize_whitespace(record.get("downloaded_pdf", ""))
    else:
        summary_mode = normalize_whitespace(record)
        full_text_status = ""
        downloaded_pdf = ""
    if downloaded_pdf:
        return True
    if full_text_status.lower() in {"elsevier_api", "pdf", "fulltext"}:
        return True
    low = summary_mode.lower()
    return any(token in low for token in ["全文", "正文", "pdf", "xml", "full text", "full-text", "fulltext", "可提取文本"])


def source_links(record: dict, source: str) -> dict:
    pdf_path = (
        record.get("downloaded_pdf", "")
        or record.get("publisher_pdf", "")
        or record.get("pdf_path", "")
        or record.get("local_pdf", "")
        or record.get("local_path", "")
    )
    return {
        "pubmed_url": record.get("pubmed_url", ""),
        "doi_url": record.get("doi_url", ""),
        "web_url": record.get("web_url", "") or (source if detect_source_kind(source)[0] == "web_url" else ""),
        "pdf_path": pdf_path,
    }


def format_markdown_link(label: str, target: str) -> str:
    target = (target or "").strip()
    if not target:
        return ""
    if re.match(r"^https?://", target, re.IGNORECASE):
        return f"[{label}]({target})"
    path = Path(target)
    if path.exists():
        try:
            vault_rel = path.resolve().relative_to(obsidian_vault_path().resolve())
            return f"![[{vault_rel.as_posix()}]]" if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'} else f"[[{vault_rel.as_posix()}]]"
        except Exception:
            safe_path = quote(path.resolve().as_posix(), safe="/:")
    return f"[{label}]({safe_path})"


def is_preprint_record(record: dict) -> bool:
    doi = parse_doi_candidate(normalize_whitespace(str(record.get("doi", "") or "")))
    web_url = normalize_whitespace(str(record.get("web_url", "") or ""))
    doi_url = normalize_whitespace(str(record.get("doi_url", "") or ""))
    hay = " ".join(part for part in [doi, web_url, doi_url] if part).lower()
    return doi.startswith("10.1101/") or "biorxiv.org" in hay or "medrxiv.org" in hay


def looks_like_browser_print_pdf_path(path_raw: str) -> bool:
    path_raw = normalize_whitespace(path_raw)
    if not path_raw:
        return False
    lower = path_raw.replace("\\", "/").lower()
    name = Path(path_raw).name.lower()
    if "/.playwright-cli/" in lower:
        return True
    if re.fullmatch(r"page-\d{4}-\d{2}-\d{2}t\d{2}[-:]\d{2}[-:]\d{2}.*\.pdf", name):
        return True
    if name.endswith(" - page.pdf") or " pageprint" in name:
        return True
    return False
    safe_target = quote(target.replace("\\", "/"), safe="/:")
    return f"[{label}]({safe_target})"


def parse_pubmed_xml(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    article = root.find("PubmedArticle")
    if article is None:
        return {}
    medline = article.find("MedlineCitation")
    pubmed = article.find("PubmedData")
    article_node = medline.find("Article") if medline is not None else None
    if medline is None or article_node is None:
        return {}

    pmid = text_or_empty(medline.find("PMID"))
    title_node = article_node.find("ArticleTitle")
    title = normalize_whitespace("".join(title_node.itertext()) if title_node is not None else "")

    abstract_parts = []
    abstract = article_node.find("Abstract")
    if abstract is not None:
        for child in abstract.findall("AbstractText"):
            label = child.attrib.get("Label", "").strip()
            body = normalize_whitespace("".join(child.itertext()))
            if body:
                abstract_parts.append(f"{label}: {body}" if label else body)

    authors = []
    affiliations = set()
    author_list = article_node.find("AuthorList")
    if author_list is not None:
        for author in author_list.findall("Author"):
            collective = text_or_empty(author.find("CollectiveName"))
            if collective:
                authors.append(collective)
            else:
                fore = text_or_empty(author.find("ForeName"))
                last = text_or_empty(author.find("LastName"))
                name = " ".join(part for part in [fore, last] if part)
                if name:
                    authors.append(name)
            for aff in author.findall("AffiliationInfo/Affiliation"):
                aff_text = text_or_empty(aff)
                if aff_text:
                    affiliations.add(aff_text)

    doi = ""
    if pubmed is not None:
        for article_id in pubmed.findall("ArticleIdList/ArticleId"):
            if article_id.attrib.get("IdType") == "doi":
                doi = text_or_empty(article_id)
                if doi:
                    break

    keywords = []
    for kw in medline.findall("KeywordList/Keyword"):
        item = normalize_whitespace("".join(kw.itertext()))
        if item:
            keywords.append(item)
    if not keywords:
        for mesh in medline.findall("MeshHeadingList/MeshHeading/DescriptorName")[:8]:
            item = normalize_whitespace("".join(mesh.itertext()))
            if item:
                keywords.append(item)

    return {
        "source_kind": "pubmed",
        "title": title,
        "authors": authors,
        "journal": text_or_empty(article_node.find("Journal/Title")),
        "pmid": pmid,
        "doi": doi,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": " ".join(abstract_parts).strip(),
        "affiliations": sorted(affiliations),
        "keywords": keywords,
        "full_text": "",
        "summary_mode": "基于摘要/元数据",
        "image_url": "",
    }


def fetch_pubmed_by_pmid(pmid: str) -> dict:
    params = add_ncbi_api_key({"db": "pubmed", "id": pmid, "retmode": "xml"})
    xml_text = fetch_url(f"{PUBMED_FETCH_BASE}?{urlencode(params)}").decode("utf-8", errors="replace")
    record = parse_pubmed_xml(xml_text)
    page_meta = fetch_pubmed_page_metadata(pmid)
    if page_meta:
        record = merge_records(record, page_meta, prefer_new=False)
        for link in page_meta.get("full_text_links", []):
            full_record = try_fetch_full_text_link(link)
            if full_record and full_record.get("full_text"):
                return merge_fulltext_record(record, full_record)
    return record


def _fetch_pmc_id_via_elink(pmid: str) -> str:
    """Query NCBI eLink API to find the PMC ID for a PubMed paper.

    Returns the PMC ID as a plain digit string (e.g. "9876543"), or "" if
    the paper is not available in PubMed Central (paywalled or not deposited).

    API: elink.fcgi?dbfrom=pubmed&db=pmc&id={pmid}&retmode=json
    Rate-limit: respects NCBI's 3 req/s limit via the shared fetch_url path.
    """
    if not pmid:
        return ""
    params = add_ncbi_api_key({"dbfrom": "pubmed", "db": "pmc", "id": pmid, "retmode": "json"})
    url = f"{NCBI_ELINK_BASE}?{urlencode(params)}"
    try:
        raw = fetch_url(url, timeout=15).decode("utf-8", errors="replace")
        data = json.loads(raw)
        for ls in data.get("linksets", []):
            for lsdb in ls.get("linksetdbs", []):
                if lsdb.get("dbto") == "pmc":
                    ids = lsdb.get("links", [])
                    if ids:
                        return str(ids[0])
    except Exception:
        pass
    return ""


def _pmc_graphic_urls(pmc_id: str, href: str) -> list[str]:
    raw = normalize_whitespace(href)
    if not raw:
        return []
    if raw.startswith("http://") or raw.startswith("https://"):
        return [raw]
    raw = raw.lstrip("./")
    if not raw:
        return []
    candidates: list[str] = []
    names = [raw]
    stem, ext = os.path.splitext(raw)
    if not ext:
        names.extend([f"{raw}.jpg", f"{raw}.jpeg", f"{raw}.png", f"{raw}.webp", f"{raw}.gif"])
    for name in names:
        for url in (
            f"https://pmc.ncbi.nlm.nih.gov/articles/instance/PMC{pmc_id}/bin/{name}",
            f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/bin/{name}",
        ):
            if url not in candidates:
                candidates.append(url)
    return candidates


def _download_pmc_xml_figures(pmc_id: str, identifier: str, figure_specs: list[dict], referer: str) -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR or not figure_specs:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    saved_items: list[dict] = []
    for pos, spec in enumerate(figure_specs[:6], start=1):
        href = normalize_whitespace(spec.get("href", ""))
        urls = _pmc_graphic_urls(pmc_id, href)
        if not urls:
            continue
        for url in urls:
            try:
                data = fetch_url(url, timeout=120, headers={"Referer": referer})
            except Exception:
                continue
            if len(data) < 15_000:
                continue
            ext = guess_image_extension(data, url)
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            target = (out_dir / safe_filename(f"{identifier}-pmc-figure-{pos}")).with_suffix(ext)
            try:
                target.write_bytes(data)
            except Exception:
                continue
            if target.stat().st_size < 15_000:
                target.unlink(missing_ok=True)
                continue
            if ext == ".png":
                width, height = read_png_size(target)
                if width and height and (width < 220 or height < 220):
                    target.unlink(missing_ok=True)
                    continue
            saved_paths.append(str(target))
            saved_items.append(
                {
                    "index": spec.get("index", pos),
                    "label": spec.get("label", ""),
                    "caption": spec.get("caption", ""),
                    "source": "pmc-xml",
                    "source_url": url,
                    "path": str(target),
                }
            )
            break
    return saved_paths, saved_items


def parse_pmc_xml(xml_text: str, pmc_id: str, pmc_url: str) -> dict:
    soup = BeautifulSoup(xml_text or "", "xml")
    if not soup.find():
        return {}

    article = soup.find("article") or soup
    title = clean_record_title(clean_structured_text((article.find("article-title") or article.find("title")).get_text(" ", strip=True) if (article.find("article-title") or article.find("title")) else ""))
    abstract_node = article.find("abstract")
    abstract = clean_structured_text(abstract_node.get_text(" ", strip=True)) if abstract_node else ""
    journal_node = article.find("journal-title")
    journal = clean_structured_text(journal_node.get_text(" ", strip=True)) if journal_node else ""

    doi = ""
    pmid = ""
    for node in article.find_all("article-id"):
        pub_id_type = normalize_whitespace(node.get("pub-id-type", "")).lower()
        value = clean_structured_text(node.get_text(" ", strip=True))
        if pub_id_type == "doi" and value and not doi:
            doi = parse_doi_candidate(value) or value
        if pub_id_type == "pmid" and value and not pmid:
            pmid = value

    authors: list[str] = []
    for contrib in article.find_all("contrib"):
        if normalize_whitespace(contrib.get("contrib-type", "")).lower() not in {"", "author"}:
            continue
        surname = clean_structured_text((contrib.find("surname").get_text(" ", strip=True) if contrib.find("surname") else ""))
        given = clean_structured_text((contrib.find("given-names").get_text(" ", strip=True) if contrib.find("given-names") else ""))
        collab = clean_structured_text((contrib.find("collab").get_text(" ", strip=True) if contrib.find("collab") else ""))
        name = normalize_whitespace(" ".join(part for part in [given, surname] if part)) or collab
        if name:
            authors.append(name)
    authors = clean_author_candidates(authors)

    affiliations = unique_keep_order(
        clean_structured_text(node.get_text(" ", strip=True))
        for node in article.find_all("aff")
        if clean_structured_text(node.get_text(" ", strip=True))
    )
    keywords = unique_keep_order(
        clean_structured_text(node.get_text(" ", strip=True))
        for node in article.find_all("kwd")
        if clean_structured_text(node.get_text(" ", strip=True))
    )

    body_node = article.find("body")
    body_parts: list[str] = []
    section_headers: list[str] = []
    if body_node is not None:
        for node in body_node.find_all(["sec", "title", "p"]):
            name = (node.name or "").lower()
            parent_names = {(parent.name or "").lower() for parent in node.parents}
            if parent_names & {"fig", "table-wrap", "ref-list", "ack", "supplementary-material"}:
                continue
            text = clean_structured_text(node.get_text(" ", strip=True))
            if not text:
                continue
            if name == "title":
                if len(text) >= 4:
                    section_headers.append(text)
                    body_parts.append(text)
            elif len(text) >= 20:
                body_parts.append(text)
    full_text = "\n\n".join(unique_keep_order(body_parts))[:45000]

    figure_specs: list[dict] = []
    for idx, fig in enumerate(article.find_all("fig"), start=1):
        label = clean_structured_text(fig.find("label").get_text(" ", strip=True) if fig.find("label") else "")
        caption_node = fig.find("caption")
        caption = clean_structured_text(caption_node.get_text(" ", strip=True)) if caption_node else ""
        graphic = fig.find(["graphic", "inline-graphic", "media", "supplementary-material"])
        href = ""
        if graphic is not None:
            href = clean_structured_text(
                graphic.get("xlink:href", "") or graphic.get("href", "") or graphic.get("src", "")
            )
        figure_specs.append(
            {
                "index": idx,
                "label": label,
                "caption": caption,
                "href": href,
                "source": "pmc-xml",
            }
        )

    figure_paths, figure_items = _download_pmc_xml_figures(
        pmc_id,
        title or f"PMC{pmc_id}",
        figure_specs,
        pmc_url,
    )
    if not figure_items:
        figure_items = [item for item in figure_specs if item.get("caption") or item.get("href")]

    return {
        "source_kind": "pmc_xml",
        "title": title,
        "authors": authors,
        "journal": journal,
        "pmid": pmid,
        "doi": doi,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": abstract,
        "affiliations": affiliations,
        "keywords": keywords,
        "full_text": full_text,
        "web_url": pmc_url,
        "summary_mode": "PMC XML 全文提取" if full_text else "PMC XML 元数据提取",
        "acquisition_path": f"PMC{pmc_id}",
        "section_headers": section_headers[:20],
        "figure_paths": figure_paths,
        "figure_items": figure_items[:8],
        "image_url": figure_items[0].get("source_url", "") if figure_items else "",
        "full_text_status": "fulltext" if full_text else "",
    }


def _fetch_pmc_xml_record(pmc_id: str) -> dict:
    if not pmc_id:
        return {}
    pmc_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/"
    params = add_ncbi_api_key({"db": "pmc", "id": f"PMC{pmc_id}", "retmode": "xml"})
    try:
        xml_text = fetch_url(f"{PUBMED_FETCH_BASE}?{urlencode(params)}", timeout=45).decode("utf-8", errors="replace")
    except Exception:
        return {}
    if "<article" not in xml_text.lower():
        return {}
    record = parse_pmc_xml(xml_text, pmc_id, pmc_url)
    if looks_like_full_article(record.get("title", ""), record.get("full_text", "")):
        return record
    return record if record.get("full_text") else {}


def _fetch_pmc_record(pmc_id: str) -> dict:
    """Fetch PMC full text, preferring XML and falling back to HTML.

    Strategy:
      1. NCBI EFetch PMC XML (structured JATS, best for sections/captions)
      2. Lightweight HTML via fetch_generic_web
      3. Playwright fallback (PMC can sometimes require JS for full rendering)

    Returns a record dict with full_text when successful, {} on failure.
    PMC content is open-access — no authentication or scraping risk.
    """
    if not pmc_id:
        return {}
    pmc_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/"
    try:
        record = _fetch_pmc_xml_record(pmc_id)
        if record.get("full_text"):
            record.setdefault("web_url", pmc_url)
            record.setdefault("summary_mode", "PMC XML 全文提取")
            return record
    except Exception:
        pass
    # Try lightweight extraction first
    try:
        record = fetch_generic_web(pmc_url)
        if looks_like_full_article(record.get("title", ""), record.get("full_text", "")):
            record["summary_mode"]    = "PMC 网页正文提取"
            record["acquisition_path"] = f"PMC{pmc_id}"
            record["web_url"]          = pmc_url
            return record
    except Exception:
        pass
    # Playwright fallback
    try:
        record = fetch_web_with_playwright(pmc_url, headed=PREFER_VISIBLE_BROWSER) or {}
        if record:
            record.setdefault("summary_mode",    "PMC 网页正文提取")
            record.setdefault("acquisition_path", f"PMC{pmc_id}")
            record.setdefault("web_url",          pmc_url)
        return record
    except Exception:
        return {}


def fetch_openalex_by_doi(doi: str) -> dict:
    doi = parse_doi_candidate(doi)
    if not doi:
        return {}
    url = f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}"
    try:
        raw = fetch_url(url, timeout=45).decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception:
        return {}

    title = clean_record_title(payload.get("display_name", "") or "")
    authors: list[str] = []
    for authorship in payload.get("authorships", []) or []:
        author = authorship.get("author", {}) if isinstance(authorship, dict) else {}
        name = normalize_whitespace(author.get("display_name", "") or "")
        if name:
            authors.append(name)
    journal = ""
    if isinstance(payload.get("primary_location"), dict):
        source = payload["primary_location"].get("source", {})
        if isinstance(source, dict):
            journal = normalize_whitespace(source.get("display_name", "") or "")
    if not journal and isinstance(payload.get("host_venue"), dict):
        journal = normalize_whitespace(payload["host_venue"].get("display_name", "") or "")
    year = str(payload.get("publication_year") or "")
    abstract = ""
    inv = payload.get("abstract_inverted_index") or {}
    if isinstance(inv, dict):
        positions = []
        for word, idxs in inv.items():
            if isinstance(idxs, list):
                for pos in idxs:
                    try:
                        positions.append((int(pos), word))
                    except Exception:
                        continue
        tokens = [word for _, word in sorted(positions, key=lambda item: item[0])]
        abstract = normalize_whitespace(" ".join(tokens))

    return {
        "source_kind": "openalex",
        "title": title,
        "authors": clean_author_candidates(authors),
        "journal": journal,
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}",
        "abstract": abstract,
        "affiliations": [],
        "keywords": [],
        "full_text": "",
        "summary_mode": "基于 OpenAlex 开放获取元数据",
        "image_url": "",
        "year": year,
    }


def fetch_crossref_by_doi(doi: str) -> dict:
    doi = parse_doi_candidate(doi)
    if not doi:
        return {}
    url = f"https://api.crossref.org/works/{quote(doi, safe='/')}"
    try:
        raw = fetch_url(url, timeout=45).decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception:
        return {}

    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    if not isinstance(message, dict):
        return {}

    title_values = message.get("title") or []
    title = clean_record_title(title_values[0] if isinstance(title_values, list) and title_values else str(title_values or ""))

    authors: list[str] = []
    for item in message.get("author", []) or []:
        if not isinstance(item, dict):
            continue
        given = normalize_whitespace(str(item.get("given", "") or ""))
        family = normalize_whitespace(str(item.get("family", "") or ""))
        name = normalize_whitespace(" ".join(part for part in [given, family] if part))
        if name:
            authors.append(name)

    journal_values = message.get("container-title") or []
    journal = journal_values[0] if isinstance(journal_values, list) and journal_values else str(journal_values or "")
    journal = normalize_whitespace(journal)

    year = ""
    for field in ["published-print", "published-online", "issued", "created"]:
        candidate = message.get(field) or {}
        if isinstance(candidate, dict):
            parts = candidate.get("date-parts") or []
            if parts and isinstance(parts[0], list) and parts[0]:
                year = str(parts[0][0])
                if year:
                    break

    abstract = normalize_whitespace(re.sub(r"<[^>]+>", " ", str(message.get("abstract", "") or "")))
    web_url = normalize_whitespace(str(message.get("URL", "") or ""))

    return {
        "source_kind": "crossref",
        "title": title,
        "authors": clean_author_candidates(authors),
        "journal": journal,
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}",
        "web_url": web_url,
        "abstract": abstract,
        "affiliations": [],
        "keywords": [],
        "full_text": "",
        "summary_mode": "基于 Crossref DOI 元数据",
        "image_url": "",
        "year": year,
    }


def fetch_pubmed_by_doi(doi: str) -> dict:
    doi = parse_doi_candidate(doi)
    if not doi:
        return {}
    params = add_ncbi_api_key({
        "db": "pubmed",
        "term": f'"{doi}"[AID]',
        "retmode": "json",
        "retmax": "3",
    })
    try:
        raw = fetch_url(f"{PUBMED_SEARCH_BASE}?{urlencode(params)}").decode("utf-8", errors="replace")
        payload = json.loads(raw)
        ids = payload.get("esearchresult", {}).get("idlist", []) if isinstance(payload, dict) else []
        if not ids:
            return {}
        return fetch_pubmed_by_pmid(ids[0])
    except Exception:
        return {}


def parse_html_metadata(url: str, html_text: str) -> dict:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    page_title = html.unescape(normalize_whitespace(title_match.group(1))) if title_match else ""

    meta = {}
    for match in re.finditer(
        r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
        html_text,
        re.IGNORECASE,
    ):
        meta[match.group(1).lower()] = html.unescape(normalize_whitespace(match.group(2)))

    citation_authors = [
        html.unescape(normalize_whitespace(match.group(1)))
        for match in re.finditer(
            r'<meta[^>]+name=["\']citation_author["\'][^>]+content=["\']([^"\']*)["\']',
            html_text,
            re.IGNORECASE,
        )
        if normalize_whitespace(match.group(1))
    ]

    body_text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    body_text = normalize_whitespace(body_text)
    main_title, main_text = extract_main_text_from_html(url, html_text, fallback_body=body_text)
    doi = meta.get("citation_doi", "") or meta.get("dc.identifier", "")
    doi = parse_doi_candidate(doi.replace("doi:", "").strip()) if doi else ""

    pdf_url = ""
    pdf_match = re.search(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html_text, re.IGNORECASE)
    if pdf_match:
        pdf_url = urljoin(url, html.unescape(pdf_match.group(1)))

    image_url = meta.get("og:image", "") or meta.get("twitter:image", "")
    if image_url:
        image_url = urljoin(url, image_url)
    title = clean_record_title(meta.get("citation_title", "") or meta.get("og:title", "") or main_title or page_title)
    figure_candidates = collect_html_figure_candidates(html_text, url)
    figure_paths, figure_items = save_html_figure_urls(figure_candidates, title or identifier_from_url(url))
    full_text = main_text or body_text[:45000]
    if figure_items and not image_url:
        image_url = figure_items[0].get("src", "")

    return {
        "source_kind": "web_url",
        "title": title,
        "authors": citation_authors or ([meta["citation_author"]] if meta.get("citation_author") else []),
        "journal": meta.get("citation_journal_title", "") or meta.get("og:site_name", ""),
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": meta.get("description", "") or meta.get("og:description", ""),
        "affiliations": [],
        "keywords": [item.strip() for item in (meta.get("keywords", "") or "").split(",") if item.strip()],
        "full_text": full_text,
        "pdf_url": pdf_url,
        "web_url": url,
        "summary_mode": "基于网页内容/元数据",
        "image_url": image_url,
        "figure_paths": figure_paths,
        "figure_items": figure_items,
        "summary_mode": "基于全文/网页正文" if looks_like_full_article(title, full_text) else "基于网页内容/元数据",
    }


def fetch_generic_web(url: str) -> dict:
    raw = fetch_url(url).decode("utf-8", errors="replace")
    return parse_html_metadata_v2(url, raw)


def parse_html_metadata_v2(url: str, html_text: str) -> dict:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    page_title = html.unescape(normalize_whitespace(title_match.group(1))) if title_match else ""

    meta = {}
    for match in re.finditer(
        r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
        html_text,
        re.IGNORECASE,
    ):
        meta[match.group(1).lower()] = html.unescape(normalize_whitespace(match.group(2)))

    citation_authors = [
        html.unescape(normalize_whitespace(match.group(1)))
        for match in re.finditer(
            r'<meta[^>]+name=["\']citation_author["\'][^>]+content=["\']([^"\']*)["\']',
            html_text,
            re.IGNORECASE,
        )
        if normalize_whitespace(match.group(1))
    ]

    jsonld_authors: list[str] = []
    jsonld_keywords: list[str] = []
    jsonld_journal = ""
    jsonld_abstract = ""
    jsonld_doi = ""
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        raw = html.unescape(match.group(1) or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            author_field = node.get("author")
            author_nodes = author_field if isinstance(author_field, list) else ([author_field] if author_field else [])
            for author_node in author_nodes:
                if isinstance(author_node, dict) and author_node.get("name"):
                    jsonld_authors.append(str(author_node["name"]))
                elif isinstance(author_node, str):
                    jsonld_authors.append(author_node)
            if not jsonld_journal:
                is_part = node.get("isPartOf")
                if isinstance(is_part, dict) and is_part.get("name"):
                    jsonld_journal = str(is_part["name"])
                elif isinstance(node.get("publisher"), dict) and node["publisher"].get("name"):
                    jsonld_journal = str(node["publisher"]["name"])
            if not jsonld_abstract and node.get("description"):
                jsonld_abstract = normalize_whitespace(str(node["description"]))
            kw = node.get("keywords")
            if kw:
                if isinstance(kw, list):
                    jsonld_keywords.extend(str(x) for x in kw)
                else:
                    jsonld_keywords.extend(part.strip() for part in str(kw).split(","))
            for key in ("identifier", "doi"):
                value = node.get(key)
                candidate = ""
                if isinstance(value, str):
                    candidate = parse_doi_candidate(value.replace("doi:", "").strip())
                elif isinstance(value, dict):
                    candidate = parse_doi_candidate(str(value.get("value", "")).replace("doi:", "").strip())
                if candidate and not jsonld_doi:
                    jsonld_doi = candidate

    body_text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    body_text = truncate_article_shell_noise(normalize_whitespace(body_text))
    main_title, main_text = extract_main_text_from_html(url, html_text, fallback_body=body_text)
    main_quality = len(normalize_whitespace(main_text)) + 4000 * count_article_section_hits(main_text)
    body_quality = len(normalize_whitespace(body_text)) + 4000 * count_article_section_hits(body_text)
    full_text = (main_text if main_quality >= body_quality else body_text)[:45000]

    doi = (
        meta.get("citation_doi", "")
        or meta.get("dc.identifier", "")
        or meta.get("dc.identifier.doi", "")
        or meta.get("prism.doi", "")
        or jsonld_doi
        or find_doi_in_text(" ".join([url, page_title, body_text[:8000]]))
    )
    doi = parse_doi_candidate(str(doi).replace("doi:", "").strip()) if doi else ""

    pdf_url = ""
    pdf_match = re.search(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html_text, re.IGNORECASE)
    if pdf_match:
        pdf_url = urljoin(url, html.unescape(pdf_match.group(1)))

    image_url = meta.get("og:image", "") or meta.get("twitter:image", "")
    if image_url:
        image_url = urljoin(url, image_url)
    title = clean_record_title(
        meta.get("citation_title", "")
        or meta.get("dc.title", "")
        or meta.get("twitter:title", "")
        or meta.get("og:title", "")
        or main_title
        or page_title
    )
    figure_candidates = collect_html_figure_candidates(html_text, url)
    figure_paths, figure_items = save_html_figure_urls(figure_candidates, title or identifier_from_url(url))
    if figure_items and not image_url:
        image_url = figure_items[0].get("src", "")

    authors = clean_author_candidates(
        citation_authors
        or ([meta["citation_author"]] if meta.get("citation_author") else [])
        or jsonld_authors
    )
    journal = (
        meta.get("citation_journal_title", "")
        or meta.get("prism.publicationname", "")
        or meta.get("og:site_name", "")
        or jsonld_journal
    )
    abstract = (
        meta.get("citation_abstract", "")
        or meta.get("description", "")
        or meta.get("og:description", "")
        or jsonld_abstract
    )
    keywords = unique_keep_order(
        [item.strip() for item in (meta.get("keywords", "") or "").split(",") if item.strip()] + jsonld_keywords
    )

    return {
        "source_kind": "web_url",
        "title": title,
        "authors": authors,
        "journal": journal,
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": abstract,
        "affiliations": [],
        "keywords": keywords,
        "full_text": full_text,
        "pdf_url": pdf_url,
        "web_url": url,
        "image_url": image_url,
        "figure_paths": figure_paths,
        "figure_items": figure_items,
        "summary_mode": "基于全文/网页正文" if looks_like_full_article(title, full_text) else "基于网页内容/元数据",
    }


def publisher_page_blocked(title: str, body_text: str) -> bool:
    hay = normalize_whitespace(f"{title} {body_text[:4000]}").lower()
    markers = [
        "just a moment",
        "请稍候",
        "checking your browser",
        "enable javascript and cookies",
        "attention required",
        "cloudflare",
    ]
    return any(marker in hay for marker in markers)


def page_is_cookie_wall(title: str, body_text: str) -> bool:
    hay = normalize_whitespace(f"{title} {body_text[:6000]}").lower()
    cookie_markers = [
        "your privacy, your choice",
        "accept all cookies",
        "manage cookies",
        "privacy policy",
        "cookie policy",
    ]
    article_markers = ["abstract", "introduction", "results", "discussion", "references"]
    return any(marker in hay for marker in cookie_markers) and not any(marker in hay for marker in article_markers)


def count_article_section_hits(text: str) -> int:
    hay = (text or "").lower()
    markers = [
        "abstract",
        "introduction",
        "background",
        "materials and methods",
        "methods",
        "results",
        "discussion",
        "conclusion",
        "references",
        "supplementary",
    ]
    hits = 0
    for marker in markers:
        pattern = rf"(?<![a-z]){re.escape(marker)}(?![a-z]|:)"
        if re.search(pattern, hay):
            hits += 1
    return hits


def looks_like_full_article(title: str, body_text: str) -> bool:
    if not body_text:
        return False
    if publisher_page_blocked(title, body_text) or page_is_cookie_wall(title, body_text):
        return False
    lowered_title = clean_record_title(title).lower()
    if not lowered_title:
        return False
    if lowered_title in {"redirecting", "untitled", "please wait", "请稍候…", "请稍候"}:
        return False
    section_hits = count_article_section_hits(body_text)
    if section_hits >= 2:
        return True
    clean = normalize_whitespace(body_text)
    if section_hits >= 1 and len(clean) >= 4500:
        return True
    if len(clean) >= 6000 and any(token in clean.lower() for token in ["sample", "sequencing", "analysis", "genome", "species", "experiment", "review", "evolution"]):
        return True
    return False


def browser_record_has_fulltext(record: dict) -> bool:
    if not record:
        return False
    full_text = record.get("full_text", "") or ""
    if summary_mode_indicates_fulltext(record) and full_text:
        return True
    return looks_like_full_article(record.get("title", ""), full_text)


def snapshot_quality(candidate: dict) -> int:
    if not candidate:
        return -1
    title = clean_record_title(candidate.get("title", ""))
    body = candidate.get("body", "") or ""
    figures = candidate.get("figures") or []
    headings = candidate.get("headings") or []
    score = len(normalize_whitespace(body))
    score += 5000 * min(len(figures), 4)
    score += 500 * min(len(headings), 10)
    if title:
        score += 2000
    if looks_like_full_article(title, body):
        score += 100000
    return score


def pdf_text_quality(text: str) -> int:
    clean = normalize_whitespace(text or "")
    if not clean:
        return -1
    if page_is_cookie_wall("", clean) or publisher_page_blocked("", clean):
        return -1
    return len(clean) + 4000 * count_article_section_hits(clean)


def extract_pubmed_full_text_links(html_text: str) -> list[str]:
    links = []
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*linksrc=fulltextorjournal_fulltext[^>]*>', html_text, re.IGNORECASE):
        href = html.unescape(match.group(1))
        links.append(href)
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*title="See full text options[^"]*"[^>]*>', html_text, re.IGNORECASE):
        href = html.unescape(match.group(1))
        links.append(href)
    deduped = []
    for href in links:
        absolute = urljoin("https://pubmed.ncbi.nlm.nih.gov/", href)
        if absolute not in deduped:
            deduped.append(absolute)
    return deduped


def fetch_pubmed_page_metadata(pmid: str) -> dict:
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    try:
        raw = fetch_url(url).decode("utf-8", errors="replace")
    except Exception:
        return {}
    links = extract_pubmed_full_text_links(raw)
    return {
        "pubmed_url": url,
        "full_text_links": links,
        "full_text_status": "PubMed 页面提供全文链接" if links else "",
    }


def parse_playwright_result(raw: str) -> str:
    match = re.search(r"### Result\s*(.*?)\s*### Ran Playwright code", raw, re.DOTALL)
    return match.group(1).strip() if match else ""


def normalized_pdf_candidates(candidates: list[str], current_url: str = "") -> list[str]:
    seen: list[str] = []
    for candidate in candidates or []:
        url = normalize_whitespace(candidate)
        if not url:
            continue
        url = urljoin(current_url, url)
        variants = [url]
        if "/doi/epdf/" in url:
            variants.insert(0, url.replace("/doi/epdf/", "/doi/pdfdirect/") + ("&download=true" if "?" in url else "?download=true"))
            variants.insert(1, url.replace("/doi/epdf/", "/doi/pdf/"))
            variants.append(url.replace("/doi/epdf/", "/doi/pdfdirect/"))
        if "/doi/pdfdirect/" in url:
            variants.insert(0, url if "download=true" in url else (url + ("&download=true" if "?" in url else "?download=true")))
            variants.append(url.replace("/doi/pdfdirect/", "/doi/pdf/"))
        if "/doi/pdf/" in url:
            variants.insert(0, url.replace("/doi/pdf/", "/doi/pdfdirect/") + ("&download=true" if "?" in url else "?download=true"))
            variants.append(url.replace("/doi/pdf/", "/doi/epdf/"))
        for variant in variants:
            if variant and variant not in seen:
                seen.append(variant)
    seen.sort(
        key=lambda url: (
            0 if re.search(r'\.full\.pdf(?:"|$)', url, re.IGNORECASE) else
            1 if "/doi/pdfdirect/" in url else
            2 if "/doi/pdf/" in url else
            3 if re.search(r'\.pdf(?:"|$)', url, re.IGNORECASE) and ".pdf+html" not in url.lower() else
            4 if "/doi/epdf/" in url else
            5
        )
    )
    return [url for url in seen if ".pdf+html" not in url.lower()]


def collect_page_dom_metadata(page, current_url: str) -> dict:
    try:
        payload = page.evaluate(
            """() => {
                const norm = (x) => (x || '').replace(/\\s+/g, ' ').trim();
                const qmeta = (...names) => {
                  for (const name of names) {
                    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
                    const val = norm(el ? el.getAttribute('content') : '');
                    if (val) return val;
                  }
                  return '';
                };
                const uniq = (items) => Array.from(new Set((items || []).map(norm).filter(Boolean)));
                const bodyText = norm(document.body ? document.body.innerText : '');
                const authors = [];
                document.querySelectorAll('meta[name="citation_author"]').forEach(el => authors.push(el.getAttribute('content') || ''));
                document.querySelectorAll('a[rel="author"], [class*="author"] a, [data-test*="author"] a, [data-testid*="author"] a').forEach(el => {
                  const txt = norm(el.innerText || el.textContent || '');
                  if (txt && txt.length <= 120) authors.push(txt);
                });
                let doi = qmeta('citation_doi', 'dc.identifier', 'dc.identifier.doi', 'prism.doi');
                if (!doi) {
                  const doiLink = document.querySelector('a[href*="doi.org/10."]');
                  if (doiLink) {
                    const href = doiLink.getAttribute('href') || '';
                    const m = href.match(/10\\.\\d{4,9}\\/[-._;()/:A-Z0-9]+/i);
                    if (m) doi = m[0];
                  }
                }
                if (!doi) {
                  const m = bodyText.match(/10\\.\\d{4,9}\\/[-._;()/:A-Z0-9]+/i);
                  if (m) doi = m[0];
                }
                const kw = qmeta('keywords');
                const pdfCandidates = [];
                const metaPdf = qmeta('citation_pdf_url');
                if (metaPdf) pdfCandidates.push(metaPdf);
                document.querySelectorAll('a[href], area[href]').forEach(el => {
                  const href = el.href || el.getAttribute('href') || '';
                  const txt = norm(el.innerText || el.textContent || '').toLowerCase();
                  if (!href) return;
                  if (/\\.pdf(\\"|$)/i.test(href) || /\\/doi\\/(pdf|pdfdirect|epdf)\\//i.test(href) || txt === 'pdf' || txt.includes('download pdf')) {
                    pdfCandidates.push(href);
                  }
                });
                let imageUrl = qmeta('og:image', 'twitter:image');
                if (!imageUrl) {
                  const lead = document.querySelector('figure img, article img, main img');
                  imageUrl = lead ? (lead.currentSrc || lead.src || '') : '';
                }
                return {
                  title: qmeta('citation_title', 'dc.title', 'twitter:title', 'og:title') || norm(document.title),
                  authors: uniq(authors),
                  journal: qmeta('citation_journal_title', 'prism.publicationname', 'og:site_name'),
                  doi: norm((doi || '').replace(/^doi:\\s*/i, '')),
                  abstract: qmeta('citation_abstract', 'description', 'og:description'),
                  keywords: uniq((kw || '').split(',').map(x => x.trim())),
                  pdf_url: uniq(pdfCandidates).find(x => x && !x.toLowerCase().includes('.pdf+html')) || uniq(pdfCandidates)[0] || '',
                  image_url: norm(imageUrl),
                  web_url: location.href,
                };
            }"""
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def fetch_pdf_with_python_playwright_context(context, pdf_candidates: list[str], title_hint: str) -> dict:
    if not pdf_candidates:
        return {}
    download_page = None
    try:
        for candidate in pdf_candidates:
            download_page = context.new_page()
            try:
                with download_page.expect_download(timeout=25000) as dl_info:
                    download_page.goto(candidate, wait_until="domcontentloaded", timeout=120000)
                download = dl_info.value
                temp_path = Path(tempfile.mkdtemp(prefix="py_pw_pdf_")) / safe_filename(title_hint or "publisher") 
                temp_pdf = temp_path.with_suffix(".pdf")
                download.save_as(str(temp_pdf))
                if temp_pdf.exists() and looks_like_pdf_bytes(temp_pdf.read_bytes()):
                    saved_path = save_pdf_bytes(temp_pdf.read_bytes(), "publisher", title_hint or "publisher")
                    parse_path = Path(saved_path) if saved_path else temp_pdf
                    record = parse_local_pdf_file(parse_path, merge_doi=False)
                    if saved_path:
                        record["downloaded_pdf"] = saved_path
                    record["pdf_url"] = candidate
                    return record
            except Exception:
                try:
                    resp = download_page.goto(candidate, wait_until="commit", timeout=120000)
                    ctype = (resp.header_value("content-type") or "").lower() if resp else ""
                    body = resp.body() if resp else b""
                    if ("pdf" in ctype) or looks_like_pdf_bytes(body):
                        saved_path = save_pdf_bytes(body, "publisher", title_hint or "publisher")
                        parse_path = Path(saved_path) if saved_path else None
                        if parse_path and parse_path.exists():
                            record = parse_local_pdf_file(parse_path, merge_doi=False)
                            record["downloaded_pdf"] = str(parse_path)
                            record["pdf_url"] = candidate
                            return record
                except Exception:
                    pass
            finally:
                try:
                    download_page.close()
                except Exception:
                    pass
                download_page = None
    except Exception:
        pass
    finally:
        if download_page is not None:
            try:
                download_page.close()
            except Exception:
                pass
    return {}


def download_pdf_to_dir(url: str, identifier: str, title: str) -> str:
    if not PDF_SAVE_DIR or not url:
        return ""
    try:
        PDF_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        data = fetch_url(url, timeout=120, headers={"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"})
        if not looks_like_pdf_bytes(data):
            return ""
        filename = safe_filename(f"{identifier} - {title or 'paper'}") + ".pdf"
        out_path = PDF_SAVE_DIR / filename
        out_path.write_bytes(data)
        return str(out_path)
    except Exception:
        return ""


def save_pdf_bytes(data: bytes, identifier: str, title: str) -> str:
    if not PDF_SAVE_DIR or not data:
        return ""
    try:
        PDF_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        filename = safe_filename(f"{identifier} - {title or 'paper'}") + ".pdf"
        out_path = PDF_SAVE_DIR / filename
        out_path.write_bytes(data)
        return str(out_path)
    except Exception:
        return ""


def looks_like_pdf_bytes(data: bytes) -> bool:
    head = (data or b"")[:1024]
    return b"%PDF" in head


def extract_pdf_images(path: Path, identifier: str) -> list[str]:
    pdfimages = find_tool("pdfimages.exe") or find_tool("pdfimages")
    out_dir = PDF_SAVE_DIR / "figures" if PDF_SAVE_DIR else None
    if not pdfimages or not out_dir:
        return []
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = out_dir / safe_filename(identifier)
        result = subprocess.run(
            [pdfimages, "-png", str(path), str(prefix)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode != 0:
            return []
        scored = []
        for img in sorted(out_dir.glob(prefix.name + "-*.png")):
            try:
                size = img.stat().st_size
                if size < 10_000:
                    continue
                width, height = read_png_size(img)
                if width and height:
                    if width < 120 or height < 120:
                        continue
                    score = size + (width * height)
                else:
                    score = size
                scored.append((score, str(img)))
            except Exception:
                continue
        scored.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in scored[:4]]
    except Exception:
        return []


def extract_fig1_pymupdf(path: Path, identifier: str) -> list[str]:
    """Extract Figure 1 from a PDF using PyMuPDF (fitz).

    Strategy:
    1. Scan pages for a "Fig. 1" / "Figure 1" caption text block.
    2. Clip the region that includes the image block(s) above the caption.
    3. Render at 3× scale (≈ 225 DPI) and save as PNG.
    Fallback: first large embedded raster image from the whole document.
    Returns a list with at most 1 file path, or [] on any failure.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    out_dir = PDF_SAVE_DIR / "figures" if PDF_SAVE_DIR else None
    if not out_dir:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_filename(identifier)}-fig1.png"

    CAP_PAT = re.compile(r"(?i)^fig(?:ure)?\.?\s*1[\.\s\)\,]")

    try:
        doc = fitz.open(str(path))
    except Exception:
        return []

    try:
        for pn, pg in enumerate(doc):
            pg_rect = pg.rect
            blk_dict = pg.get_text("dict")["blocks"]

            caption_rect = None
            image_rects: list = []

            for blk in blk_dict:
                if blk["type"] == 0:  # text block
                    full = " ".join(
                        sp["text"]
                        for ln in blk.get("lines", [])
                        for sp in ln.get("spans", [])
                    ).strip()
                    if CAP_PAT.match(full) and caption_rect is None:
                        caption_rect = fitz.Rect(blk["bbox"])
                elif blk["type"] == 1:  # image block
                    r = fitz.Rect(blk["bbox"])
                    if r.width > 80 and r.height > 80:
                        image_rects.append(r)

            if caption_rect is None:
                continue

            # Image blocks strictly above the caption (with 30 pt tolerance)
            fig_rects = [
                r for r in image_rects
                if r.y1 <= caption_rect.y0 + 30 and r.y0 < caption_rect.y0
            ]

            if fig_rects:
                combined = fig_rects[0]
                for r in fig_rects[1:]:
                    combined = combined | r
                combined = combined | caption_rect
                clip = fitz.Rect(
                    max(0, combined.x0 - 8),
                    max(0, combined.y0 - 8),
                    min(pg_rect.x1, combined.x1 + 8),
                    min(pg_rect.y1, combined.y1 + 50),
                )
            else:
                # Vector figure — clip the region above the caption text
                fig_height = min(caption_rect.y0, 500)
                clip = fitz.Rect(
                    max(0, caption_rect.x0 - 20),
                    max(0, caption_rect.y0 - fig_height),
                    min(pg_rect.x1, caption_rect.x1 + 20),
                    min(pg_rect.y1, caption_rect.y1 + 60),
                )

            mat = fitz.Matrix(3.0, 3.0)
            pix = pg.get_pixmap(matrix=mat, clip=clip, alpha=False)
            if pix.width > 120 and pix.height > 120:
                pix.save(str(out_path))
                return [str(out_path)]

        # Fallback: first large embedded raster image anywhere in the document
        for pg in doc:
            for img_info in pg.get_images(full=True):
                xref = img_info[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.width >= 300 and pix.height >= 200:
                        if pix.n > 4:  # CMYK → convert to RGB
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        pix.save(str(out_path))
                        return [str(out_path)]
                except Exception:
                    continue
    except Exception:
        pass
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return []


def read_png_size(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as fh:
            data = fh.read(24)
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except Exception:
        pass
    return 0, 0


def truncate_article_shell_noise(text: str) -> str:
    clean = text or ""
    if not clean:
        return ""
    markers = [
        "Additional links",
        "About Wiley Online Library",
        "Wiley Home Page",
        "Log in to Wiley Online Library",
        "Create a new account",
        "Forgot your password",
        "Request Username",
        "Export citation",
        "Download Citation",
        "Article Metrics",
        "Scite metrics",
        "Share QR Code",
        "Generating QR code",
        "Close Figure Viewer",
        "Privacy Policy",
        "Terms of Use",
        "Manage Cookies",
    ]
    cut = len(clean)
    for marker in markers:
        idx = clean.find(marker)
        if idx != -1 and idx > 3000:
            cut = min(cut, idx)
    clean = clean[:cut]
    clean = re.sub(r"(Search for more papers by this author\s+){2,}", "Search for more papers by this author ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(PDF\s+About\s+Sections\s+PDF\s+CITE\s+Tools\s+Request permission\s+Add to favorites\s+Track citation\s+ShareShare\s+Give access)\b", "", clean, flags=re.IGNORECASE)
    return normalize_whitespace(clean)


def extract_main_text_from_html(url: str, html_text: str, fallback_body: str = "") -> tuple[str, str]:
    title = ""
    clean_text = ""
    try:
        doc = Document(html_text)
        title = normalize_whitespace(doc.short_title() or doc.title() or "")
        summary_html = doc.summary(html_partial=True)
        extracted = trafilatura.extract(
            summary_html or html_text,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            deduplicate=True,
        )
        clean_text = truncate_article_shell_noise(normalize_whitespace(extracted or ""))
        if not clean_text and summary_html:
            summary_soup = BeautifulSoup(summary_html, "lxml")
            clean_text = truncate_article_shell_noise(normalize_whitespace(summary_soup.get_text(" ", strip=True)))
    except Exception:
        clean_text = ""

    if not clean_text:
        try:
            extracted = trafilatura.extract(
                html_text,
                url=url,
                output_format="txt",
                include_comments=False,
                include_tables=True,
                favor_precision=True,
                deduplicate=True,
            )
            clean_text = truncate_article_shell_noise(normalize_whitespace(extracted or ""))
        except Exception:
            clean_text = ""

    if not title:
        soup = BeautifulSoup(html_text, "lxml")
        title = normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
    if not clean_text:
        clean_text = truncate_article_shell_noise(normalize_whitespace(fallback_body))
    return title, clean_text[:45000]


def collect_html_figure_candidates(html_text: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    candidates = []
    seen = set()

    def pick_src(tag) -> str:
        for attr in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-img-src",
            "data-full-size-image-src",
            "data-srcset",
            "srcset",
        ):
            raw = (tag.get(attr) or "").strip()
            if not raw:
                continue
            if attr.endswith("srcset"):
                first = raw.split(",")[0].strip()
                if first:
                    return first.split()[0]
                continue
            return raw
        return ""

    figures = soup.select("figure")
    for idx, fig in enumerate(figures):
        img = fig.find("img")
        if not img:
            img = fig.find("source")
        if not img:
            continue
        src = pick_src(img)
        if not src:
            continue
        src = urljoin(base_url, src)
        caption_node = fig.find("figcaption")
        caption = normalize_whitespace(caption_node.get_text(" ", strip=True) if caption_node else "")
        key = (src, caption)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "index": idx,
                "href": urljoin(base_url, (fig.find("a", href=True).get("href") if fig.find("a", href=True) else "").strip()) if fig.find("a", href=True) else "",
                "src": src,
                "caption": caption,
                "alt": normalize_whitespace(img.get("alt", "")),
                "source": "figure",
            }
        )
    return candidates


def filter_image_candidates(items: list[dict], base_url: str) -> list[dict]:
    filtered = []
    seen = set()

    def pick_src(item: dict) -> str:
        for attr in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-img-src",
            "data-full-size-image-src",
            "data-srcset",
            "srcset",
        ):
            raw = normalize_whitespace(item.get(attr, "") or "")
            if not raw:
                continue
            if attr.endswith("srcset"):
                first = raw.split(",")[0].strip()
                if first:
                    return first.split()[0]
                continue
            return raw
        return ""

    for idx, item in enumerate(items):
        src = urljoin(base_url, pick_src(item))
        if not src:
            continue
        src_lower = src.lower()
        alt = normalize_whitespace(item.get("alt", ""))
        caption = normalize_whitespace(item.get("caption", ""))
        width = int(item.get("width", 0) or 0)
        height = int(item.get("height", 0) or 0)
        is_probable_figure = any(
            token in src_lower
            for token in ["/fig", "_fig", "-fig", "figure", "mediaobjects"]
        )
        is_noise = any(
            token in src_lower
            for token in ["logo", "banner", "spinner", "icon", "altmetric", "bing.com", "cloudfront.net/v1/", "bio-"]
        )
        if is_noise:
            continue
        if not is_probable_figure and width < 250 and height < 250:
            continue
        key = (src, alt, caption)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(
            {
                "index": int(item.get("index", idx) or idx),
                "href": urljoin(base_url, (item.get("href") or "").strip()) if item.get("href") else "",
                "src": src,
                "alt": alt,
                "caption": caption,
                "width": width,
                "height": height,
                "source": "image",
            }
        )
    filtered.sort(key=lambda item: (bool(re.search(r"(^|[-_/])fig", item["src"].lower())), item["width"] * item["height"]), reverse=True)
    return filtered[:6]


def save_html_figure_urls(figure_candidates: list[dict], identifier: str, referer: str = "") -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR or not figure_candidates:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    saved_items: list[dict] = []
    for pos, item in enumerate(figure_candidates[:4], start=1):
        srcs = [item.get("href", ""), item.get("src", "")]
        preferred_sources = []
        for candidate_src in srcs:
            candidate_src = candidate_src or ""
            if candidate_src and candidate_src not in preferred_sources:
                preferred_sources.append(candidate_src)
        if not preferred_sources:
            continue
        for src in preferred_sources:
            ext = Path(urlparse(src).path).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                ext = ".png"
            target = out_dir / safe_filename(f"{identifier}-html-figure-{pos}")
            target = target.with_suffix(ext)
            try:
                data = fetch_url(src, timeout=120, headers={"Referer": referer})
                if len(data) < 15_000:
                    continue
                target.write_bytes(data)
                width, height = read_png_size(target)
                if width and height and (width < 220 or height < 220):
                    target.unlink(missing_ok=True)
                    continue
                saved_paths.append(str(target))
                saved_items.append({**item, "path": str(target), "source_url": src})
                break
            except Exception:
                continue
    return saved_paths, saved_items


def save_html_figure_urls_with_playwright_context(base: list[str], figure_candidates: list[dict], identifier: str) -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR or not figure_candidates:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="playwright_fig_ctx_"))
    saved_paths: list[str] = []
    saved_items: list[dict] = []
    try:
        for pos, item in enumerate(figure_candidates[:4], start=1):
            source_candidates = []
            for candidate_src in [item.get("href", ""), item.get("src", "")]:
                candidate_src = normalize_whitespace(candidate_src)
                if candidate_src and candidate_src not in source_candidates:
                    source_candidates.append(candidate_src)
            if not source_candidates:
                continue
            for src in source_candidates:
                ext = ".png"
                temp_target = (temp_dir / safe_filename(f"{identifier}-html-figure-{pos}")).with_suffix(ext)
                final_target = (out_dir / safe_filename(f"{identifier}-html-figure-{pos}")).with_suffix(ext)
                js = (
                    f"const url = {json.dumps(src)};"
                    f"const target = {json.dumps(temp_target.as_posix())};"
                    "const p = await page.context().newPage();"
                    "try {"
                    "  const resp = await p.goto(url, { waitUntil: 'commit', timeout: 120000 });"
                    "  const body = resp ? await resp.body() : Buffer.from('');"
                    "  const ct = ((resp && resp.headers()['content-type']) || '').toLowerCase();"
                    "  if (resp && resp.status() === 200 && ct.startsWith('image/') && body.length > 15000) {"
                    "    await p.screenshot({ path: target, fullPage: true }).catch(() => {});"
                    "    if (!body.length) {"
                    "      await p.screenshot({ path: target, fullPage: true }).catch(() => {});"
                    "    }"
                    "    await p.close().catch(() => {});"
                    "    return JSON.stringify({ ok: true, url, status: resp.status(), len: body.length, contentType: ct });"
                    "  }"
                    "  await p.close().catch(() => {});"
                    "  return JSON.stringify({ ok: false, url, status: resp ? resp.status() : 0, len: body.length, contentType: ct });"
                    "} catch (e) {"
                    "  await p.close().catch(() => {});"
                    "  return JSON.stringify({ ok: false, url, error: String(e) });"
                    "}"
                )
                try:
                    result = subprocess.run(
                        base + ["run-code", js],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=120,
                    )
                    if result.returncode != 0 or not temp_target.exists():
                        continue
                    width, height = read_png_size(temp_target)
                    if width and height and (width < 220 or height < 220):
                        temp_target.unlink(missing_ok=True)
                        continue
                    if temp_target.stat().st_size < 15_000:
                        temp_target.unlink(missing_ok=True)
                        continue
                    shutil.copy2(temp_target, final_target)
                    saved_paths.append(str(final_target))
                    saved_items.append({**item, "path": str(final_target), "source_url": src})
                    break
                except Exception:
                    continue
        return saved_paths, saved_items
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def capture_playwright_figures(base: list[str], identifier: str, figure_candidates: list[dict]) -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR or not figure_candidates:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="playwright_figshot_"))
    saved_paths: list[str] = []
    saved_items: list[dict] = []
    try:
        for pos, item in enumerate(figure_candidates[:4], start=1):
            idx = int(item.get("index", pos - 1))
            temp_target = (temp_dir / safe_filename(f"{identifier}-html-figure-{idx+1}")).with_suffix(".png")
            final_target = (out_dir / safe_filename(f"{identifier}-html-figure-{idx+1}")).with_suffix(".png")
            href_key = Path(urlparse(item.get("href", "")).path).name
            src_key = Path(urlparse(item.get("src", "")).path).name
            caption_hint = normalize_whitespace(item.get("caption", "")).split("\n", 1)[0][:80]
            if href_key or src_key:
                key = href_key or src_key
                js = (
                    f"const linked = page.locator('figure a[href*={json.dumps(key)}], figure img[src*={json.dumps(key)}]').first();"
                    "let fig = null;"
                    "if (await linked.count()) { fig = linked.locator('xpath=ancestor::figure[1]'); }"
                    "if (!fig && await page.locator('figure').count()) {"
                    f"  fig = page.locator('figure').nth({idx});"
                    "}"
                    "if (fig) {"
                    "  await fig.scrollIntoViewIfNeeded().catch(() => {});"
                    "  const img = fig.locator('img').first();"
                    "  await img.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});"
                    "  await page.waitForFunction(el => !el || (el.complete && (el.naturalWidth || 0) > 250), await img.elementHandle().catch(() => null), { timeout: 8000 }).catch(() => {});"
                    "  await page.waitForTimeout(1200);"
                    f"  await fig.screenshot({{ path: {json.dumps(str(temp_target))}, animations: 'disabled' }});"
                    "}"
                )
            elif caption_hint:
                js = (
                    "const loc = page.locator('figure').filter({ hasText: "
                    + json.dumps(caption_hint)
                    + " }).first();"
                    "if (await loc.count()) {"
                    "  await loc.scrollIntoViewIfNeeded().catch(() => {});"
                    "  const img = loc.locator('img').first();"
                    "  await img.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});"
                    "  await page.waitForTimeout(1200);"
                    f"  await loc.screenshot({{ path: {json.dumps(str(temp_target))}, animations: 'disabled' }});"
                    "}"
                )
            else:
                js = (
                    "const loc = page.locator('figure');"
                    f"const count = await loc.count();"
                    f"if ({idx} < count) {{"
                    f"  const fig = loc.nth({idx});"
                    "  await fig.scrollIntoViewIfNeeded().catch(() => {});"
                    "  const img = fig.locator('img').first();"
                    "  await img.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});"
                    "  await page.waitForTimeout(1200);"
                    f"  await fig.screenshot({{ path: {json.dumps(str(temp_target))}, animations: 'disabled' }});"
                    "}"
                )
            try:
                result = subprocess.run(
                    base + ["run-code", js],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                if result.returncode != 0 or not temp_target.exists():
                    continue
                width, height = read_png_size(temp_target)
                if temp_target.stat().st_size < 25_000:
                    continue
                if width and height and (width < 220 or height < 220):
                    continue
                shutil.copy2(temp_target, final_target)
                saved_paths.append(str(final_target))
                saved_items.append({**item, "path": str(final_target), "source_url": item.get("href", "") or item.get("src", "")})
            except Exception:
                continue
        return saved_paths, saved_items
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def capture_playwright_images(base: list[str], identifier: str, image_candidates: list[dict]) -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR or not image_candidates:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    saved_items: list[dict] = []
    for pos, item in enumerate(image_candidates[:4], start=1):
        idx = int(item.get("index", pos - 1))
        src = item.get("src", "")
        src_key = Path(urlparse(src).path).name
        target = out_dir / safe_filename(f"{identifier}-img-{idx+1}")
        target = target.with_suffix(".png")
        if src_key:
            js = (
                f"const exact = page.locator('img[src*={json.dumps(src_key)}]').first();"
                "if (await exact.count()) {"
                "  await exact.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});"
                "  await exact.scrollIntoViewIfNeeded().catch(() => {});"
                "  await page.waitForTimeout(1200);"
                f"  await exact.screenshot({{ path: {json.dumps(str(target))}, animations: 'disabled' }});"
                "} else {"
                "  const loc = page.locator('img');"
                f"  const count = await loc.count();"
                f"  if ({idx} < count) {{"
                f"    await loc.nth({idx}).waitFor({{ state: 'visible', timeout: 5000 }}).catch(() => {{}});"
                f"    await loc.nth({idx}).scrollIntoViewIfNeeded().catch(() => {{}});"
                f"    await page.waitForTimeout(1200);"
                f"    await loc.nth({idx}).screenshot({{ path: {json.dumps(str(target))}, animations: 'disabled' }});"
                "  }"
                "}"
            )
        else:
            js = (
                "const loc = page.locator('img');"
                f"const count = await loc.count();"
                f"if ({idx} < count) {{"
                f"  await loc.nth({idx}).waitFor({{ state: 'visible', timeout: 5000 }}).catch(() => {{}});"
                f"  await loc.nth({idx}).scrollIntoViewIfNeeded().catch(() => {{}});"
                f"  await page.waitForTimeout(1200);"
                f"  await loc.nth({idx}).screenshot({{ path: {json.dumps(str(target))}, animations: 'disabled' }});"
                "}"
            )
        try:
            result = subprocess.run(
                base + ["run-code", js],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if result.returncode != 0 or not target.exists():
                continue
            width, height = read_png_size(target)
            if target.stat().st_size < 15_000:
                continue
            if width and height and (width < 220 or height < 220):
                continue
            saved_paths.append(str(target))
            saved_items.append({**item, "path": str(target)})
        except Exception:
            continue
    return saved_paths, saved_items


def identifier_from_url(url: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", url).strip("-")
    return safe_filename(slug[:80] or "playwright-page")


def parse_playwright_pdf_path(stdout: str, cwd: Path) -> Path | None:
    match = re.search(r"\[Page as pdf\]\(([^)]+)\)", stdout or "")
    if not match:
        return None
    raw = match.group(1).strip().strip("'\"")
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path if path.exists() else None


def capture_playwright_snapshot(base: list[str], out_path: Path) -> dict:
    js = (
        "() => {"
        "const main = document.querySelector('article, main, [role=main], .article, .article__body, .c-article-body, .main-content') || document.documentElement;"
        "const figNodes = Array.from(document.querySelectorAll('figure')).slice(0, 12);"
        "const figures = [];"
        "for (let idx = 0; idx < figNodes.length; idx++) {"
        "  const fig = figNodes[idx];"
        "  const img = fig.querySelector('img') || fig.querySelector('picture img');"
        "  const cap = fig.querySelector('figcaption');"
        "  const rect = fig.getBoundingClientRect();"
        "  const link = fig.querySelector('a[href]');"
        "  const src = img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : '';"
        "  if (!src) continue;"
        "  figures.push({"
        "    index: idx,"
        "    href: link ? link.href : '',"
        "    src: src,"
        "    alt: img ? (img.alt || '') : '',"
        "    caption: cap ? (cap.innerText || cap.textContent || '') : '',"
        "    width: Math.round(rect.width || 0),"
        "    height: Math.round(rect.height || 0)"
        "  });"
        "}"
        "const headings = Array.from(main.querySelectorAll('h1,h2,h3')).map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 40);"
        "const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);"
        "const metaPdf = document.querySelector('meta[name=\"citation_pdf_url\"], meta[property=\"citation_pdf_url\"]');"
        "const pdfCandidates = [];"
        "if (metaPdf && metaPdf.content) pdfCandidates.push(metaPdf.content);"
        "for (const href of links) {"
        "  if (/\\.pdf(?:\\x22|$)/i.test(href) || /\\/doi\\/(pdf|pdfdirect|epdf)\\//i.test(href)) pdfCandidates.push(href);"
        "}"
        "const uniqPdf = Array.from(new Set(pdfCandidates.filter(Boolean)));"
        "uniqPdf.sort((a,b) => {"
        "  const score = (x) => /\\.full\\.pdf(?:\\x22|$)/i.test(x) ? 5 : (/\\/doi\\/pdfdirect\\//i.test(x) ? 4 : (/\\/doi\\/pdf\\//i.test(x) ? 3 : (/\\.pdf(?:\\x22|$)/i.test(x) && !/\\.pdf\\+html/i.test(x) ? 2 : (/\\/doi\\/epdf\\//i.test(x) ? 1 : 0))));"
        "  return score(b) - score(a);"
        "});"
        "const pdf = uniqPdf.find(x => !/\\.pdf\\+html/i.test(x)) || uniqPdf[0] || '';"
        "const qmeta = (...names) => {"
        "  for (const name of names) {"
        "    const el = document.querySelector(`meta[name=\"${name}\"], meta[property=\"${name}\"]`);"
        "    const val = el ? (el.getAttribute('content') || '').trim() : '';"
        "    if (val) return val;"
        "  }"
        "  return '';"
        "};"
        "const authors = Array.from(document.querySelectorAll('meta[name=\"citation_author\"]')).map(el => (el.content || '').trim()).filter(Boolean);"
        "let doi = qmeta('citation_doi', 'dc.identifier', 'dc.identifier.doi', 'prism.doi');"
        "if (!doi) {"
        "  const doiLink = document.querySelector('a[href*=\"doi.org/10.\"]');"
        "  const href = doiLink ? (doiLink.href || '') : '';"
        "  const m = href.match(/10\\.\\d{4,9}\\/[\\-._;()/:A-Z0-9]+/i);"
        "  if (m) doi = m[0];"
        "}"
        "const imageCandidates = Array.from(document.images).slice(0, 60).map((img, idx) => ({"
        "  index: idx,"
        "  src: img.currentSrc || img.src || '',"
        "  alt: img.alt || '',"
        "  width: img.naturalWidth || img.width || 0,"
        "  height: img.naturalHeight || img.height || 0"
        "})).filter(x => x.src);"
        "return JSON.stringify({"
        "  url: location.href,"
        "  title: document.title,"
        "  body: (document.body ? document.body.innerText : '').slice(0, 80000),"
        "  html: (main.outerHTML || '').slice(0, 500000),"
        "  headings: headings,"
        "  authors: Array.from(new Set(authors)),"
        "  journal: qmeta('citation_journal_title', 'prism.publicationname', 'og:site_name'),"
        "  doi: (doi || '').replace(/^doi:\\s*/i, ''),"
        "  abstract: qmeta('citation_abstract', 'description', 'og:description'),"
        "  keywords: (qmeta('keywords') || '').split(',').map(x => x.trim()).filter(Boolean),"
        "  figureCount: document.querySelectorAll('figure').length,"
        "  figures: figures,"
        "  pdf: pdf,"
        "  pdfCandidates: uniqPdf,"
        "  imageUrl: qmeta('og:image', 'twitter:image'),"
        "  images: imageCandidates.map(x => x.src).slice(0, 20),"
        "  imageCandidates: imageCandidates"
        "});"
        "}"
    )
    result = subprocess.run(
        base + ["eval", js],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    payload = parse_playwright_result(result.stdout or "")
    if result.returncode != 0 or not payload:
        return {}
    try:
        outer = json.loads(payload)
        if isinstance(outer, str):
            return json.loads(outer)
        if isinstance(outer, dict):
            return outer
        return {}
    except Exception:
        return {}


def parse_local_pdf_file(path: Path, merge_doi: bool = False) -> dict:
    data = path.read_bytes()
    text = extract_pdf_text_from_bytes(data, temp_pdf_path=path)
    if not text:
        print(
            f"[paper-reader] 警告：无法从 {path.name} 提取可读文本。\n"
            "  常见原因：PDF 是扫描件/图片型，或文字层被加密。\n"
            "  笔记将退回摘要模式，内容质量会明显下降。\n"
            "  建议：使用含可选择文字的原版 PDF，或在命令行加 --local-only 跳过网络补全。",
            file=__import__("sys").stderr,
        )
    title = path.stem
    authors = []
    doi = ""
    try:
        reader = PdfReader(str(path))
        meta = reader.metadata or {}
        meta_title = normalize_whitespace(str(meta.get("/Title", "") or ""))
        if meta_title:
            title = meta_title
        author = normalize_whitespace(str(meta.get("/Author", "") or ""))
        if author:
            authors = [item.strip() for item in re.split(r"[;,]", author) if item.strip()]
        doi = parse_doi_candidate(normalize_whitespace(str(meta.get("/Subject", "") or "")))
    except Exception:
        pass
    doi = doi or find_doi_in_text(text[:15000])
    record = {
        "source_kind": "local_pdf",
        "title": title,
        "authors": authors,
        "journal": "",
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": "",
        "affiliations": [],
        "keywords": [],
        "full_text": text,
        "local_path": str(path),
        "downloaded_pdf": str(path),
        "summary_mode": "基于全文/PDF文本提取" if text else "基于 PDF 可提取文本片段",
        "image_url": "",
        # PyMuPDF-based extraction: finds Figure 1 by caption, produces one
        # well-cropped PNG at high resolution.  Falls back to pdfimages if
        # PyMuPDF is unavailable or returns nothing.
        "figure_paths": extract_fig1_pymupdf(path, path.stem) or extract_pdf_images(path, path.stem),
    }
    if merge_doi and doi:
        try:
            if is_elsevier_doi(doi):
                # Local PDF already present → only fetch Elsevier API metadata (title/
                # authors/abstract/journal).  skip_pdf_download=True prevents
                # fetch_elsevier_article from attempting HTTP + Playwright PDF
                # download — we already have the PDF locally.
                api_record = fetch_elsevier_article(doi, skip_pdf_download=True)
            else:
                # For ALL other publishers (Science, Nature, Wiley, etc.) the same
                # problem applies: fetch_doi() can trigger Playwright browser
                # automation (should_try_browser=True) which wastes 5-10 min for
                # metadata that open APIs provide in ~1-2 sec.  Since we already
                # have the full PDF text locally, use only lightweight sources:
                # OpenAlex (title/authors/journal) + PubMed (abstract/PMID).
                api_record = fetch_openalex_by_doi(doi) or {}
                pubmed_extra = fetch_pubmed_by_doi(doi)
                if pubmed_extra:
                    api_record = merge_records(api_record, pubmed_extra, prefer_new=False)
            record = merge_records(record, api_record, prefer_new=False)
            # Always prefer the API-fetched title: publisher PDF /Title metadata is
            # often an internal production string (e.g. "CELREP116110_grabs 1..1")
            # rather than the actual paper title.
            api_title = clean_record_title(api_record.get("title", ""))
            if api_title:
                record["title"] = api_title
        except Exception:
            pass
        if text:
            record["full_text"] = text
            record["summary_mode"] = "基于全文/PDF文本提取"
            record["downloaded_pdf"] = str(path)
    return record


def print_current_page_to_pdf(base: list[str], headed: bool, identifier: str) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="playwright_page_pdf_"))
    try:
        if headed:
            time.sleep(3)
        pdf_result = subprocess.run(
            base + ["pdf"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=str(workdir),
        )
        pdf_path = parse_playwright_pdf_path(pdf_result.stdout or "", workdir)
        if not pdf_path:
            return {}
        saved_path = ""
        if PDF_SAVE_DIR:
            try:
                PDF_SAVE_DIR.mkdir(parents=True, exist_ok=True)
                target = (PDF_SAVE_DIR / safe_filename(identifier + " - page")).with_suffix(".pdf")
                shutil.copy2(pdf_path, target)
                saved_path = str(target)
                pdf_path = target
            except Exception:
                saved_path = str(pdf_path)
        record = parse_local_pdf_file(pdf_path, merge_doi=False)
        if saved_path:
            record["downloaded_pdf"] = saved_path
        return record
    except Exception:
        return {}
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


def capture_python_playwright_primary_figure(page, identifier: str) -> tuple[list[str], list[dict]]:
    if not PDF_SAVE_DIR:
        return [], []
    out_dir = PDF_SAVE_DIR / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    figure_paths: list[str] = []
    figure_items: list[dict] = []
    try:
        figures = page.locator("figure")
        count = min(figures.count(), 4)
    except Exception:
        count = 0
    for idx in range(count):
        try:
            fig = figures.nth(idx)
            fig.scroll_into_view_if_needed(timeout=6000)
            page.wait_for_timeout(1800)
            fig_text = ""
            try:
                fig_text = normalize_whitespace(fig.inner_text(timeout=3000))
            except Exception:
                fig_text = ""
            img = fig.locator("img").first
            try:
                img.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            natural_width = 0
            try:
                natural_width = page.evaluate("(el) => el ? (el.naturalWidth || 0) : 0", img.element_handle(timeout=3000))
            except Exception:
                natural_width = 0
            box = fig.bounding_box()
            if not box or box.get("width", 0) < 220 or box.get("height", 0) < 220:
                continue
            if (
                box.get("height", 0) > 850
                and (
                    "abstract" in fig_text.lower()
                    or "review article" in fig_text.lower()
                    or identifier[:40].lower() in fig_text.lower()
                )
            ):
                continue
            target = (out_dir / safe_filename(f"{identifier}-html-figure-{idx+1}")).with_suffix(".png")
            if natural_width > 10:
                fig.screenshot(path=str(target))
            else:
                viewer_saved = False
                try:
                    fig.screenshot(path=str(target))
                    if target.exists() and target.stat().st_size >= 25_000:
                        viewer_saved = True
                except Exception:
                    viewer_saved = False
                try:
                    if viewer_saved:
                        pass
                    else:
                        viewer = fig.get_by_text("Open in figure viewer").first
                        if viewer.count() > 0:
                            viewer.click(timeout=5000, force=True)
                            page.wait_for_timeout(3000)
                            clip = {
                                "x": max(0, box["x"] - 40),
                                "y": max(0, box["y"] - 220),
                                "width": min(950, box["width"] + 140),
                                "height": min(1000, box["height"] + 420),
                            }
                            page.screenshot(path=str(target), clip=clip)
                            viewer_saved = True
                            try:
                                page.keyboard.press("Escape")
                                page.wait_for_timeout(800)
                            except Exception:
                                pass
                except Exception:
                    viewer_saved = False
                if not viewer_saved:
                    fig.screenshot(path=str(target))
            if not target.exists() or target.stat().st_size < 25_000:
                target.unlink(missing_ok=True)
                continue
            caption = ""
            source_url = ""
            try:
                caption = normalize_whitespace(fig.locator("figcaption").first.inner_text(timeout=3000))
            except Exception:
                pass
            try:
                source_url = normalize_whitespace(img.get_attribute("src", timeout=3000) or "")
            except Exception:
                pass
            figure_paths.append(str(target))
            figure_items.append(
                {
                    "index": idx,
                    "path": str(target),
                    "caption": caption,
                    "source_url": source_url,
                    "source": "figure",
                }
            )
            break
        except Exception:
            continue
    return figure_paths, figure_items


def collect_python_playwright_pdf_candidates(page, current_url: str) -> list[str]:
    candidates: list[str] = []
    try:
        anchors = page.locator("a[href], area[href]")
        count = min(anchors.count(), 300)
    except Exception:
        count = 0
    for idx in range(count):
        try:
            href = anchors.nth(idx).get_attribute("href", timeout=1500) or ""
        except Exception:
            continue
        href = normalize_whitespace(href)
        if not href:
            continue
        lower = href.lower()
        anchor_text = ""
        try:
            anchor_text = normalize_whitespace(anchors.nth(idx).inner_text(timeout=1500)).lower()
        except Exception:
            anchor_text = ""
        if (
            ".pdf" in lower
            or "/doi/pdf" in lower
            or "/doi/epdf/" in lower
            or "/doi/pdfdirect/" in lower
            or anchor_text == "pdf"
            or "download pdf" in anchor_text
        ):
            candidates.append(urljoin(current_url, href))
    return normalized_pdf_candidates(candidates, current_url)


def fetch_web_with_python_playwright(url: str, headed: bool = False) -> dict:
    if sync_playwright is None:
        return {}
    chrome_path = find_chrome_executable()
    if not chrome_path:
        return {}
    browser = None
    context = None
    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not headed,
                executable_path=chrome_path,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1200},
                ignore_https_errors=True,
                user_agent=STANDARD_CHROME_UA,
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = window.chrome || { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(10000 if headed else 6500)
            try_solve_human_check(page)
            for label in ["Accept all cookies", "Accept all", "Accept Cookies", "Accept", "Allow all", "I agree"]:
                try:
                    btn = page.get_by_role("button", name=label)
                    if btn.count() > 0:
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
            try_solve_human_check(page)
            for _ in range(2):
                try:
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1500)
                except Exception:
                    break
            try:
                page.mouse.wheel(0, -2200)
            except Exception:
                pass
            page.wait_for_timeout(1200)

            current_url = normalize_whitespace(page.url) or url
            title = clean_record_title(page.title())
            body = ""
            html_fragment = ""
            try:
                body = normalize_whitespace(page.locator("body").inner_text(timeout=20000))
            except Exception:
                pass
            try:
                html_fragment = page.content()
            except Exception:
                pass

            dom_meta = collect_page_dom_metadata(page, current_url)
            html_meta = parse_html_metadata_v2(current_url, html_fragment) if html_fragment else {}
            html_meta = merge_records(html_meta, dom_meta, prefer_new=True) if dom_meta else html_meta
            html_title, html_text = extract_main_text_from_html(current_url, html_fragment, fallback_body=body)
            if html_meta.get("title"):
                title = clean_record_title(html_meta.get("title", "")) or title
            elif html_title:
                title = clean_record_title(html_title) or title
            normalized_body = html_meta.get("full_text", "") or html_text or body
            html_is_full = looks_like_full_article(title, normalized_body)

            figure_paths, figure_items = capture_python_playwright_primary_figure(page, title or identifier_from_url(url))
            pdf_candidates = collect_python_playwright_pdf_candidates(page, current_url)
            html_pdf_url = normalize_whitespace(html_meta.get("pdf_url", "")) if html_meta else ""
            if html_pdf_url:
                pdf_candidates = normalized_pdf_candidates(pdf_candidates + [html_pdf_url], current_url)
            pdf_url = pdf_candidates[0] if pdf_candidates else ""
            downloaded_pdf = ""
            publisher_pdf_record = {}
            if pdf_url:
                try:
                    publisher_pdf_record = fetch_pdf_with_python_playwright_context(context, pdf_candidates, title or identifier_from_url(url))
                except Exception:
                    publisher_pdf_record = {}
                if not (publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf")):
                    for candidate_pdf in pdf_candidates:
                        try:
                            publisher_pdf_record = fetch_pdf_url(candidate_pdf, referer=current_url)
                        except Exception:
                            publisher_pdf_record = {}
                        if publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf"):
                            if not publisher_pdf_record.get("pdf_url"):
                                publisher_pdf_record["pdf_url"] = candidate_pdf
                            break
                downloaded_pdf = publisher_pdf_record.get("downloaded_pdf", "") if isinstance(publisher_pdf_record, dict) else ""

            record = {
                "source_kind": "web_url",
                "title": title,
                "authors": clean_author_candidates(html_meta.get("authors", []) if isinstance(html_meta.get("authors"), list) else []),
                "journal": normalize_whitespace(html_meta.get("journal", "")),
                "pmid": "",
                "doi": normalize_whitespace(html_meta.get("doi", "")),
                "pubmed_url": "",
                "doi_url": normalize_whitespace(f"https://doi.org/{html_meta.get('doi', '')}") if html_meta.get("doi") else "",
                "abstract": normalize_whitespace(html_meta.get("abstract", "")),
                "affiliations": [],
                "keywords": html_meta.get("keywords", []) if isinstance(html_meta.get("keywords"), list) else [],
                "full_text": normalized_body[:45000],
                "pdf_url": pdf_url,
                "pdf_candidates": pdf_candidates,
                "downloaded_pdf": downloaded_pdf,
                "web_url": current_url,
                "summary_mode": "基于全文/网页正文" if html_is_full else ("基于网页内容/元数据" if normalized_body else "基于网页元数据"),
                "image_url": normalize_whitespace(html_meta.get("image_url", "")),
                "figure_paths": figure_paths,
                "figure_items": figure_items,
            }
            if publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf"):
                record = merge_fulltext_record(record, publisher_pdf_record)
            if record.get("full_text") or record.get("downloaded_pdf") or record.get("title"):
                return record
    except Exception:
        return {}
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
    return {}


def fetch_web_with_playwright(url: str, headed: bool = False) -> dict:
    if SKIP_PLAYWRIGHT:
        return {}
    python_record = fetch_web_with_python_playwright(url, headed=headed)
    if python_record.get("full_text") or python_record.get("downloaded_pdf") or python_record.get("title"):
        return python_record
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return {}
    session_name = f"{PLAYWRIGHT_SESSION}-{int(time.time() * 1000)}"
    base = [npx, "--yes", "--package", "@playwright/cli", "playwright-cli", f"-s={session_name}"]
    snapshot_dir: Path | None = None
    try:
        open_args = ["open", url]
        if headed:
            open_args.append("--headed")
        open_result = subprocess.run(
            base + open_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if open_result.returncode != 0:
            return {}
        prime_js = (
            "await page.waitForLoadState('domcontentloaded');"
            "const clickIfMatch = async (els, checks) => {"
            "  for (const el of els) {"
            "    const txt = ((await el.innerText().catch(() => '')) || '').trim().toLowerCase();"
            "    const aria = (((await el.getAttribute('aria-label').catch(() => '')) || '') + ' ' + ((await el.getAttribute('title').catch(() => '')) || '')).toLowerCase();"
            "    const hay = `${txt} ${aria}`;"
            "    if (checks.some(token => hay.includes(token))) {"
            "      try { await el.click({ timeout: 2000 }); return true; } catch (e) {}"
            "    }"
            "  }"
            "  return false;"
            "};"
            "for (let round = 0; round < 3; round++) {"
            "  const buttons = await page.locator('button, [role=button], a').all();"
            "  await clickIfMatch(buttons, ['accept all', 'accept cookies', 'accept', 'allow all', 'agree']);"
            "  await clickIfMatch(buttons, ['close', 'dismiss', 'not now', 'continue without']);"
            "  await page.mouse.wheel(0, 2200);"
            "  await page.waitForTimeout(2500);"
            "}"
            "await page.waitForFunction(() => document.querySelectorAll('img[src*=\"-fig-\"], figure img').length > 0, { timeout: 8000 }).catch(() => {});"
            "await page.mouse.wheel(0, -2200);"
            "await page.waitForTimeout(1000);"
        )
        dismiss_expr = (
            "(function(){"
            "const sels=["
            "'button[aria-label*=close i]',"
            "'button[aria-label*=dismiss i]',"
            "'button[aria-label*=cancel i]',"
            "'button[title*=close i]',"
            "'button[title*=dismiss i]',"
            "'[data-testid*=close]',"
            "'[class*=close]'"
            "];"
            "let clicked=0;"
            "for (const sel of sels){"
            "  for (const el of document.querySelectorAll(sel)){"
            "    const txt=((el.innerText||el.textContent||'').trim()).toLowerCase();"
            "    const label=((el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||'')).toLowerCase();"
            "    if (txt==='x' || txt.includes('close') || txt.includes('dismiss') || txt.includes('not now') || txt.includes('continue without') || label.includes('close') || label.includes('dismiss')){"
            "      try{ el.click(); clicked++; }catch(e){}"
            "    }"
            "  }"
            "}"
            "return clicked;"
            "})()"
        )
        data = None
        best_score = -1
        attempts = 4 if headed else 3
        initial_wait = 10 if headed else 6
        retry_wait = 6 if headed else 4
        time.sleep(initial_wait)
        snapshot_dir = Path(tempfile.mkdtemp(prefix="playwright_snapshot_"))
        for attempt in range(attempts):
            try:
                subprocess.run(
                    base + ["run-code", prime_js],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
            except Exception:
                pass
            if headed:
                try:
                    subprocess.run(
                        base + ["eval", dismiss_expr],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                except Exception:
                    pass
            snapshot_path = snapshot_dir / f"snapshot-{attempt}.json"
            candidate = capture_playwright_snapshot(base, snapshot_path)
            if candidate:
                candidate_title = clean_record_title(candidate.get("title", ""))
                candidate_body = normalize_whitespace(candidate.get("body", "") or "")
                quality = snapshot_quality(candidate)
                if quality > best_score:
                    data = candidate
                    best_score = quality
                if looks_like_full_article(candidate_title, candidate_body):
                    break
            if attempt < attempts - 1:
                time.sleep(retry_wait)
        body = normalize_whitespace((data or {}).get("body", "") or "")
        html_fragment = (data or {}).get("html", "") or ""
        normalized_body = body
        title = clean_record_title((data or {}).get("title", ""))
        current_url = normalize_whitespace((data or {}).get("url", "")) or url
        snapshot_meta = {
            "title": clean_record_title((data or {}).get("title", "")),
            "authors": (data or {}).get("authors", []) or [],
            "journal": normalize_whitespace((data or {}).get("journal", "")),
            "doi": normalize_whitespace((data or {}).get("doi", "")),
            "abstract": normalize_whitespace((data or {}).get("abstract", "")),
            "keywords": (data or {}).get("keywords", []) or [],
            "pdf_url": normalize_whitespace((data or {}).get("pdf", "")),
            "image_url": normalize_whitespace((data or {}).get("imageUrl", "")),
        }
        html_meta = parse_html_metadata_v2(current_url, html_fragment) if html_fragment else {}
        html_meta = merge_records(html_meta, snapshot_meta, prefer_new=True)
        html_title, html_text = extract_main_text_from_html(current_url, html_fragment, fallback_body=body)
        if html_meta.get("title"):
            title = clean_record_title(html_meta.get("title", "")) or title
        elif html_title:
            title = clean_record_title(html_title) or title
        if html_meta.get("full_text"):
            normalized_body = html_meta.get("full_text", "")
        elif html_text:
            normalized_body = html_text
        pdf_candidates = normalized_pdf_candidates((data or {}).get("pdfCandidates") or [(data or {}).get("pdf", "")], current_url)
        html_pdf_url = normalize_whitespace(html_meta.get("pdf_url", "")) if html_meta else ""
        if html_pdf_url:
            pdf_candidates = normalized_pdf_candidates(pdf_candidates + [html_pdf_url], current_url)
        pdf_url = pdf_candidates[0] if pdf_candidates else ""
        image_url = normalize_whitespace(html_meta.get("image_url", "")) or (((data or {}).get("images") or [""])[0] if isinstance((data or {}).get("images"), list) and (data or {}).get("images") else "")
        figure_count = int((data or {}).get("figureCount", 0) or 0)
        figure_candidates = (data or {}).get("figures") or collect_html_figure_candidates(html_fragment, current_url)
        figure_candidates = [
            {
                **item,
                "href": urljoin(current_url, item.get("href", "")) if item.get("href") else "",
                "src": urljoin(current_url, item.get("src", "")),
                "caption": normalize_whitespace(item.get("caption", "")),
                "alt": normalize_whitespace(item.get("alt", "")),
            }
            for item in figure_candidates
            if item.get("src", "")
        ]
        if not figure_candidates:
            figure_candidates = filter_image_candidates((data or {}).get("imageCandidates") or [], current_url)
        figure_candidates.sort(
            key=lambda item: (int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0)),
            reverse=True,
        )
        figure_paths, figure_items = save_html_figure_urls_with_playwright_context(
            base,
            figure_candidates,
            title or identifier_from_url(url),
        )
        if not figure_paths:
            figure_paths, figure_items = save_html_figure_urls(figure_candidates, title or identifier_from_url(url), referer=current_url)
        if not figure_paths and (figure_candidates or figure_count):
            screenshot_targets = figure_candidates or [{"index": idx, "source": "figure"} for idx in range(min(4, figure_count))]
            if screenshot_targets and any((item.get("source") == "figure") or item.get("href") for item in screenshot_targets):
                figure_paths, figure_items = capture_playwright_figures(base, title or identifier_from_url(url), screenshot_targets)
            elif screenshot_targets and any(item.get("src") for item in screenshot_targets):
                figure_paths, figure_items = capture_playwright_images(base, title or identifier_from_url(url), screenshot_targets)
            else:
                figure_paths, figure_items = capture_playwright_figures(base, title or identifier_from_url(url), screenshot_targets)
        downloaded_pdf = download_pdf_to_dir(pdf_url, "playwright", title) if pdf_url else ""
        html_is_full = looks_like_full_article(title, normalized_body)
        record = {
            "source_kind": "web_url",
            "title": title,
                "authors": clean_author_candidates(html_meta.get("authors", []) if isinstance(html_meta.get("authors"), list) else []),
            "journal": normalize_whitespace(html_meta.get("journal", "")),
            "pmid": "",
            "doi": normalize_whitespace(html_meta.get("doi", "")),
            "pubmed_url": "",
            "doi_url": normalize_whitespace(f"https://doi.org/{html_meta.get('doi', '')}") if html_meta.get("doi") else "",
            "abstract": normalize_whitespace(html_meta.get("abstract", "")),
            "affiliations": [],
            "keywords": html_meta.get("keywords", []) if isinstance(html_meta.get("keywords"), list) else [],
            "full_text": normalized_body[:45000],
            "pdf_url": pdf_url,
            "pdf_candidates": pdf_candidates,
            "downloaded_pdf": downloaded_pdf,
            "web_url": current_url,
            "summary_mode": "基于全文/网页正文" if looks_like_full_article(title, body) else ("基于网页内容/元数据" if normalized_body else "基于网页元数据"),
            "image_url": image_url,
            "figure_paths": figure_paths,
            "figure_items": figure_items,
            "summary_mode": "基于全文/网页正文" if html_is_full else ("基于网页内容/元数据" if normalized_body else "基于网页元数据"),
        }
        if pdf_url:
            publisher_pdf_record = fetch_pdf_with_playwright_context(
                base,
                pdf_candidates or [pdf_url],
                current_url,
                title or identifier_from_url(url),
            )
            if not (publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf")):
                for candidate_pdf in pdf_candidates or [pdf_url]:
                    try:
                        publisher_pdf_record = fetch_pdf_url(candidate_pdf, referer=current_url)
                    except Exception:
                        publisher_pdf_record = {}
                    if publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf"):
                        if not publisher_pdf_record.get("pdf_url"):
                            publisher_pdf_record["pdf_url"] = candidate_pdf
                        break
            if publisher_pdf_record.get("full_text") or publisher_pdf_record.get("downloaded_pdf"):
                record = merge_fulltext_record(record, publisher_pdf_record)
        page_pdf_record = print_current_page_to_pdf(base, headed, title or identifier_from_url(url))
        if page_pdf_record.get("downloaded_pdf") and not record.get("downloaded_pdf"):
            record["downloaded_pdf"] = page_pdf_record["downloaded_pdf"]
        if (not record.get("figure_paths")) and page_pdf_record.get("figure_paths"):
            record["figure_paths"] = page_pdf_record.get("figure_paths", [])
        page_pdf_text = page_pdf_record.get("full_text", "") or ""
        pdf_quality = pdf_text_quality(page_pdf_text)
        html_quality = len(normalized_body) + 4000 * count_article_section_hits(normalized_body)
        if not (pdf_quality > 0 and (not html_is_full or pdf_quality > html_quality + 6000)):
            page_pdf_text = ""
        if page_pdf_text and (count_article_section_hits(page_pdf_text) >= 2 or len(page_pdf_text) > 12000):
            record = merge_records(record, page_pdf_record, prefer_new=False)
            if len(page_pdf_text) >= len(record.get("full_text", "")):
                record["full_text"] = page_pdf_text
            record["summary_mode"] = "基于全文/PDF文本提取"
            if page_pdf_record.get("downloaded_pdf") and not record.get("downloaded_pdf"):
                record["downloaded_pdf"] = page_pdf_record["downloaded_pdf"]
            if (not record.get("figure_paths")) and page_pdf_record.get("figure_paths"):
                record["figure_paths"] = page_pdf_record.get("figure_paths", [])
        elif looks_like_full_article(title, normalized_body):
            record["summary_mode"] = "基于全文/网页正文"
        if html_is_full and record.get("summary_mode") != "基于全文/PDF文本提取":
            record["summary_mode"] = "基于全文/网页正文"
        if record.get("full_text") or record.get("title") or record.get("downloaded_pdf"):
            return record
        return {}
    except Exception:
        return {}
    finally:
        try:
            if snapshot_dir is not None:
                shutil.rmtree(snapshot_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if headed:
                time.sleep(2)
            subprocess.run(
                base + ["close"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except Exception:
            pass


def try_fetch_full_text_link(url: str) -> dict:
    if not url:
        return {}
    try:
        if url.lower().endswith(".pdf"):
            return fetch_pdf_url(url)
        record = fetch_generic_web(url)
        if looks_like_full_article(record.get("title", ""), record.get("full_text", "")):
            return record
        if record.get("pdf_url"):
            try:
                pdf_record = fetch_pdf_url(record["pdf_url"])
                return merge_records(record, pdf_record, prefer_new=False)
            except Exception:
                pass
    except Exception:
        pass

    browser_record = fetch_web_with_playwright(url, headed=PREFER_VISIBLE_BROWSER)
    redirect_url = normalize_whitespace(browser_record.get("web_url", "")) if browser_record else ""
    if (not browser_record_has_fulltext(browser_record)) and redirect_url and redirect_url != url and "doi.org/" not in redirect_url.lower():
        retried = fetch_web_with_playwright(redirect_url, headed=PREFER_VISIBLE_BROWSER)
        if browser_record_has_fulltext(retried):
            browser_record = retried
        elif retried:
            browser_record = merge_records(browser_record or {}, retried, prefer_new=True)
    if browser_record.get("pdf_url"):
        try:
            pdf_record = fetch_pdf_url(browser_record["pdf_url"])
            return merge_records(browser_record, pdf_record, prefer_new=False)
        except Exception:
            pass
    if browser_record_has_fulltext(browser_record):
        return browser_record
    return {}


def extract_pdf_text_with_pdftotext(path: Path) -> str:
    pdftotext = find_tool("pdftotext.exe") or find_tool("pdftotext")
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


def extract_pdf_text_from_bytes(data: bytes, temp_pdf_path: Path | None = None) -> str:
    if temp_pdf_path is not None:
        parsed = extract_pdf_text_with_pdftotext(temp_pdf_path)
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


def fetch_doi(doi: str) -> dict:
    doi = parse_doi_candidate(doi)
    if not doi:
        return {}
    url = f"https://doi.org/{quote(doi, safe='/')}"
    raw = ""
    try:
        raw = fetch_url(url).decode("utf-8", errors="replace")
    except Exception:
        pass
    meta = parse_html_metadata(url, raw) if raw else {
        "source_kind": "doi",
        "title": "",
        "authors": [],
        "journal": "",
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": url,
        "abstract": "",
        "affiliations": [],
        "keywords": [],
        "full_text": "",
        "summary_mode": "基于 DOI 元数据",
        "image_url": "",
    }

    openalex_meta = fetch_openalex_by_doi(doi)
    if openalex_meta:
        meta = merge_records(meta, openalex_meta, prefer_new=False)

    should_try_browser = (
        (not raw)
        or publisher_page_blocked(meta.get("title", ""), meta.get("full_text", ""))
        or (meta.get("title", "") == "Just a moment...")
    )
    if should_try_browser:
        browser_meta = fetch_web_with_playwright(url, headed=PREFER_VISIBLE_BROWSER)
        redirect_url = normalize_whitespace(browser_meta.get("web_url", "")) if browser_meta else ""
        if (not browser_record_has_fulltext(browser_meta)) and redirect_url and redirect_url != url and "doi.org/" not in redirect_url.lower():
            retried = fetch_web_with_playwright(redirect_url, headed=PREFER_VISIBLE_BROWSER)
            if browser_record_has_fulltext(retried):
                browser_meta = retried
            elif retried:
                browser_meta = merge_records(browser_meta or {}, retried, prefer_new=True)
        if browser_record_has_fulltext(browser_meta):
            preserve_elsevier_api = (
                is_elsevier_doi(doi)
                and normalize_whitespace(str(meta.get("full_text_status", "") or "")).lower() == "elsevier_api"
                and summary_mode_indicates_fulltext(meta)
            )
            meta = merge_records(meta, browser_meta, prefer_new=not preserve_elsevier_api)
            if preserve_elsevier_api:
                meta["summary_mode"] = normalize_whitespace(str(meta.get("summary_mode", "") or "")) or "基于 Elsevier API 全文/XML"
                meta["acquisition_path"] = normalize_whitespace(str(meta.get("acquisition_path", "") or "")) or "Elsevier API(view=FULL)"
                meta["source_kind"] = "doi"
                if normalize_whitespace(str(meta.get("web_url", "") or "")).lower().startswith("https://www.cell.com/"):
                    preferred_web = normalize_whitespace(str(browser_meta.get("web_url", "") or ""))
                    if "sciencedirect.com" in preferred_web.lower():
                        meta["web_url"] = preferred_web
            if browser_meta.get("pdf_url"):
                try:
                    meta = merge_records(meta, fetch_pdf_url(browser_meta["pdf_url"]), prefer_new=False)
                except Exception:
                    pass
        elif browser_meta.get("pdf_url"):
            try:
                meta = merge_records(meta, fetch_pdf_url(browser_meta["pdf_url"]), prefer_new=False)
            except Exception:
                pass
    meta["doi"] = doi
    meta["doi_url"] = url
    pubmed_meta = fetch_pubmed_by_doi(doi)
    if pubmed_meta:
        meta = merge_records(meta, pubmed_meta, prefer_new=False)
        if pubmed_meta.get("full_text") or pubmed_meta.get("downloaded_pdf"):
            meta = merge_fulltext_record(meta, pubmed_meta)
    return meta


def fetch_pdf_url(url: str, referer: str = "") -> dict:
    data = fetch_url(
        url,
        headers={
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            "Referer": referer,
        },
    )
    if not looks_like_pdf_bytes(data):
        raise ValueError(f"URL did not return a PDF: {url}")
    tmp_pdf = Path(tempfile.mkdtemp(prefix="paper_pdf_")) / "download.pdf"
    tmp_pdf.write_bytes(data)
    saved_path = save_pdf_bytes(data, "publisher", identifier_from_url(url))
    parse_path = Path(saved_path) if saved_path else tmp_pdf
    text = extract_pdf_text_from_bytes(data, temp_pdf_path=parse_path)
    title = ""
    doi = ""
    try:
        reader = PdfReader(io.BytesIO(data))
        meta = reader.metadata or {}
        title = normalize_whitespace(str(meta.get("/Title", "") or ""))
        doi = parse_doi_candidate(normalize_whitespace(str(meta.get("/Subject", "") or "")))
    except Exception:
        pass
    doi = doi or find_doi_in_text(text[:15000])

    record = {
        "source_kind": "pdf_url",
        "title": title,
        "authors": [],
        "journal": "",
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "abstract": "",
        "affiliations": [],
        "keywords": [],
        "full_text": text,
        "pdf_url": url,
        "downloaded_pdf": saved_path,
        "summary_mode": "基于全文/PDF文本提取" if text else "基于 PDF 可提取文本片段",
        "image_url": "",
    }
    if saved_path:
        record["figure_paths"] = extract_pdf_images(Path(saved_path), Path(saved_path).stem)
    if doi:
        try:
            record = merge_records(record, fetch_doi(doi), prefer_new=False)
        except Exception:
            pass
        if text:
            record["full_text"] = text
            record["summary_mode"] = "基于全文/PDF文本提取"
    if saved_path and not record.get("downloaded_pdf"):
        record["downloaded_pdf"] = saved_path
    try:
        tmp_pdf.unlink(missing_ok=True)
        tmp_pdf.parent.rmdir()
    except Exception:
        pass
    return record


def fetch_pdf_with_playwright_context(base: list[str], pdf_candidates: list[str], current_url: str, title_hint: str) -> dict:
    if not pdf_candidates:
        return {}
    temp_dir = Path(tempfile.mkdtemp(prefix="playwright_pdf_ctx_"))
    temp_pdf = temp_dir / "ctx-download.pdf"
    try:
        js = (
            f"const candidates = {json.dumps(pdf_candidates)};"
            f"const target = {json.dumps(temp_pdf.as_posix())};"
            "const attempts = [];"
            "for (const candidate of candidates) {"
            "  try {"
            "    const p = await page.context().newPage();"
            "    const downloadPromise = p.waitForEvent('download', { timeout: 15000 }).catch(() => null);"
            "    let resp = null;"
            "    try {"
            "      resp = await p.goto(candidate, { waitUntil: 'commit', timeout: 120000 });"
            "    } catch (e) {"
            "      attempts.push({ url: candidate, stage: 'goto', error: String(e) });"
            "    }"
            "    const download = await downloadPromise;"
            "    if (download) {"
            "      await download.saveAs(target);"
            "      attempts.push({ url: candidate, via: 'download', suggested: download.suggestedFilename() });"
            "      await p.close().catch(() => {});"
            "      return JSON.stringify({ ok: true, url: candidate, attempts });"
            "    }"
            "    let body = resp ? await resp.body() : Buffer.from('');"
            "    let ct = ((resp && resp.headers()['content-type']) || '').toLowerCase();"
            "    let finalUrl = p.url();"
            "    attempts.push({ url: candidate, finalUrl, status: resp ? resp.status() : 0, len: body.length, contentType: ct });"
            "    if (!(body.slice(0, 1024).includes(Buffer.from('%PDF'))) && ct.includes('text/html')) {"
            "      try {"
            "        await p.waitForLoadState('domcontentloaded').catch(() => {});"
            "        await p.waitForTimeout(2500);"
            "        const iframeSrc = await p.evaluate(() => {"
            "          const iframe = document.querySelector('iframe[src*=\"pdfdirect\"], iframe[src*=\"/doi/pdfdirect/\"]');"
            "          return iframe ? iframe.getAttribute('src') || '' : '';"
            "        });"
            "        let abs = '';"
            "        if (iframeSrc) {"
            "          abs = new URL(iframeSrc, p.url()).toString();"
            "        } else if (/\\/doi\\/(pdf|epdf)\\//i.test(candidate)) {"
            "          abs = candidate.replace('/doi/epdf/', '/doi/pdfdirect/').replace('/doi/pdf/', '/doi/pdfdirect/');"
            "        }"
            "        if (abs) {"
            "          const dl2Promise = p.waitForEvent('download', { timeout: 15000 }).catch(() => null);"
            "          let resp2 = null;"
            "          try {"
            "            resp2 = await p.goto(abs, { waitUntil: 'commit', timeout: 120000 });"
            "          } catch (e) {"
            "            attempts.push({ url: abs, stage: 'goto', error: String(e) });"
            "          }"
            "          const download2 = await dl2Promise;"
            "          if (download2) {"
            "            await download2.saveAs(target);"
            "            attempts.push({ url: abs, via: 'download', suggested: download2.suggestedFilename() });"
            "            await p.close().catch(() => {});"
            "            return JSON.stringify({ ok: true, url: abs, attempts });"
            "          }"
            "          body = resp2 ? await resp2.body() : Buffer.from('');"
            "          ct = ((resp2 && resp2.headers()['content-type']) || '').toLowerCase();"
            "          finalUrl = p.url();"
            "          attempts.push({ url: abs, finalUrl, status: resp2 ? resp2.status() : 0, len: body.length, contentType: ct, via: 'iframe-pdfdirect' });"
            "        }"
            "      } catch (e) {"
            "        attempts.push({ url: candidate, stage: 'iframe-pdfdirect', error: String(e) });"
            "      }"
            "    }"
            "    if (body.slice(0, 1024).includes(Buffer.from('%PDF'))) {"
            "      await p.close().catch(() => {});"
            "      return JSON.stringify({ ok: true, url: finalUrl || candidate, attempts });"
            "    }"
            "    await p.close().catch(() => {});"
            "  } catch (e) {"
            "    attempts.push({ url: candidate, error: String(e) });"
            "  }"
            "}"
            "return JSON.stringify({ ok: false, attempts });"
        )
        result = subprocess.run(
            base + ["run-code", js],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode != 0 or not temp_pdf.exists():
            return {}
        data = temp_pdf.read_bytes()
        if not looks_like_pdf_bytes(data):
            return {}
        saved_path = save_pdf_bytes(data, "publisher", title_hint or identifier_from_url(current_url))
        parse_path = Path(saved_path) if saved_path else temp_pdf
        record = parse_local_pdf_file(parse_path, merge_doi=False)
        payload = parse_playwright_result(result.stdout or "")
        try:
            decoded = json.loads(payload) if payload else {}
        except Exception:
            decoded = {}
        if isinstance(decoded, dict) and decoded.get("url"):
            record["pdf_url"] = decoded["url"]
        if saved_path:
            record["downloaded_pdf"] = saved_path
        return record
    except Exception:
        return {}
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def read_local_pdf(path_str: str, enrich_metadata: bool = True) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Local PDF not found: {path}")
    return parse_local_pdf_file(path, merge_doi=enrich_metadata)


def resolve_source(source: str) -> dict:
    kind, value = detect_source_kind(source)
    if kind == "pubmed_url":
        pmid_match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?", value)
        if not pmid_match:
            raise ValueError(f"Cannot parse PMID from URL: {value}")
        pmid = pmid_match.group(1)

        # ── Step A: PMC-first — free, legal, highest metadata quality ────────
        # Query NCBI eLink to check if this paper has a PMC full-text record
        # before attempting any publisher-side browser automation.
        pmc_id = _fetch_pmc_id_via_elink(pmid)
        if pmc_id:
            pmc_record = _fetch_pmc_record(pmc_id)
            if pmc_record.get("full_text"):
                # Enrich PMC full text with PubMed-side metadata
                pubmed_record = fetch_pubmed_by_pmid(pmid)
                record = merge_fulltext_record(pubmed_record, pmc_record)
                record["pmc_id"]       = pmc_id
                record["summary_mode"] = pmc_record.get("summary_mode", "") or record.get("summary_mode", "")
                return record

        # ── Step B: PubMed page full-text links (e.g. open-access publisher) ─
        record = fetch_pubmed_by_pmid(pmid)
        for link in record.get("full_text_links", []):
            ft = try_fetch_full_text_link(link)
            if ft.get("full_text") or ft.get("downloaded_pdf"):
                return merge_fulltext_record(record, ft)

        # ── Step C: Publisher page via DOI (Elsevier API or browser scraping) ─
        if record.get("doi"):
            try:
                ft = fetch_doi(record["doi"])
                if ft.get("full_text") or ft.get("downloaded_pdf"):
                    return merge_fulltext_record(record, ft)
            except Exception:
                pass
        return record

    if kind == "pmc_url":
        # Direct PMC URL (e.g. https://pmc.ncbi.nlm.nih.gov/articles/PMC9876543/)
        pmc_match = re.search(r"PMC(\d+)", value, re.IGNORECASE)
        pmc_id = pmc_match.group(1) if pmc_match else ""
        record = _fetch_pmc_record(pmc_id) if pmc_id else {}
        if not record:
            # Fallback: treat as generic web URL (Playwright)
            record = fetch_web_with_playwright(value, headed=PREFER_VISIBLE_BROWSER) or {}
        return record
    if kind == "doi":
        if value.startswith("10.1101/"):
            preprint_record = fetch_preprint_record(value)
            if preprint_record:
                return preprint_record
        return fetch_doi(value)
    if kind == "local_pdf":
        return read_local_pdf(value)
    if kind == "pdf_url":
        return fetch_pdf_url(value)
    if kind == "web_url":
        if is_biorxiv_or_medrxiv_content_url(value):
            preprint_record = fetch_preprint_record(value)
            if preprint_record:
                return preprint_record
        generic_record = {}
        try:
            generic_record = fetch_generic_web(value)
        except Exception:
            generic_record = {}
        browser_record = fetch_web_with_playwright(value, headed=PREFER_VISIBLE_BROWSER)
        if looks_like_full_article(generic_record.get("title", ""), generic_record.get("full_text", "")):
            record = merge_records(generic_record, browser_record or {}, prefer_new=False)
            if generic_record.get("figure_items"):
                record["figure_items"] = generic_record.get("figure_items", [])
            if generic_record.get("figure_paths"):
                record["figure_paths"] = generic_record.get("figure_paths", [])
            if generic_record.get("image_url"):
                record["image_url"] = generic_record.get("image_url", "")
            record["summary_mode"] = "基于全文/网页正文"
        else:
            record = browser_record or generic_record
        if record.get("doi"):
            try:
                pubmed_meta = fetch_pubmed_by_doi(record["doi"])
                if pubmed_meta:
                    record = merge_records(record, pubmed_meta, prefer_new=False)
                    if pubmed_meta.get("full_text") or pubmed_meta.get("downloaded_pdf"):
                        record = merge_fulltext_record(record, pubmed_meta)
            except Exception:
                pass
        if record.get("doi"):
            try:
                enriched = fetch_doi(record["doi"])
                record = merge_records(record, enriched, prefer_new=False)
            except Exception:
                pass
        return record
    raise ValueError(f"Unsupported source: {source}")


def build_fallback_sections(record: dict, mode: str) -> dict:
    abstract = record.get("abstract", "")
    full_text = record.get("full_text", "")
    title = record.get("title", "")
    rich_text = full_text if normalize_whitespace(full_text) else abstract
    summary_text = rich_text or title

    finding_sentences = article_sentences(summary_text, limit=5)
    main_findings = markdown_bullets(
        finding_sentences,
        limit=5,
        fallback="No stable main findings could be extracted.",
    )

    method_candidates = [
        sentence
        for sentence in article_sentences(rich_text, limit=8)
        if any(token in sentence.lower() for token in ["method", "using", "performed", "review", "simulation", "assay", "analysis", "model", "measure", "dataset"])
    ]
    core_methods = markdown_bullets(
        method_candidates[:3],
        limit=3,
        fallback="No stable method details could be extracted.",
    )

    research_seed = first_sentence(abstract or summary_text or title)
    research_question = ensure_question(research_seed or title or "What core question does this study ask")

    data_items = []
    if record.get("pmid"):
        data_items.append(f"PubMed PMID: {record['pmid']}")
    if record.get("doi"):
        data_items.append(f"DOI: {record['doi']}")
    if record.get("keywords"):
        data_items.append(f"Keywords: {', '.join(unique_keep_order(record.get('keywords', []))[:6])}")
    if record.get("downloaded_pdf"):
        data_items.append("A PDF was downloaded and can be used to verify the text and figures.")
    if record.get("figure_paths"):
        data_items.append(f"{len(record.get('figure_paths', []))} image files were extracted.")
    if not data_items:
        data_items.append("The available material is not enough to recover data and sample details reliably.")

    limitations = []
    if not summary_mode_indicates_fulltext(record):
        limitations.append("The current evidence relies mainly on the abstract, metadata, or visible page text, so details are limited.")
    title_text = (title + " " + abstract).lower()
    if "review" in title_text or "mini-review" in title_text:
        limitations.append("This appears to be a review article, so the strength of the conclusion depends on coverage and selection quality.")
    if any(token in title_text for token in ["simulation", "docking", "homology modeling", "in silico"]):
        limitations.append("If the method is computational, check whether there is enough experimental validation.")
    if mode == "critical":
        limitations.append("For critical reading, still verify sample size, controls, statistics, and reproducibility.")

    notes = [
        f"Evidence level: {record.get('summary_mode', 'unknown')}",
        f"Full text available: {'yes' if summary_mode_indicates_fulltext(record) else 'no'}",
        f"Figures available: {'yes' if record.get('figure_paths') else 'no'}",
    ]
    if record.get("image_url"):
        notes.append("The page includes a reusable image that can be fetched later.")
    if publisher_page_blocked(title, full_text) or page_is_cookie_wall(title, full_text):
        notes.append("The captured page looks like a security page or cookie wall rather than the article body.")
    if re.search(r"graphical abstract|highlights", f"{title} {abstract} {full_text}", re.IGNORECASE):
        notes.append("The source material mentions Graphical abstract or Highlights, which can help recover key points.")

    return {
        "paper_topic": title or first_sentence(summary_text) or "Paper topic could not be confirmed yet",
        "one_sentence_summary": first_sentence(main_findings.replace("- ", "")) or title or "No one-sentence summary available.",
        "background_context": first_sentence(abstract or summary_text) or "The available material is not enough to reconstruct the background reliably.",
        "research_question": research_question,
        "data_materials": markdown_bullets(data_items, fallback="The available material is not enough to recover data and sample details reliably."),
        "core_methods": core_methods,
        "main_findings": main_findings,
        "figure_takeaways": figure_takeaways_from_record(record),
        "strengths": markdown_bullets(
            [
                "The topic and core question can usually be reconstructed quickly from the abstract or visible text.",
                "If a PDF or extracted figures exist, the methods and results can be checked more directly." if record.get("downloaded_pdf") or record.get("figure_paths") else "The current material is enough to create a structured note scaffold.",
            ]
        ),
        "limitations": markdown_bullets(limitations, fallback="The current information is not enough for a finer limitation assessment."),
        "critical_analysis": markdown_bullets(
            [
                "In fallback mode, critical analysis should stay conservative and avoid claims beyond the abstract or visible page text.",
                "Revisit sample size, control design, statistics, and reproducibility after you return to the original paper." if mode == "critical" else "It is still worth checking sample size, controls, and statistics in the original paper.",
            ]
        ),
        "related_concepts": (
            "\n".join(f"- [[{kw.strip()}]]" for kw in unique_keep_order(record.get("keywords", []))[:4])
            if record.get("keywords")
            else "- (Not enough information to identify related concepts reliably)"
        ),
        "quick_reference": markdown_bullets(
            [
                f"Evidence level: {record.get('summary_mode', 'unknown')}",
                f"Full text/body: {'yes' if summary_mode_indicates_fulltext(record) else 'no'}",
                f"Images: {'yes' if record.get('figure_paths') else 'no'}",
            ]
        ),
        "notes": markdown_bullets(notes),
    }


def build_materials_payload(record: dict, source: str, mode: str) -> dict:
    links = source_links(record, source)
    return {
        "source": source,
        "mode": mode,
        "metadata": {
            "title": record.get("title", ""),
            "authors": record.get("authors", []),
            "journal": record.get("journal", ""),
            "pmid": record.get("pmid", ""),
            "doi": record.get("doi", ""),
            "pubmed_url": record.get("pubmed_url", ""),
            "doi_url": record.get("doi_url", ""),
            "summary_mode": record.get("summary_mode", ""),
            "keywords": record.get("keywords", []),
            "affiliations": record.get("affiliations", []),
            "image_url": record.get("image_url", ""),
            "full_text_links": record.get("full_text_links", []),
            "full_text_status": record.get("full_text_status", ""),
            "downloaded_pdf": record.get("downloaded_pdf", ""),
            "figure_items": record.get("figure_items", []),
            "figure_paths": record.get("figure_paths", []),
            "web_url": links["web_url"],
            "pdf_path": links["pdf_path"],
        },
        "abstract": record.get("abstract", ""),
        "full_text_excerpt": (record.get("full_text", "") or "")[:48000],
    }


def build_metadata_table(rows: list[tuple[str, str]]) -> str:
    lines = ["| Key | Value |", "| --- | --- |"]
    for key, value in rows:
        cell = clean_structured_text(value).replace(chr(10), "<br>").strip()
        if not cell or cell in {"", "-", "无", "N/A"}:
            continue
        lines.append(f"| {key} | {cell} |")
    return chr(10).join(lines)


def build_sources_list(items: list[tuple[str, str]]) -> str:
    lines = []
    for label, value in items:
        value = normalize_whitespace(value)
        if not value:
            continue
        lines.append(f"- {label}: {value}")
    return chr(10).join(lines) if lines else "-"





def generate_sections_with_codex(record: dict, source: str, mode: str) -> dict | None:
    materials = build_materials_payload(record, source, mode)
    tmp_dir = Path(tempfile.mkdtemp(prefix="paper_reader_"))
    material_path = tmp_dir / "materials.json"
    material_path.write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt = build_generation_prompt(material_path, mode)

    codex_cmd = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if not codex_cmd:
        return None
    codex_path = Path(codex_cmd)
    node_cmd = shutil.which("node") or shutil.which("node.exe")
    codex_js = codex_path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if node_cmd and codex_js.exists():
        cmd = [
            node_cmd,
            str(codex_js),
            "exec",
            "--skip-git-repo-check",
            "--full-auto",
            "-C",
            str(SCRIPT_DIR.parent.parent.parent),
            prompt,
        ]
    else:
        cmd = [
            codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--full-auto",
            "-C",
            str(SCRIPT_DIR.parent.parent.parent),
            prompt,
        ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=900,
        )
    except Exception:
        return None
    if result is None or result.returncode != 0:
        return None
    raw = decode_text_blob(result.stdout or b"").strip()
    if not raw:
        return None
    required = {
        "paper_topic",
        "one_sentence_summary",
        "background_context",
        "research_question",
        "data_materials",
        "core_methods",
        "main_findings",
        "figure_takeaways",
        "strengths",
        "limitations",
        "critical_analysis",
        "related_concepts",
        "quick_reference",
        "notes",
    }

    def looks_good(obj: object) -> dict | None:
        if not isinstance(obj, dict):
            return None
        if not required.issubset(obj):
            return None
        return {key: sanitize_model_text(obj.get(key, "")) for key in required}

    try:
        parsed = json.loads(raw)
        clean = looks_good(parsed)
        if clean:
            return clean
    except Exception:
        pass

    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean

    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean

    return None


WORKSPACE_ROOT = Path(os.getenv("CODEX_WORKSPACE_ROOT") or os.getcwd()).resolve()
SECTION_KEYS = [
    "paper_topic",
    "one_sentence_summary",
    "background_context",
    "research_question",
    "data_materials",
    "core_methods",
    "main_findings",
    "figure_takeaways",
    "strengths",
    "limitations",
    "critical_analysis",
    "related_concepts",
    "quick_reference",
    "notes",
]
BAD_OUTPUT_PHRASES = [
    "与题目相关的核心科学问题",
    "当前主要基于自动提取结果",
    "当前可用信息不足以支持更细的局限性判断",
    "当前回退模式下",
    "暂无一句话总结",
    "论文主题待进一步确认",
    "Graphical abstract",
    "Contents lists available at ScienceDirect",
    "One-Sentence Summary",
    "Research Question",
    "Background Context",
    "Core Methods",
    "Main Findings",
    "Quick Reference",
    "summary_mode:",
    "authors:",
    "figure_paths:",
    "figure_items:",
    "pdf_path:",
    "downloaded_pdf:",
    "web_url:",
    "source:",
    "title:",
    "doi:",
    "journal:",
    "image_url:",
    "abstract_available:",
    "full_text_excerpt_available:",
    "figure_count:",
    "evidence_note:",
    ]


def count_cjk_chars(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def count_latin_tokens(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{3,}", text or ""))


def needs_chinese_rewrite(sections: dict) -> bool:
    combined = "\n".join(str(sections.get(key, "") or "") for key in SECTION_KEYS)
    if not combined.strip():
        return True
    if looks_like_mojibake(combined):
        return True
    if re.search(r"(?im)^\s*(finding|basis|path|caption|takeaway|figure)\s*[:：]", combined):
        return True
    lowered = combined.lower()
    if any(phrase.lower() in lowered for phrase in BAD_OUTPUT_PHRASES):
        return True
    if any(token in lowered for token in ["<h1", "<h2", "<p>", "<div", "</p>", "</div>"]):
        return True
    chinese = count_cjk_chars(combined)
    latin = count_latin_tokens(combined)
    if chinese < 80 and latin > 20:
        return True
    if chinese < 150 and latin > 80:
        return True
    for key in [
        "one_sentence_summary",
        "research_question",
        "background_context",
        "core_methods",
        "main_findings",
        "critical_analysis",
        "notes",
    ]:
        text = str(sections.get(key, "") or "")
        if not text:
            continue
        if looks_like_mojibake(text):
            return True
        if any(phrase.lower() in text.lower() for phrase in BAD_OUTPUT_PHRASES):
            return True
        if text.strip() in {"Abstract.", "Summary.", "Introduction."}:
            return True
        if count_cjk_chars(text) < 20 and count_latin_tokens(text) > 8:
            return True
    return False


def parse_sections_output(raw: str) -> dict | None:
    required = set(SECTION_KEYS)

    def looks_good(obj: object) -> dict | None:
        if not isinstance(obj, dict):
            return None
        if not required.issubset(obj):
            return None
        result = {key: sanitize_model_text(obj.get(key, "")) for key in SECTION_KEYS}
        result["related_concepts"] = _normalize_related_concepts(result["related_concepts"])
        return result

    try:
        parsed = json.loads(raw)
        clean = looks_good(parsed)
        if clean:
            return clean
    except Exception:
        pass

    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean

    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean
    return None


def decode_text_blob(blob: bytes | str) -> str:
    if isinstance(blob, str):
        return blob
    if not blob:
        return ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "cp1252"):
        try:
            return blob.decode(encoding)
        except Exception:
            continue
    return blob.decode("utf-8", errors="replace")


MOJIBAKE_MARKERS = [
    "鍩",
    "鐨",
    "鏂",
    "绗",
    "鍙",
    "锛",
    "銆",
    "鈥",
    "鏈",
    "缁",
    "鎬",
]


def looks_like_mojibake(text: str) -> bool:
    sample = str(text or "")
    if not sample:
        return False
    score = sum(sample.count(token) for token in MOJIBAKE_MARKERS)
    if score >= 4:
        return True
    if ("锟" in sample or "�" in sample) and count_cjk_chars(sample) > 8:
        return True
    return False


def try_repair_mojibake(text: str) -> str:
    raw = str(text or "")
    if not looks_like_mojibake(raw):
        return raw
    candidates = [raw]
    for source_enc in ("gbk", "gb18030"):
        for target_enc in ("utf-8", "utf-8-sig"):
            try:
                repaired = raw.encode(source_enc, errors="ignore").decode(target_enc, errors="ignore")
            except Exception:
                continue
            if repaired:
                candidates.append(repaired)
    best = raw
    best_score = (-1, 10**9)
    for candidate in candidates:
        candidate_score = (
            count_cjk_chars(candidate),
            -sum(candidate.count(token) for token in MOJIBAKE_MARKERS),
        )
        if candidate_score > best_score:
            best = candidate
            best_score = candidate_score
    return best


def sanitize_model_text(text: object) -> str:
    cleaned = clean_structured_text(text)
    cleaned = try_repair_mojibake(cleaned)
    cleaned = cleaned.replace("\ufeff", "").strip()
    return cleaned


def _normalize_related_concepts(text: str) -> str:
    """Ensure each [[concept]] wiki-link occupies its own bullet line.

    LLMs sometimes output concepts inline in two common patterns:
      Pattern A (dash-separated):  - [[A]] - [[B]] - [[C]]
      Pattern B (space-separated): [[A]] [[B]] [[C]]

    Both are normalised to:
      - [[A]]
      - [[B]]
      - [[C]]
    """
    if not text or "[[" not in text:
        return text
    # Pattern A: "]] - [[" boundary
    text = re.sub(r"\]\]\s*-\s*\[\[", "]]\n- [[", text)
    # Pattern B: "]] [[" boundary (space-separated, no dash)
    text = re.sub(r"\]\]\s+\[\[", "]]\n- [[", text)
    # Ensure first item also has a leading bullet if not already present
    text = text.strip()
    if text.startswith("[["):
        text = "- " + text
    return text


def run_codex_prompt(prompt: str, cwd: Path, tmp_dir: Path, timeout: int = 900):
    codex_cmd = shutil.which("codex.cmd") or shutil.which("codex") or shutil.which("codex.exe")
    if not codex_cmd:
        return None
    try:
        result = subprocess.run(
            [
                codex_cmd,
                "exec",
                "--skip-git-repo-check",
                "--full-auto",
                "-C",
                str(cwd),
            ],
            input=prompt.encode("utf-8"),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            stdout=decode_text_blob(result.stdout or b""),
            stderr=decode_text_blob(result.stderr or b""),
        )
    except Exception:
        return None


def build_fallback_sections(record: dict, mode: str) -> dict:
    abstract = record.get("abstract", "")
    full_text = record.get("full_text", "")
    title = record.get("title", "")
    # Prefer full_text (local PDF / PMC) over abstract (PubMed metadata).
    # abstract or full_text would silently drop PDF content whenever PubMed
    # abstract is present — exactly the wrong priority for local-PDF inputs.
    rich_text = full_text if normalize_whitespace(full_text) else abstract
    summary_text = rich_text or title
    title_hint = title or "该论文"
    access_issue = publisher_page_blocked(title, full_text) or page_is_cookie_wall(title, full_text)
    evidence_level = record.get("summary_mode", "") or "未知"
    has_pdf = bool(record.get("downloaded_pdf"))
    has_figures = bool(record.get("figure_paths"))
    has_captioned_figures = bool(record.get("figure_items"))
    abstract_available = bool(normalize_whitespace(abstract))
    full_text_available = bool(normalize_whitespace(full_text)) and not access_issue

    if access_issue and not abstract_available:
        findings = "当前抓取到的主要是安全验证页或 cookie wall，不是论文正文，因此还不能可靠提炼主要结论。"
    else:
        findings = f"围绕\"{title_hint}\"的主要结果已经能看出一个大致轮廓，但还需要结合正文和图表逐条核对。"

    methods_candidates = [
        s for s in sentence_chunks(rich_text, limit=8)
        if any(token in s.lower() for token in ["method", "using", "performed", "review", "simulation", "assay", "analysis", "model"])
    ]
    methods_candidate = " ".join(methods_candidates[:2]) if methods_candidates else ""
    methods = (
        methods_candidate
        if methods_candidate and count_cjk_chars(methods_candidate) >= 12
        else f"当前材料显示作者围绕\"{title_hint}\"展开了分析，但更细的方法步骤还需要回到正文确认。"
    )

    limitations = []
    if "全文" not in evidence_level:
        limitations.append("当前主要依据摘要、网页元数据或可见页面文本，证据细节有限。")
    if access_issue:
        limitations.append("当前页面更像安全验证页或 cookie wall，正文可信度不足。")
    title_text = (title + " " + abstract).lower()
    if "review" in title_text or "mini-review" in title_text:
        limitations.append("该文属于综述类文章，结论强度依赖纳入研究的覆盖范围和质量。")
    if any(token in title_text for token in ["simulation", "docking", "homology modeling", "in silico"]):
        limitations.append("如果方法偏计算建模，需要额外关注是否有充分实验验证。")
    if mode == "critical":
        limitations.append("批判性阅读时还应继续核查样本量、对照设计、统计方法和可重复性。")

    notes = {
        "standard": ["标准阅读模式，优先保留可以复用的论文细节。"],
        "quick": ["快速浏览模式，重点是帮助判断这篇文章是否值得细读。"],
        "critical": ["批判性分析模式，更强调证据边界、方法假设和外推风险。"],
    }[mode]
    if record.get("image_url"):
        notes.append("网页里还有可引用图片，可在需要时进一步抓取。")
    if access_issue:
        notes.append("当前页面更像安全验证页或 cookie wall，正文可信度不足。")
    if re.search(r"graphical abstract|highlights", f"{title} {abstract} {full_text}", re.IGNORECASE):
        notes.append("材料里出现了 Graphical abstract 或 Highlights，适合优先回看。")

    abstract_hint = first_sentence(abstract or full_text)
    figure_takeaways = (
        figure_takeaways_from_record(record)
        if (has_figures or has_captioned_figures)
        else "- 当前没有可解释的图注或稳定图像，暂时无法给出图级 takeaway。"
    )

    if access_issue and not abstract_available and not full_text_available:
        one_sentence_summary = "当前材料主要是安全验证页或 cookie wall，尚未成功提取正文内容。"
        background_context = "需要先恢复对正文、PDF 或摘要的访问，才能判断这篇研究的背景与动机。"
        research_question = "这篇文章的研究问题目前无法从可用材料中可靠确认。"
        core_methods = "- 当前没有可靠正文，方法细节无法确认。"
        main_findings = markdown_bullets(
            [
                "当前抓取到的主要是安全验证页或 cookie wall，不是论文正文。",
                "需要重新获取摘要、PDF 或可访问的全文后，才能提炼主要结果。",
            ]
        )
        strengths = [
            f"已经记录到来源链接和证据层级: {evidence_level}",
            "后续只要恢复正文访问，就能继续补齐结构化笔记。",
        ]
        critical_analysis = markdown_bullets(
            [
                "当前不应基于这份页面文本推断论文结论。",
                "先解决访问限制，再核查样本、方法和结果图。",
            ]
        )
    else:
        one_sentence_summary = abstract_hint or f"这篇文章围绕\"{title_hint}\"展开，核心信息还需要结合正文再核实。"
        background_context = f"这项研究放在\"{title_hint}\"的背景下，主要是为了说明为什么这个问题值得继续追问。"
        research_question = f"作者想回答的是\"{title_hint}\"对应的关键机制、关系或因果链条到底是什么。"
        core_methods = methods
        main_findings = findings
        strengths = [
            "当前自动提取到了基础信息，但创新点和细节仍需要结合原文进一步核对。",
            "如果后续恢复全文或图表，笔记还能继续补强证据链。",
        ]
        critical_analysis = "在回退模式下，这份批判性分析只能作为初稿，后续仍要回到原文核查关键证据。"

    return {
        "paper_topic": title_hint or first_sentence(summary_text) or "这篇文章的主题还需要进一步确认。",
        "one_sentence_summary": one_sentence_summary,
        "background_context": background_context,
        "research_question": ensure_question(research_question),
        "data_materials": markdown_bullets(
            [
                "可用材料包括摘要、网页正文摘录和基础元数据。" if abstract_available or full_text_available else "目前只有较少的可用材料，主要是页面可见文本和基础元数据。",
                "已抓到全文级正文，可据此整理研究对象、结果和关键论证。" if full_text_available else "当前还没有稳定的全文摘录，因此细节判断需要保守。",
                "图像材料已落盘，可辅助核对正文中的关键图示。" if has_figures else "目前没有稳定图像可用于交叉核对图示结论。",
                "本地 PDF 已落盘，可回看原始版式和图注。" if has_pdf else "本次未拿到本地 PDF，只能依赖网页或 API 提供的正文材料。",
            ],
            fallback="当前材料主要来自摘要、网页元数据或可见页面文本。",
        ),
        "core_methods": core_methods,
        "main_findings": main_findings,
        "figure_takeaways": figure_takeaways,
        "strengths": markdown_bullets(
            strengths,
            fallback="当前自动提取到了基础信息，但创新点和细节仍需要结合原文进一步核对。",
        ),
        "limitations": markdown_bullets(limitations, fallback="当前可用信息还不足以支持更细的局限性判断。"),
        "critical_analysis": critical_analysis,
        "related_concepts": (
            "\n".join(f"- [[{kw.strip()}]]" for kw in unique_keep_order(record.get("keywords", []))[:4])
            if record.get("keywords")
            else "- （当前可用信息不足以可靠确定相关概念）"
        ),
        "quick_reference": markdown_bullets(
            [
                f"证据层级：{evidence_level}",
                f"摘要材料：{'有' if abstract_available else '无'}",
                f"正文摘录：{'有' if full_text_available else '无'}",
                f"图像材料：{'有' if has_figures else '无'}",
                f"访问受限：{'是' if access_issue else '否'}",
            ],
            limit=5,
            fallback="当前可用信息不足以形成可靠速览。",
        ),
        "notes": markdown_bullets(notes),
    }
def build_generation_prompt(material_path: Path, mode: str) -> str:
    mode_hint = {
        "standard": "生成一份适合 Obsidian 论文笔记的中文研究记录，厚一点、具体一点，不要摘要腔。",
        "quick": "生成一份简洁但仍有判断力的中文速览笔记，明确告诉我这篇文章值不值得细读。",
        "critical": "生成一份中文批判性笔记，重点写证据边界、方法假设、替代解释和外推风险。",
    }[mode]
    return (
        "请读取本地 JSON 材料文件，只输出一个 JSON 对象，不要解释，不要 Markdown 代码围栏。\n"
        f"材料文件: {material_path}\n"
        "你是在为 Obsidian 里的论文笔记生成结构化中文内容。\n"
        f"{mode_hint}\n"
        "硬性要求:\n"
        "1. 先看 summary_mode，再决定证据层级：如果是全文/PDF/XML，就按全文层级写；如果只是摘要/网页内容/元数据，就明确保守，不要假装看过全文。\n"
        "2. 必须优先使用材料中的 abstract、full_text_excerpt、figure_items、figure_paths、web_url、pdf_path、downloaded_pdf、summary_mode；不要只盯着标题。\n"
        "3. 如果材料里出现 Abstract.、A B S T R A C T、摘要、引言、结果、讨论、Graphical abstract、Highlights 等分节，要尽量利用这些结构，不要漏掉正文里的关键段落。\n"
        "4. 如果抓到的是 cookie wall、Just a moment、security check、请开启 JavaScript / cookies 之类页面，就要诚实说明访问受限，不要编造论文内容。\n"
        "5. 全文尽量用中文重述，不要直接复制英文摘要原句；只有专有名词、基因名、物种名、方法名等必要术语可以保留英文。\n"
        "6. 任何字段都不要写成模板占位句，如\"与题目相关的核心科学问题\"\"当前主要基于自动提取结果\"\"暂无一句话总结\"这类空话。\n"
        "7. one_sentence_summary、background_context、research_question、main_findings、critical_analysis、notes 必须包含这篇文章自己的信息。\n"
        "8. research_question 必须是一个明确的问题句，结尾要像问题，而不是题目复述。\n"
        "9. background_context 要解释\"为什么这个问题值得研究\"，不要只复述题目。\n"
        "10. main_findings 最多 5 条，优先写具体结果、比较对象、机制、数据集或样本信息；不要输出 finding: / basis: 这类中间字段；每条必须单独占一行（用真实换行符分隔），不要在同一行内用 ' - ' 连缀多条。\n"
        "11. figure_takeaways 要结合 figure_items 和 figure_paths；如果图注可用，就解释图像支持了什么结论，图像为何重要；不要输出 path: / caption: / takeaway: 这类中间字段。\n"
        "12. quick_reference 要写成 3-5 条短的检查清单式条目；每条必须单独占一行（用真实换行符分隔），不要在同一行内用 ' - ' 连缀多条。\n"
        "13. strengths 和 limitations 每条也必须单独占一行，不要内联连缀。\n"
        "14. related_concepts 给出 Obsidian 链接形式，每个概念必须单独占一行，格式严格为 - [[概念名]]，不要在同一行内用 ' - ' 连缀多个概念，例如正确格式：\n- [[comparative genomics]]\n- [[regulatory evolution]]\n"
        "15. notes 要写成可复用的研究笔记，而不是泛泛而谈的概述，优先用 bullet；每条单独占一行。\n"
        "16. 不要编造 materials.json 里没有的事实；不确定就明确写\"不足以确认\"。特别注意：物种学名、基因名、新发现的命名（如 sp. nov.）、具体统计数字，必须只来自当前材料，绝不能依赖训练记忆补全。\n"
        "17. 如果需要引用英文术语，保持最小化，不要让整句变成英文。\n"
        "18. 在 main_findings 和 quick_reference 中，对最关键的基因名、物种名、方法名或定量数值用 **加粗** 标注；每条最多加粗 1-2 处，不要滥用。\n"
        "19. 输出只允许一个 JSON 对象，且键必须严格是：\n"
        "paper_topic, one_sentence_summary, background_context, research_question, data_materials, core_methods, main_findings, figure_takeaways, strengths, limitations, critical_analysis, related_concepts, quick_reference, notes\n"
    )
def build_rewrite_prompt(material_path: Path, draft_path: Path, mode: str) -> str:
    mode_hint = {
        "standard": "把初稿重写成更自然、更完整的中文论文笔记。",
        "quick": "把初稿重写成更简洁但仍然准确的中文速览笔记。",
        "critical": "把初稿重写成更锋利的中文批判性笔记。",
    }[mode]
    return (
        "你将读取两个本地 JSON 文件：materials.json 和 draft.json。\n"
        f"materials.json: {material_path}\n"
        f"draft.json: {draft_path}\n"
        "draft.json 只是初稿，请你根据 materials.json 和 draft.json 重写最终版本。\n"
        f"{mode_hint}\n"
        "硬性要求:\n"
        "1. 只输出一个 JSON 对象，不要解释，不要代码围栏。\n"
        "2. 输出键必须与 draft.json 完全一致。\n"
        "3. 全部字段都要用自然中文改写，删除英文直出、模板句、占位句和重复句。\n"
        "4. 不要保留\"与题目相关的核心科学问题\"\"当前主要基于自动提取结果\"\"暂无一句话总结\"这类空话。\n"
        "5. 如果 draft 里某个字段太泛泛，你要结合 materials.json 补足为更具体的中文表达。\n"
        "6. 一定要重新检查 summary_mode、abstract、full_text_excerpt、figure_items、figure_paths、web_url、pdf_path、downloaded_pdf，不要把全文级材料写成摘要级材料。\n"
        "7. 如果材料里出现 cookie wall、Just a moment、Highlights、Graphical abstract 之类提示，要在限制或 notes 里明确体现。\n"
        "8. one_sentence_summary、research_question、background_context、core_methods、main_findings、critical_analysis、notes 必须像真实研究笔记，不像模板。\n"
        "9. main_findings 和 figure_takeaways 必须改写成自然中文，不要保留 finding: / basis: / path: / caption: / takeaway: 这类字段壳；每条单独占一行，不要内联连缀。\n"
        "10. 如果 figure_takeaways 需要提图号，可以写成自然句式，例如\"图1：...\"或\"这张图说明...\"。\n"
        "11. quick_reference 要保持短、像清单，不要写成散文；每条单独占一行，不要在同一行内用 ' - ' 连缀。\n"
        "12. strengths 和 limitations 每条也必须单独占一行，不要内联连缀。\n"
        "13. notes 要写成未来可复用的研究提示，尽量具体，优先 bullet；每条单独占一行。\n"
        "14. 如果英文学术术语无法自然翻成中文，可以保留最小必要英文，但整句必须是中文。\n"
        "15. 不要编造 materials.json 里没有的事实；只在已有证据上重写和润色。\n"
        "16. 在 main_findings 和 quick_reference 中，对最关键的基因名、物种名、方法名或定量数值用 **加粗** 标注；每条最多加粗 1-2 处，不要滥用。\n"
        "17. 输出只允许一个 JSON 对象，且键严格是：\n"
        "paper_topic, one_sentence_summary, background_context, research_question, data_materials, core_methods, main_findings, figure_takeaways, strengths, limitations, critical_analysis, related_concepts, quick_reference, notes\n"
    )
def is_elsevier_doi(doi: str) -> bool:
    return normalize_whitespace(doi).startswith("10.1016/")


def parse_elsevier_xml(xml_text: str, doi: str, api_url: str) -> dict:
    soup = BeautifulSoup(xml_text or "", "xml")
    texts = []
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if not name:
            continue
        parent = tag.parent
        excluded = False
        while getattr(parent, "name", None):
            parent_name = (parent.name or "").lower()
            if any(marker in parent_name for marker in ["ref", "reference", "bibliograph"]):
                excluded = True
                break
            parent = parent.parent
        if excluded:
            continue
        if name.endswith("para") or name.endswith("caption") or name == "p" or name.endswith("summary"):
            txt = clean_structured_text(tag.get_text(" ", strip=True))
            if txt and len(txt) > 20:
                texts.append(txt)
    texts = unique_keep_order(texts)
    title = ""
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.endswith("title") and len(clean_structured_text(tag.get_text(" ", strip=True))) > 10:
            title = clean_structured_text(tag.get_text(" ", strip=True))
            break
    abstract = ""
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.endswith("abstract") or name.endswith("description"):
            abstract = clean_structured_text(tag.get_text(" ", strip=True))
            if len(abstract) > 20:
                break
    journal = ""
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.endswith("publicationname") or name.endswith("source-title") or name.endswith("journal-title"):
            journal = clean_structured_text(tag.get_text(" ", strip=True))
            if journal:
                break
    web_url = ""
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        href = clean_structured_text(tag.get("href", "") or tag.get("xlink:href", "") or tag.get("url", "") or "")
        rel = clean_structured_text(tag.get("rel", "") or tag.get("type", "") or "")
        if not href:
            continue
        if "scidir" in name or "sciencedirect" in href.lower() or "science/article/pii" in href.lower():
            web_url = href
            break
        if "scidir" in rel.lower():
            web_url = href
            break
    pdf_url = ""
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        text = clean_structured_text(tag.get_text(" ", strip=True))
        href = clean_structured_text(tag.get("href", "") or tag.get("xlink:href", "") or tag.get("url", "") or "")
        if name.endswith("ucs-locator") and text:
            pdf_url = text
            break
        if not pdf_url and href and ".pdf" in href.lower():
            pdf_url = href
    authors = []
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if not name.endswith("creator"):
            continue
        text = clean_structured_text(tag.get_text(" ", strip=True))
        if text and len(text) < 120:
            authors.append(text)
    if not authors:
        fallback_authors = []
        for tag in soup.find_all(True):
            name = (tag.name or "").lower()
            if not name.endswith("author"):
                continue
            parent = tag.parent
            excluded = False
            while getattr(parent, "name", None):
                parent_name = (parent.name or "").lower()
                if any(marker in parent_name for marker in ["ref", "reference", "bibliograph"]):
                    excluded = True
                    break
                parent = parent.parent
            if excluded:
                continue
            text = clean_structured_text(tag.get_text(" ", strip=True))
            if text and len(text) < 120:
                fallback_authors.append(text)
        authors = fallback_authors
    authors = clean_author_candidates(authors)[:20]
    keywords = []
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.endswith("keyword") or name.endswith("kwd"):
            text = clean_structured_text(tag.get_text(" ", strip=True))
            if text:
                keywords.append(text)
    keywords = unique_keep_order(keywords)
    full_text = "\\n\\n".join([item for item in [title, abstract] + texts if item])
    summary_mode = "基于 Elsevier API 全文/XML" if full_text else "基于 Elsevier API XML 元数据"
    record = {
        "source_kind": "doi",
        "title": title,
        "authors": authors,
        "journal": journal,
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": f"https://doi.org/{doi}",   # clean URL — never expose api_url (contains API key)
        "abstract": abstract,
        "affiliations": [],
        "keywords": keywords,
        "full_text": full_text[:45000],
        "summary_mode": summary_mode,
        "image_url": "",
        "full_text_links": [api_url],           # api_url kept for internal fetching only
        "full_text_status": "elsevier_api",
        "acquisition_path": "Elsevier API(view=FULL)",
        "web_url": web_url or f"https://doi.org/{doi}",
    }
    if pdf_url:
        record["pdf_url"] = pdf_url
    return record


def fetch_elsevier_article(doi: str, skip_pdf_download: bool = False) -> dict:
    """Fetch article metadata and optionally full text from the Elsevier API.

    Args:
        doi: The Elsevier DOI to fetch.
        skip_pdf_download: When True, skip every attempt to download a PDF
            (direct HTTP and Playwright browser).  Use this when the caller
            already has a local copy of the PDF and only needs metadata
            (title, authors, journal, abstract).
    """
    api_key = elsevier_api_key()
    doi = parse_doi_candidate(doi)
    if not api_key or not doi or not is_elsevier_doi(doi):
        return {}
    api_url = (
        f"https://api.elsevier.com/content/article/doi/{quote(doi, safe='/')}"
        f"?apiKey={quote(api_key, safe='')}&httpAccept=text/xml&view=FULL"
    )
    raw = ""
    try:
        raw = fetch_url(
            api_url,
            headers={
                "X-ELS-APIKey": api_key,
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            },
            timeout=90,
        ).decode("utf-8", errors="replace")
    except Exception:
        # urllib may fail with SSL proxy issues; fall back to subprocess curl
        try:
            from pdf_fetcher import _elsevier_fetch_xml_via_curl
            raw = _elsevier_fetch_xml_via_curl(doi, api_key)
        except Exception:
            raw = ""
    if not raw or not raw.strip() or "<" not in raw or "article" not in raw.lower():
        return {}
    record = parse_elsevier_xml(raw, doi, api_url)
    pdf_url = normalize_whitespace(record.get("pdf_url", ""))
    if pdf_url and not skip_pdf_download:
        try:
            pdf_record = fetch_pdf_url(pdf_url, referer=api_url)
            if pdf_record:
                record = merge_fulltext_record(record, pdf_record)
                record["summary_mode"] = "基于 Elsevier API 全文/XML + PDF"
                record["acquisition_path"] = "Elsevier API(view=FULL) + PDF fallback"
        except Exception:
            pass
        if not record.get("downloaded_pdf"):
            try:
                if sync_playwright is not None:
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=not PREFER_VISIBLE_BROWSER)
                        context = browser.new_context(accept_downloads=True)
                        try:
                            browser_pdf = fetch_pdf_with_python_playwright_context(context, [pdf_url], record.get("title", "") or doi)
                        finally:
                            try:
                                context.close()
                            except Exception:
                                pass
                            try:
                                browser.close()
                            except Exception:
                                pass
                        if browser_pdf:
                            record = merge_fulltext_record(record, browser_pdf)
                            record["summary_mode"] = "基于 Elsevier API 全文/XML + PDF"
                            record["acquisition_path"] = "Elsevier API(view=FULL) + PDF fallback"
            except Exception:
                pass
    if not record.get("downloaded_pdf") or not record.get("figure_paths"):
        try:
            root = pdf_picture_root_dir()
            if root.exists():
                prefix = safe_filename(record.get("title", "") or doi)
                if prefix:
                    pdf_hits = sorted(root.glob(prefix + "*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if pdf_hits and not record.get("downloaded_pdf"):
                        record["downloaded_pdf"] = str(pdf_hits[0])
                        record.setdefault("pdf_url", str(pdf_hits[0]))
                    fig_dir = root / "figures"
                    if fig_dir.exists() and not record.get("figure_paths"):
                        fig_hits = sorted(fig_dir.glob(prefix + "-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                        if fig_hits:
                            record["figure_paths"] = [str(path) for path in fig_hits[:8]]
                            record["figure_items"] = [{"path": str(path), "caption": ""} for path in fig_hits[:8]]
        except Exception:
            pass
    return record


def fetch_doi(doi: str) -> dict:


    doi = parse_doi_candidate(doi)
    if not doi:
        return {}
    if doi.startswith("10.1101/"):
        preprint_record = fetch_preprint_record(doi)
        if preprint_record:
            return preprint_record
    elsevier_meta = {}
    url = f"https://doi.org/{quote(doi, safe='/')}"
    raw = ""
    try:
        raw = fetch_url(url).decode("utf-8", errors="replace")
    except Exception:
        pass
    meta = parse_html_metadata(url, raw) if raw else {
        "source_kind": "doi",
        "title": "",
        "authors": [],
        "journal": "",
        "pmid": "",
        "doi": doi,
        "pubmed_url": "",
        "doi_url": url,
        "abstract": "",
        "affiliations": [],
        "keywords": [],
        "full_text": "",
        "summary_mode": "基于 DOI 元数据",
        "image_url": "",
    }
    if is_elsevier_doi(doi):
        try:
            elsevier_meta = fetch_elsevier_article(doi)
            if elsevier_meta:
                meta = merge_fulltext_record(meta, elsevier_meta)
                meta["source_kind"] = "doi"
                meta["summary_mode"] = elsevier_meta.get("summary_mode", "") or "基于 Elsevier API 全文/XML"
                meta["acquisition_path"] = elsevier_meta.get("acquisition_path", "") or "Elsevier API(view=FULL)"
                meta["web_url"] = elsevier_meta.get("web_url", "") or meta.get("web_url", "")
                meta["full_text_status"] = elsevier_meta.get("full_text_status", "") or "elsevier_api"
        except Exception:
            pass
    should_try_browser = (
        (not raw)
        or publisher_page_blocked(meta.get("title", ""), meta.get("full_text", ""))
        or page_is_cookie_wall(meta.get("title", ""), meta.get("full_text", ""))
        or (not is_elsevier_doi(doi) and not looks_like_full_article(meta.get("title", ""), meta.get("full_text", "")))
        or (is_elsevier_doi(doi) and (not meta.get("downloaded_pdf") or not meta.get("figure_paths") or not browser_record_has_fulltext(meta)))
    )
    if should_try_browser and not SKIP_PLAYWRIGHT:
        # ── Try patchright-based PDF fetcher first (bot-detection bypass) ──────
        _pf_succeeded = False
        try:
            from pdf_fetcher import fetch_paper_pdf as _pf_fetch
            _pdf_root = pdf_picture_root_dir()
            _fig_dir  = _pdf_root / "figures"
            _ident    = safe_filename(meta.get("title", "") or doi)[:80]
            pf_result = _pf_fetch(
                url,
                elsevier_api_key=elsevier_api_key(),
                pdf_save_dir=_pdf_root,
                fig_save_dir=_fig_dir,
                identifier=_ident,
            )
            if pf_result and (pf_result.get("full_text") or pf_result.get("downloaded_pdf")):
                meta = merge_fulltext_record(meta, pf_result)
                if pf_result.get("summary_mode"):
                    meta["summary_mode"] = pf_result["summary_mode"]
                if pf_result.get("acquisition_path"):
                    meta["acquisition_path"] = pf_result["acquisition_path"]
                _pf_succeeded = True
        except Exception:
            _pf_succeeded = False
        # ── Fall back to standard playwright if patchright didn't get content ──
        if not _pf_succeeded:
            browser_meta = fetch_web_with_playwright(url, headed=PREFER_VISIBLE_BROWSER)
            redirect_url = normalize_whitespace(browser_meta.get("web_url", "")) if browser_meta else ""
            if (not browser_record_has_fulltext(browser_meta)) and redirect_url and redirect_url != url and "doi.org/" not in redirect_url.lower():
                retried = fetch_web_with_playwright(redirect_url, headed=PREFER_VISIBLE_BROWSER)
                if browser_record_has_fulltext(retried):
                    browser_meta = retried
                elif retried:
                    browser_meta = merge_records(browser_meta or {}, retried, prefer_new=True)
            if browser_record_has_fulltext(browser_meta):
                meta = merge_records(meta, browser_meta, prefer_new=True)
                if browser_meta.get("pdf_url"):
                    try:
                        meta = merge_records(meta, fetch_pdf_url(browser_meta["pdf_url"]), prefer_new=False)
                    except Exception:
                        pass
            elif browser_meta.get("pdf_url"):
                try:
                    meta = merge_records(meta, fetch_pdf_url(browser_meta["pdf_url"]), prefer_new=False)
                except Exception:
                    pass
    meta["doi"] = doi
    meta["doi_url"] = url
    pubmed_meta = fetch_pubmed_by_doi(doi)
    if pubmed_meta:
        meta = merge_records(meta, pubmed_meta, prefer_new=False)
        if pubmed_meta.get("full_text") or pubmed_meta.get("downloaded_pdf"):
            meta = merge_fulltext_record(meta, pubmed_meta)
    if elsevier_meta and normalize_whitespace(str(elsevier_meta.get("full_text", "") or "")):
        meta = merge_fulltext_record(meta, elsevier_meta)
        meta["source_kind"] = "doi"
        meta["summary_mode"] = elsevier_meta.get("summary_mode", "") or "基于 Elsevier API 全文/XML"
        meta["acquisition_path"] = elsevier_meta.get("acquisition_path", "") or "Elsevier API(view=FULL)"
        meta["web_url"] = elsevier_meta.get("web_url", "") or meta.get("web_url", "")
        meta["full_text_status"] = elsevier_meta.get("full_text_status", "") or "elsevier_api"
    return meta


def generate_sections_with_codex(record: dict, source: str, mode: str) -> dict | None:
    materials = build_materials_payload(record, source, mode)
    tmp_dir = Path(tempfile.mkdtemp(prefix="paper_reader_", dir=str(WORKSPACE_ROOT)))
    material_path = tmp_dir / "materials.json"
    material_path.write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    result = run_codex_prompt(build_generation_prompt(material_path, mode), WORKSPACE_ROOT, tmp_dir)
    draft = None
    if result is not None and result.returncode == 0:
        raw = (result.stdout or "").strip()
        draft = parse_sections_output(raw)
    if not draft:
        draft = build_fallback_sections(record, mode)
    else:
        draft = {key: sanitize_model_text(draft.get(key, "")) for key in SECTION_KEYS}
    if not needs_chinese_rewrite(draft):
        return draft
    draft_path = tmp_dir / "draft.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    rewrite = run_codex_prompt(build_rewrite_prompt(material_path, draft_path, mode), WORKSPACE_ROOT, tmp_dir)
    if rewrite is not None and rewrite.returncode == 0:
        rewritten = parse_sections_output((rewrite.stdout or "").strip())
        if rewritten:
            rewritten = {key: sanitize_model_text(rewritten.get(key, "")) for key in SECTION_KEYS}
            if not needs_chinese_rewrite(rewritten):
                return rewritten
    repaired = {key: sanitize_model_text(draft.get(key, "")) for key in SECTION_KEYS}
    return repaired

def choose_note_title(record: dict, source: str) -> str:
    if record.get("title"):
        return record["title"]
    kind, value = detect_source_kind(source)
    if kind == "local_pdf":
        return Path(value).stem
    if kind in {"web_url", "pdf_url"}:
        return urlparse(value).path.strip("/").split("/")[-1] or "web-paper"
    return "untitled-paper"


def parse_simple_frontmatter_value(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    try:
        return ast.literal_eval(raw)
    except Exception:
        return raw.strip('"').strip("'")


def load_note_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    frontmatter = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = parse_simple_frontmatter_value(value)
    return frontmatter


def merge_existing_note_resources(record: dict, candidate_paths: list[Path]) -> dict:
    merged = dict(record)
    if is_preprint_record(merged) and not normalize_whitespace(str(merged.get("downloaded_pdf", "") or "")) and not normalize_whitespace(str(merged.get("full_text", "") or "")):
        return merged
    best = {}
    best_score = -1
    for path in candidate_paths:
        if not path.exists():
            continue
        frontmatter = load_note_frontmatter(path)
        if not frontmatter:
            continue
        score = 0
        if frontmatter.get("publisher_pdf") or frontmatter.get("downloaded_pdf") or frontmatter.get("local_pdf"):
            score += 3
        if frontmatter.get("figure_paths"):
            score += 2
        if frontmatter.get("publisher_site"):
            score += 1
        if score > best_score:
            best = frontmatter
            best_score = score
    if best_score < 0:
        return merged

    for key in ["downloaded_pdf", "publisher_pdf", "local_pdf", "pdf_path", "publisher_site"]:
        if not normalize_whitespace(str(merged.get(key, "") or "")) and normalize_whitespace(str(best.get(key, "") or "")):
            merged[key] = best.get(key, "")

    existing_figures = best.get("figure_paths", [])
    if isinstance(existing_figures, str):
        existing_figures = [existing_figures] if normalize_whitespace(existing_figures) else []
    if existing_figures and not merged.get("figure_paths"):
        merged["figure_paths"] = existing_figures

    current_acquisition = normalize_whitespace(str(merged.get("acquisition_path", "") or ""))
    best_acquisition = normalize_whitespace(str(best.get("acquisition_path", "") or ""))
    if (
        best_acquisition
        and ("browser.download" in best_acquisition or "出版社 PDF" in best_acquisition)
        and (
            not current_acquisition
            or current_acquisition in {"网页正文提取", "PMC 网页正文提取", "PDF fallback 提取"}
        )
    ):
        merged["acquisition_path"] = best_acquisition
    elif not current_acquisition:
        merged["acquisition_path"] = normalize_whitespace(str(best.get("acquisition_path", "") or ""))
    return merged


def recover_existing_asset_paths(record: dict) -> dict:
    merged = dict(record)
    title = safe_filename(str(merged.get("title", "") or ""))
    if not title:
        return merged
    if is_preprint_record(merged) and not normalize_whitespace(str(merged.get("downloaded_pdf", "") or "")) and not normalize_whitespace(str(merged.get("full_text", "") or "")):
        return merged

    pdf_dir = pdf_picture_root_dir() / "pdfs"
    fig_dir = pdf_picture_root_dir() / "figures"

    def pick_best(paths: list[Path], preferred_tokens: list[str], reject_tokens: list[str]) -> str:
        ranked: list[tuple[int, Path]] = []
        for path in paths:
            name = path.name.lower()
            score = 0
            for token in preferred_tokens:
                if token in name:
                    score += 10
            for token in reject_tokens:
                if token in name:
                    score -= 10
            ranked.append((score, path))
        if not ranked:
            return ""
        ranked.sort(key=lambda item: (item[0], item[1].stat().st_size), reverse=True)
        return str(ranked[0][1])

    if pdf_dir.exists() and not normalize_whitespace(str(merged.get("publisher_pdf", "") or merged.get("downloaded_pdf", "") or "")):
        pdf_matches = list(pdf_dir.glob(f"{title}*.pdf"))
        chosen_pdf = pick_best(pdf_matches, ["pnas-publisher", "publisher"], ["pageprint", "print"])
        if chosen_pdf:
            merged["publisher_pdf"] = chosen_pdf
            merged["downloaded_pdf"] = merged.get("downloaded_pdf") or chosen_pdf
            merged["pdf_path"] = merged.get("pdf_path") or chosen_pdf
            merged["local_pdf"] = merged.get("local_pdf") or chosen_pdf
            merged["publisher_site"] = merged.get("publisher_site") or "pnas"
            current_acquisition = normalize_whitespace(str(merged.get("acquisition_path", "") or ""))
            if not current_acquisition or current_acquisition in {"网页正文提取", "PMC 网页正文提取", "PDF fallback 提取"}:
                merged["acquisition_path"] = "browser.download"

    current_figures = merged.get("figure_paths", [])
    if isinstance(current_figures, str):
        current_figures = [current_figures] if normalize_whitespace(current_figures) else []
    if fig_dir.exists() and not current_figures:
        fig_matches = list(fig_dir.glob(f"{title}*"))
        chosen_figure = pick_best(fig_matches, ["pnas-fig01", "fig01"], ["html-figure", "publisher-000", "publisher-001"])
        if chosen_figure:
            merged["figure_paths"] = [chosen_figure]
            merged["publisher_site"] = merged.get("publisher_site") or "pnas"
    return merged


def pick_representative_figure(record: dict) -> dict | None:
    items = record.get("figure_items", []) or []
    paths = record.get("figure_paths", []) or []
    candidates: list[dict] = []

    caption_hints: list[str] = []
    for item in items:
        caption = normalize_whitespace(item.get("caption", "") or item.get("alt", ""))
        if caption:
            caption_hints.append(caption)

    for item in items:
        raw_path = item.get("path", "")
        if not raw_path:
            continue
        img_path = Path(raw_path)
        if not img_path.exists():
            continue
        candidates.append(
            {
                "path": str(img_path),
                "caption": normalize_whitespace(item.get("caption", "") or item.get("alt", "")),
                "source_url": normalize_whitespace(item.get("source_url", "")),
                "source": normalize_whitespace(item.get("source", "")),
            }
        )

    for raw_path in paths:
        img_path = Path(raw_path)
        if not img_path.exists():
            continue
        lower_name = img_path.name.lower()
        caption = ""
        if re.search(r"(?:^|[-_ ])0*0\b|fig(?:ure)?[-_ ]0*1", lower_name, re.IGNORECASE):
            for hint in caption_hints:
                if re.search(r"\bfig(?:ure)?\.?\s*1\b", hint, re.IGNORECASE):
                    caption = hint
                    break
        candidates.append(
            {
                "path": str(img_path),
                "caption": caption,
                "source_url": "",
                "source": "pdfimage",
            }
        )

    scored: list[tuple[int, dict]] = []
    for item in candidates:
        img_path = Path(item["path"])
        size = img_path.stat().st_size
        width, height = read_png_size(img_path)
        pixels = width * height
        caption = normalize_whitespace(item.get("caption", ""))
        source_url = normalize_whitespace(item.get("source_url", ""))
        source_kind = normalize_whitespace(item.get("source", ""))
        lower_name = img_path.name.lower()
        score = size + pixels

        if source_kind == "figure":
            score += 4_000_000
        elif source_kind == "pdfimage":
            score += 2_500_000

        if source_url:
            score += 3_000_000
        if caption:
            score += 2_000_000
        if re.search(r"\bfig(?:ure)?\.?\s*1\b", caption, re.IGNORECASE):
            score += 2_500_000
        if re.search(r"(^|[-_/])fig", (source_url or lower_name), re.IGNORECASE):
            score += 1_000_000
        if re.search(r"(?:^|[-_ ])0*0\b", lower_name):
            score += 600_000

        if pixels < 120_000 or size < 20_000:
            score -= 4_500_000
        if "html-figure" in lower_name and not source_url:
            score -= 6_000_000
        if "wiley" in lower_name and not caption:
            score -= 3_000_000
        if "-page-" in lower_name:
            score -= 2_500_000
        if "logo" in lower_name or "header" in lower_name or "icon" in lower_name:
            score -= 4_000_000
        scored.append((score, item))

    if scored:
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1]
    return None


def pick_top_figures(record: dict, max_n: int = 3) -> list[dict]:
    """Return up to max_n best figures, ordered by descending quality score.

    Reuses the same scoring heuristics as pick_representative_figure but
    returns a list so callers can show multiple key figures in the note.
    """
    items = record.get("figure_items", []) or []
    paths = record.get("figure_paths", []) or []
    candidates: list[dict] = []

    caption_hints: list[str] = []
    for item in items:
        caption = normalize_whitespace(item.get("caption", "") or item.get("alt", ""))
        if caption:
            caption_hints.append(caption)

    for item in items:
        raw_path = item.get("path", "")
        if not raw_path:
            continue
        img_path = Path(raw_path)
        if not img_path.exists():
            continue
        candidates.append(
            {
                "path": str(img_path),
                "caption": normalize_whitespace(item.get("caption", "") or item.get("alt", "")),
                "source_url": normalize_whitespace(item.get("source_url", "")),
                "source": normalize_whitespace(item.get("source", "")),
            }
        )

    for raw_path in paths:
        img_path = Path(raw_path)
        if not img_path.exists():
            continue
        lower_name = img_path.name.lower()
        caption = ""
        if re.search(r"(?:^|[-_ ])0*0\b|fig(?:ure)?[-_ ]0*1", lower_name, re.IGNORECASE):
            for hint in caption_hints:
                if re.search(r"\bfig(?:ure)?\.?\s*1\b", hint, re.IGNORECASE):
                    caption = hint
                    break
        candidates.append(
            {
                "path": str(img_path),
                "caption": caption,
                "source_url": "",
                "source": "pdfimage",
            }
        )

    scored: list[tuple[int, dict]] = []
    for item in candidates:
        img_path = Path(item["path"])
        size = img_path.stat().st_size
        width, height = read_png_size(img_path)
        pixels = width * height
        caption = normalize_whitespace(item.get("caption", ""))
        source_url = normalize_whitespace(item.get("source_url", ""))
        source_kind = normalize_whitespace(item.get("source", ""))
        lower_name = img_path.name.lower()
        score = size + pixels

        if source_kind == "figure":
            score += 4_000_000
        elif source_kind == "pdfimage":
            score += 2_500_000
        if source_url:
            score += 3_000_000
        if caption:
            score += 2_000_000
        if re.search(r"\bfig(?:ure)?\.?\s*1\b", caption, re.IGNORECASE):
            score += 2_500_000
        if re.search(r"(^|[-_/])fig", (source_url or lower_name), re.IGNORECASE):
            score += 1_000_000
        if re.search(r"(?:^|[-_ ])0*0\b", lower_name):
            score += 600_000
        if pixels < 120_000 or size < 20_000:
            score -= 4_500_000
        if "html-figure" in lower_name and not source_url:
            score -= 6_000_000
        if "wiley" in lower_name and not caption:
            score -= 3_000_000
        if "-page-" in lower_name:
            score -= 2_500_000
        if "logo" in lower_name or "header" in lower_name or "icon" in lower_name:
            score -= 4_000_000
        scored.append((score, item))

    if not scored:
        return []
    scored.sort(key=lambda pair: pair[0], reverse=True)
    # Deduplicate by resolved path so we never return the same file twice
    seen_paths: set[str] = set()
    result: list[dict] = []
    for _, item in scored:
        resolved = str(Path(item["path"]).resolve())
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            result.append(item)
        if len(result) >= max_n:
            break
    return result


def find_chrome_executable() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def page_looks_like_human_check(text: str, title: str = "") -> bool:
    hay = f"{title} {text}".lower()
    markers = [
        "verify you are human",
        "security check",
        "just a moment",
        "please wait",
        "cloudflare",
        "执行安全验证",
        "请稍候",
        "真人",
    ]
    return any(marker in hay for marker in markers)


def try_solve_human_check(page) -> bool:
    solved = False
    for _ in range(3):
        try:
            body_text = normalize_whitespace(page.locator("body").inner_text(timeout=4000))
        except Exception:
            body_text = ""
        try:
            title = page.title()
        except Exception:
            title = ""
        if not page_looks_like_human_check(body_text, title):
            return solved
        clicked = False
        try:
            frame_els = page.locator("iframe")
            count = min(frame_els.count(), 6)
        except Exception:
            count = 0
        for idx in range(count):
            try:
                el = frame_els.nth(idx)
                src = (el.get_attribute("src", timeout=1000) or "").lower()
                frame_title = (el.get_attribute("title", timeout=1000) or "").lower()
                if not any(token in (src + " " + frame_title) for token in ["cloudflare", "challenge", "turnstile"]):
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(2500)
                clicked = True
                solved = True
                break
            except Exception:
                continue
        if not clicked:
            try:
                labels = page.locator("label, input[type=checkbox], [role=checkbox]")
                count = min(labels.count(), 4)
                for idx in range(count):
                    box = labels.nth(idx).bounding_box()
                    if not box:
                        continue
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.wait_for_timeout(2500)
                    clicked = True
                    solved = True
                    break
            except Exception:
                pass
        page.wait_for_timeout(2500)
    return solved


def _render_one_figure(item: dict, note_path: Path) -> str:
    """Render a single figure item to an Obsidian-compatible image embed string."""
    vault_root = obsidian_vault_path().resolve()
    img_path = Path(item.get("path", ""))
    try:
        vault_rel = img_path.resolve().relative_to(vault_root)
        return f"![[{vault_rel.as_posix()}]]"
    except Exception:
        pass
    # File is outside the vault — use a standard Markdown image link with a
    # relative path (Obsidian will still render it when the file is accessible).
    try:
        rel = Path(os.path.relpath(img_path.resolve(), note_path.parent.resolve()))
        return f"![Figure]({quote(rel.as_posix(), safe='/:')})"
    except Exception:
        return f"![Figure]({quote(img_path.resolve().as_posix(), safe='/:')})"


def _parse_takeaway_bullets(figure_takeaways: str) -> list[str]:
    """Split figure_takeaways into individual bullet strings (one per figure)."""
    bullets: list[str] = []
    for line in render_figure_takeaways_text(figure_takeaways).splitlines():
        candidate = normalize_whitespace(line).lstrip("- *").strip()
        # Skip blank lines and lines that look like figure-number headers only
        if candidate and not re.fullmatch(r"(?:Figure|图)\s*\d+", candidate, re.IGNORECASE):
            bullets.append(candidate)
    return bullets


def build_figures_section(record: dict, note_path: Path, figure_takeaways: str = "") -> str:
    """Render the single best figure, paired with its matching takeaway bullet.

    Each figure image is followed immediately by its corresponding Chinese
    takeaway description so readers see image and interpretation together.
    The full figure_takeaways text is also shown separately in the
    '## Figure Takeaways' template section — this function does NOT duplicate
    all bullets, it only inlines the one matching each figure.
    """
    figures = pick_top_figures(record, max_n=1)
    takeaway_bullets = _parse_takeaway_bullets(figure_takeaways)
    rendered: list[str] = []

    for idx, item in enumerate(figures):
        embed = _render_one_figure(item, note_path)
        rendered.append(embed)

        # Pair with takeaway: use index-matched bullet if available, else caption
        if idx < len(takeaway_bullets):
            desc = sanitize_figure_summary_text(takeaway_bullets[idx], record)
            if desc:
                rendered.append(f"中文说明：{desc}")
        else:
            caption = normalize_whitespace(item.get("caption", "") or item.get("alt", ""))
            if caption:
                if count_cjk_chars(caption) < 10 and count_latin_tokens(caption) > 6:
                    rendered.append("中文说明：这张图对应论文中的关键图像，图注细节需要结合正文进一步核对。")
                else:
                    rendered.append(f"中文说明：{caption}")

    return "\n\n".join(rendered) if rendered else "当前没有稳定提取到可展示的图像。"


def infer_publisher_site(record: dict) -> str:
    doi = parse_doi_candidate(normalize_whitespace(str(record.get("doi", "") or "")))
    web_url = normalize_whitespace(str(record.get("web_url", "") or ""))
    doi_url = normalize_whitespace(str(record.get("doi_url", "") or ""))
    hay = " ".join([doi, web_url, doi_url]).lower()
    if doi.startswith("10.1093/") or "academic.oup.com" in hay or "oup.com" in hay:
        return "oup"
    if doi.startswith("10.1016/") or "sciencedirect.com" in hay or "elsevier" in hay:
        return "elsevier"
    if doi.startswith("10.1038/") or "nature.com" in hay:
        return "nature"
    if doi.startswith("10.1073/") or "pnas.org" in hay:
        return "pnas"
    if "springer.com" in hay or "link.springer.com" in hay or doi.startswith("10.1186/"):
        return "springer"
    if "wiley.com" in hay or "onlinelibrary.wiley.com" in hay:
        return "wiley"
    if "pmc.ncbi.nlm.nih.gov" in hay:
        return "pmc"
    return ""


def infer_acquisition_path(record: dict) -> str:
    source_kind = normalize_whitespace(str(record.get("source_kind", "") or ""))
    web_url = normalize_whitespace(str(record.get("web_url", "") or "")).lower()
    has_pdf = bool(normalize_whitespace(str(record.get("downloaded_pdf", "") or "")))
    has_full_text = bool(normalize_whitespace(str(record.get("full_text", "") or "")))
    existing = normalize_whitespace(str(record.get("acquisition_path", "") or ""))
    existing_low = existing.lower()

    if existing and any(token in existing_low for token in ["api", "xml", "openalex", "crossref"]):
        if not has_pdf and "pdf" in existing_low:
            existing = re.sub(r"\s*\+\s*pdf[^+]*$", "", existing, flags=re.IGNORECASE).strip()
            existing_low = existing.lower()
        if has_pdf and "pdf" not in existing_low:
            return f"{existing} + PDF 落盘"
        return existing

    if source_kind == "local_pdf":
        return "本地 PDF 文本提取"
    if source_kind == "pdf_url":
        return "出版社 PDF 下载后提取"
    if source_kind == "web_url":
        if "pmc.ncbi.nlm.nih.gov" in web_url:
            return "PMC 网页正文提取" if not has_pdf else "PMC 网页正文提取 + PDF 落盘"
        if "cell.com" in web_url:
            return "网页正文提取（Cell full text）" if not has_pdf else "网页正文提取（Cell full text） + PDF 落盘"
        if "link.springer.com" in web_url or "springer.com" in web_url:
            return "网页正文提取（Springer）" if not has_pdf else "网页正文提取（Springer） + PDF 落盘"
        if "pnas.org" in web_url:
            return "网页正文提取（PNAS）" if not has_pdf else "网页正文提取（PNAS） + browser.download"
        return "网页正文提取" if not has_pdf else "网页正文提取 + PDF 落盘"
    if has_pdf:
        return existing or "PDF fallback 提取"
    if has_full_text:
        return existing or "网页正文提取"
    return existing


def ensure_named_pdf_path(pdf_path_raw: str, title: str, doi: str) -> str:
    pdf_path_raw = normalize_whitespace(pdf_path_raw)
    if not pdf_path_raw:
        return ""
    path = Path(pdf_path_raw)
    try:
        if not path.exists() or path.suffix.lower() != ".pdf":
            return pdf_path_raw
    except Exception:
        return pdf_path_raw

    lower_name = path.name.lower()
    generic_name = (
        lower_name.startswith("publisher - ")
        or lower_name.startswith("https-")
        or lower_name.startswith("download")
        or "just a moment" in lower_name
    )
    if not generic_name:
        return str(path)

    doi_part = safe_filename(doi.replace("/", "_")) if doi else ""
    title_part = safe_filename(title) if title else ""
    stem = " - ".join(part for part in [doi_part, title_part] if part) or title_part or doi_part
    if not stem:
        return str(path)
    target = path.with_name(f"{stem}.pdf")
    if target == path:
        return str(path)
    try:
        if target.exists():
            return str(target)
        path.replace(target)
        return str(target)
    except Exception:
        return str(path)


def normalize_note_record_for_output(record: dict) -> dict:
    normalized = dict(record)
    doi = parse_doi_candidate(normalize_whitespace(str(normalized.get("doi", "") or "")))
    preprint_mode = is_preprint_record(normalized)

    for text_key in [
        "title",
        "journal",
        "summary_mode",
        "acquisition_path",
        "web_url",
        "doi_url",
        "pubmed_url",
    ]:
        if text_key in normalized:
            normalized[text_key] = sanitize_model_text(normalized.get(text_key, ""))

    def _is_valid_pdf_path(path_raw: str) -> bool:
        path_raw = normalize_whitespace(path_raw)
        if not path_raw:
            return False
        if looks_like_browser_print_pdf_path(path_raw):
            return False
        path = Path(path_raw)
        try:
            if not path.exists() or path.stat().st_size <= 1024 or not looks_like_pdf_bytes(path.read_bytes()[:1024]):
                return False
            blocked_name_markers = [
                "just a moment",
                "cloudflare",
                "captcha",
                "recaptcha",
                "security check",
                "正在检查您的浏览器",
                "安全验证",
            ]
            lower_name = path.name.lower()
            if any(marker in lower_name for marker in blocked_name_markers):
                return False
            try:
                preview = normalize_whitespace((PdfReader(str(path)).pages[0].extract_text() or "")[:1500]).lower()
            except Exception:
                preview = ""
            blocked_text_markers = [
                "just a moment",
                "cloudflare",
                "enable javascript and cookies",
                "security verification",
                "captcha",
                "recaptcha",
                "performing security verification",
                "please wait while we verify",
                "正在检查您的浏览器",
                "请启用 javascript 和 cookie",
            ]
            if preview and any(marker in preview for marker in blocked_text_markers):
                return False
            return True
        except Exception:
            return False

    downloaded_pdf = normalize_whitespace(str(normalized.get("downloaded_pdf", "") or ""))
    if downloaded_pdf:
        valid_pdf = _is_valid_pdf_path(downloaded_pdf)
        if not valid_pdf:
            normalized["downloaded_pdf"] = ""
            if normalize_whitespace(str(normalized.get("publisher_pdf", "") or "")) == downloaded_pdf:
                normalized["publisher_pdf"] = ""
            if normalize_whitespace(str(normalized.get("pdf_path", "") or "")) == downloaded_pdf:
                normalized["pdf_path"] = ""
            if normalize_whitespace(str(normalized.get("local_pdf", "") or "")) == downloaded_pdf:
                normalized["local_pdf"] = ""
    for key in ["publisher_pdf", "pdf_path", "local_pdf"]:
        raw = normalize_whitespace(str(normalized.get(key, "") or ""))
        if raw and not _is_valid_pdf_path(raw):
            normalized[key] = ""

    authors = normalized.get("authors", [])
    if isinstance(authors, str):
        authors = [item.strip() for item in re.split(r"[;,]", authors) if item.strip()]
    authors = [sanitize_model_text(item) for item in (list(authors) if isinstance(authors, (list, tuple, set)) else [str(authors)])]
    authors = clean_author_candidates(list(authors) if isinstance(authors, (list, tuple, set)) else [str(authors)])

    noisy_authors = (
        not authors
        or len(authors) > 12
        or any("View all articles by this author" in item for item in authors)
        or any(len(item) > 80 for item in authors)
    )

    if doi:
        fetchers = (fetch_crossref_by_doi, fetch_openalex_by_doi) if preprint_mode else (fetch_crossref_by_doi, fetch_openalex_by_doi, fetch_pubmed_by_doi)
        for fetcher in fetchers:
            try:
                extra = fetcher(doi) or {}
            except Exception:
                extra = {}
            if not extra:
                continue

            extra_authors = extra.get("authors", [])
            if isinstance(extra_authors, str):
                extra_authors = [item.strip() for item in re.split(r"[;,]", extra_authors) if item.strip()]
            extra_authors = clean_author_candidates(
                list(extra_authors) if isinstance(extra_authors, (list, tuple, set)) else [str(extra_authors)]
            )
            if extra_authors and (
                noisy_authors
                or len(authors) <= 1
                or len(extra_authors) >= max(len(authors) + 2, 3)
            ):
                authors = extra_authors
                noisy_authors = False

            if not normalized.get("journal") and extra.get("journal"):
                normalized["journal"] = extra["journal"]
            if (not normalize_whitespace(str(normalized.get("title", "") or ""))) or normalize_whitespace(str(normalized.get("title", "") or "")).lower() in {"untitled-paper", "no title", "[no-title]"}:
                if extra.get("title"):
                    normalized["title"] = extra["title"]
            if not normalized.get("year") and extra.get("year"):
                normalized["year"] = extra["year"]
            if not normalized.get("abstract") and extra.get("abstract"):
                normalized["abstract"] = extra["abstract"]
            if not normalized.get("summary_mode") and extra.get("summary_mode"):
                normalized["summary_mode"] = extra["summary_mode"]
            if not normalized.get("acquisition_path") and extra.get("acquisition_path"):
                normalized["acquisition_path"] = extra["acquisition_path"]
            if (not preprint_mode) and extra.get("downloaded_pdf") and not normalized.get("downloaded_pdf"):
                normalized["downloaded_pdf"] = extra["downloaded_pdf"]
            if (not preprint_mode) and extra.get("pdf_url") and not normalized.get("pdf_url"):
                normalized["pdf_url"] = extra["pdf_url"]
            if extra.get("web_url") and not normalized.get("web_url"):
                normalized["web_url"] = extra["web_url"]

    normalized["acquisition_path"] = infer_acquisition_path(normalized)

    normalized["publisher_site"] = infer_publisher_site(normalized) or normalize_whitespace(str(normalized.get("publisher_site", "") or ""))
    named_pdf = ensure_named_pdf_path(
        normalize_whitespace(str(normalized.get("downloaded_pdf", "") or normalized.get("publisher_pdf", "") or normalized.get("pdf_path", "") or "")),
        normalize_whitespace(str(normalized.get("title", "") or "")),
        doi,
    )
    if named_pdf:
        normalized["downloaded_pdf"] = named_pdf
        normalized["publisher_pdf"] = named_pdf
        normalized["pdf_path"] = named_pdf
    if normalized.get("downloaded_pdf") and not normalized.get("publisher_pdf"):
        normalized["publisher_pdf"] = normalized.get("downloaded_pdf", "")
    if preprint_mode and not normalize_whitespace(str(normalized.get("downloaded_pdf", "") or "")) and not normalize_whitespace(str(normalized.get("full_text", "") or "")):
        normalized["figure_paths"] = []
        normalized["figure_items"] = []
    figure_paths = normalized.get("figure_paths", [])
    if isinstance(figure_paths, str):
        figure_paths = [normalize_whitespace(figure_paths)] if normalize_whitespace(figure_paths) else []
    elif isinstance(figure_paths, (tuple, set)):
        figure_paths = list(figure_paths)
    normalized["figure_paths"] = [item for item in unique_keep_order([str(item) for item in figure_paths]) if item]
    normalized["authors"] = unique_keep_order(authors)[:12]
    normalized.setdefault("year", "")
    return normalized


def save_note(record: dict, source: str, mode: str, created: str) -> Path:
    record = normalize_note_record_for_output(record)
    title = choose_note_title(record, source)
    identifier = record.get("pmid") or record.get("doi") or created
    identifier = safe_filename(str(identifier).replace("/", "_"))
    note_name = f"{identifier} - {safe_filename(title)}.md"
    out_path = paper_notes_dir() / "_inbox" / note_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doi = parse_doi_candidate(normalize_whitespace(str(record.get("doi", "") or "")))
    candidate_paths = [out_path]
    if doi:
        for extra_path in sorted((paper_notes_dir() / "_inbox").glob("*.md")):
            if extra_path == out_path:
                continue
            frontmatter = load_note_frontmatter(extra_path)
            if parse_doi_candidate(normalize_whitespace(str(frontmatter.get("doi", "") or ""))) == doi:
                candidate_paths.append(extra_path)
    record = merge_existing_note_resources(record, candidate_paths)
    record = recover_existing_asset_paths(record)
    record = normalize_note_record_for_output(record)

    sections_raw = generate_sections_with_codex(record, source, mode) or build_fallback_sections(record, mode)
    sections = {key: render_note_section_text(key, sections_raw.get(key, "")) for key in SECTION_KEYS}
    authors = record.get("authors", [])
    if isinstance(authors, str):
        authors = [item.strip() for item in authors.split(",") if item.strip()]
    authors = unique_keep_order(clean_author_candidates(list(authors) if isinstance(authors, (list, tuple, set)) else [str(authors)]))
    if len(authors) > 12:
        authors = authors[:12]

    tags = ["paper-note", "paper-reader", mode]
    if record.get("source_kind"):
        tags.append(record["source_kind"])
    links = source_links(record, source)
    pubmed_link = format_markdown_link("PubMed", links["pubmed_url"])
    doi_link = format_markdown_link("DOI", links["doi_url"])
    web_link = format_markdown_link("Web", links["web_url"])
    pdf_link = format_markdown_link("PDF", links["pdf_path"])

    def _yaml_path(val) -> str:
        """Normalise a filesystem path for YAML double-quoted strings.

        Backslashes in YAML double-quoted scalars are escape characters — a raw
        Windows path like E:\\Obsidian\\... triggers invalid escape sequences
        (\O, \A, etc.) that some YAML parsers reject.  Converting to forward
        slashes is universally safe and still accepted by Windows APIs.
        """
        raw = normalize_whitespace(str(val or ""))
        if not raw:
            return ""
        path = Path(raw)
        try:
            vault_rel = path.resolve().relative_to(obsidian_vault_path().resolve())
            return vault_rel.as_posix()
        except Exception:
            return raw.replace("\\", "/")

    frontmatter_rows = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"authors: {json.dumps(authors, ensure_ascii=False)}",
        f"year: {str(record.get('year') or created[:4])}",
        f'tags: [{", ".join(tags)}]',
        f'journal: "{(record.get("journal", "") or "").replace(chr(34), chr(39))}"',
        f'pmid: "{record.get("pmid", "")}"',
        f'doi: "{record.get("doi", "")}"',
        f'pubmed_url: "{links["pubmed_url"]}"',
        f'doi_url: "{links["doi_url"] or links["web_url"]}"',
        f'web_url: "{links["web_url"]}"',
        f'local_pdf: "{_yaml_path(links["pdf_path"])}"',
        f'pdf_path: "{_yaml_path(links["pdf_path"])}"',
        f'downloaded_pdf: "{_yaml_path(normalize_whitespace(record.get("downloaded_pdf", "")))}"',
        f'acquisition_path: "{_yaml_path(normalize_whitespace(record.get("acquisition_path", "")))}"',
        f'summary_mode: "{record.get("summary_mode", "")}"',
        f'analysis_mode: "{mode}"',
        f'date: "{created}"',
        "---",
    ]
    frontmatter = chr(10).join(frontmatter_rows)
    metadata_table = build_metadata_table(
        [
            ("Authors", ", ".join(authors)),
            ("Journal", record.get("journal", "") or ""),
            ("Year", str(record.get("year") or created[:4])),
            ("PMID", record.get("pmid", "") or ""),
            ("DOI", record.get("doi", "") or ""),
            ("Source kind", record.get("source_kind", "") or ""),
            ("PubMed", pubmed_link),
            ("DOI page", doi_link or web_link),
            ("Web page", web_link),
            ("Local PDF", pdf_link),
            ("Acquisition path", record.get("acquisition_path", "") or ""),
            ("Affiliations", "; ".join(record.get("affiliations", []))),
        ]
    )
    sources_list = build_sources_list(
        [
            ("PubMed", pubmed_link),
            ("DOI", doi_link),
            ("Web", web_link),
            ("Local PDF", pdf_link),
        ]
    )

    template = (SCRIPT_DIR / "assets" / "paper-note-template.md").read_text(encoding="utf-8")
    content = template.format(
        frontmatter=frontmatter,
        title=title.replace('"', "'"),
        authors=json.dumps(authors, ensure_ascii=False),
        year=str(record.get("year") or created[:4]),
        journal=(record.get("journal", "") or "").replace('"', "'"),
        metadata_table=metadata_table,
        sources_list=sources_list,
        pmid=record.get("pmid", ""),
        doi=record.get("doi", ""),
        pubmed_url=pubmed_link,
        doi_url=doi_link or web_link,
        web_url=web_link,
        local_pdf=pdf_link,
        tags=", ".join(tags),
        zotero_path="",
        summary_mode=record.get("summary_mode", ""),
        analysis_mode=mode,
        date=created,
        authors_text=", ".join(authors),
        affiliations="; ".join(record.get("affiliations", [])),
        paper_topic=sections.get("paper_topic", ""),
        one_sentence_summary=sections.get("one_sentence_summary", ""),
        background_context=sections.get("background_context", ""),
        research_question=sections.get("research_question", ""),
        data_materials=sections.get("data_materials", ""),
        core_methods=sections.get("core_methods", ""),
        main_findings=sections.get("main_findings", ""),
        figure_takeaways=sections.get("figure_takeaways", ""),
        strengths=sections.get("strengths", ""),
        limitations=sections.get("limitations", ""),
        critical_analysis=sections.get("critical_analysis", ""),
        related_concepts=sections.get("related_concepts", ""),
        quick_reference=sections.get("quick_reference", ""),
        notes=sections.get("notes", ""),
        figures=build_figures_section(record, out_path, sections.get("figure_takeaways", "")),
    )
    out_path.write_text(content, encoding="utf-8-sig")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="PubMed URL / DOI / PDF path / http(s) URL")
    parser.add_argument("--mode", choices=["standard", "quick", "critical"], default="standard")
    parser.add_argument("--date", default="")
    parser.add_argument("--prefer-visible-browser", action="store_true")
    parser.add_argument("--pdf-dir", default="")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="For local PDF inputs: skip all network metadata enrichment "
             "(OpenAlex / PubMed / Elsevier API).  Extract text and figures "
             "from the PDF only.  Fastest option when you already know the paper.",
    )
    parser.add_argument(
        "--no-playwright",
        action="store_true",
        help="Skip all Playwright browser automation.  PMC free-text (pure API) "
             "is still attempted; only publisher-side browser steps are skipped. "
             "Use this in automated pipelines where Chromium may not be installed.",
    )
    args = parser.parse_args()

    global PREFER_VISIBLE_BROWSER, SKIP_PLAYWRIGHT, PDF_SAVE_DIR
    PREFER_VISIBLE_BROWSER = bool(args.prefer_visible_browser)
    SKIP_PLAYWRIGHT = bool(args.no_playwright)
    PDF_SAVE_DIR = Path(args.pdf_dir) if args.pdf_dir else pdf_picture_root_dir()

    created = args.date or date.today().isoformat()

    # Wire --local-only into resolve_source for the local_pdf code path
    if args.local_only:
        kind, value = detect_source_kind(args.source)
        if kind == "local_pdf":
            record = read_local_pdf(value, enrich_metadata=False)
        else:
            record = resolve_source(args.source)
    else:
        record = resolve_source(args.source)
    note_path = save_note(record, args.source, args.mode, created)
    GEN_PAPER_MOC.main()

    result = {
        "source": args.source,
        "mode": args.mode,
        "summary_mode": record.get("summary_mode", ""),
        "note_path": str(note_path),
        "title": choose_note_title(record, args.source),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

