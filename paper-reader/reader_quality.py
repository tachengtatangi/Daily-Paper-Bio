"""Evidence-quality and page-access heuristics for paper-reader."""

from __future__ import annotations

import re

from reader_text import article_sentences, normalize_whitespace

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
