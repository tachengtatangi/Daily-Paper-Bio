#!/usr/bin/env python3
"""
fetch_and_score.py - Phase 1+2: Fetch, score, dedup, select top papers from PubMed.

Usage:
    python fetch_and_score.py
    python fetch_and_score.py --date 2026-02-25
    python fetch_and_score.py --days 7

JSON is printed to stdout and also written to temp_file_path("daily_papers_top30.json").

Stderr: progress logs. Stdout: JSON array of top papers.
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from user_config import daily_papers_config, daily_papers_dir, ncbi_api_key as _ncbi_api_key, temp_file_path
from cas_quartiles import lookup_journal
from library_profile import load_or_build_library_profile

_CONFIG = daily_papers_config()

BASE_KEYWORDS = list(_CONFIG["keywords"])
BASE_NEGATIVE_KEYWORDS = list(_CONFIG["negative_keywords"])
BASE_REJECTED_JOURNALS = list(_CONFIG.get("rejected_journals", []))
BASE_DOMAIN_BOOST_KEYWORDS = list(_CONFIG["domain_boost_keywords"])
KEYWORDS = list(BASE_KEYWORDS)
NEGATIVE_KEYWORDS = list(BASE_NEGATIVE_KEYWORDS)
REJECTED_JOURNALS = list(BASE_REJECTED_JOURNALS)
DOMAIN_BOOST_KEYWORDS = list(BASE_DOMAIN_BOOST_KEYWORDS)
PROFILE_BOOST_KEYWORDS: list[str] = []   # auto-extracted from PDF library; merged into DOMAIN_BOOST_KEYWORDS
PROFILE_PREFERRED_JOURNALS: list[str] = []
MIN_SCORE = _CONFIG["min_score"]
TOP_N = _CONFIG["top_n"]
SEARCH_RETMAX = int(_CONFIG.get("search_retmax", 0))
# Hard ceiling on total PubMed fetch across all days (0 = no cap).
# Prevents 7-day runs from accidentally fetching 35 000+ papers.
_cap_raw = _CONFIG.get("search_retmax_total_cap", 15000)
try:
    SEARCH_RETMAX_TOTAL_CAP = max(0, int(_cap_raw or 0))
except (TypeError, ValueError):
    SEARCH_RETMAX_TOTAL_CAP = 15000
# Broad domain terms used for PubMed remote esearch — analogous to arxiv_categories /
# biorxiv_categories in the original skill.  These cast a wide net over the research
# domain; KEYWORDS above are used only for local scoring.
# Each entry may include a PubMed field tag, e.g. "comparative genomics[tiab]".
# Entries without a tag default to [tiab] (Title/Abstract search).
# CAS 分区阈值：1=只保留 Q1；2=保留 Q1+Q2；3=保留 Q1~Q3；4=全部接受。
# 默认 1（与原行为一致，仅保留 Q1）。
try:
    MIN_QUARTILE = int(_CONFIG.get("min_quartile", 1) or 1)
except (TypeError, ValueError):
    MIN_QUARTILE = 1
MIN_QUARTILE = max(1, min(4, MIN_QUARTILE))
FILTER_AUDIT = {
    "rejected_negative_keyword": [],
    "rejected_journal": [],
    "rejected_quartile": [],
    "rejected_no_keyword": [],
    "removed_history": [],
    "removed_duplicate": [],
    "below_min_score": [],
}
PUBMED_FETCH_STATS = {
    "query": "",
    "keywords": [],
    "esearch_count": 0,
    "fetched_id_count": 0,
    "after_scoring": 0,
}

DAILYPAPERS_DIR = daily_papers_dir()
HISTORY_PATH = DAILYPAPERS_DIR / ".history.json"

PUBMED_SEARCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_SUMMARY_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_UI_BASE = "https://pubmed.ncbi.nlm.nih.gov"

# ── NCBI rate limiting ────────────────────────────────────────────────────────
# Anonymous: max 3 req/s.  With api_key: max 10 req/s.
NCBI_API_KEY: str = _ncbi_api_key()
NCBI_REQUEST_INTERVAL: float = 0.11 if NCBI_API_KEY else 0.34  # seconds between requests


class _NcbiRateLimiter:
    """Thread-safe NCBI rate limiter.

    Each call to acquire() reserves a future "fire time" slot under a lock,
    then sleeps *outside* the lock.  Multiple threads therefore sleep concurrently
    toward their own reserved slots without blocking each other, while still
    honoring the global per-second request budget.

    Example with interval=0.11s, 5 workers:
        thread-1 reserves t=0.11  ─┐
        thread-2 reserves t=0.22  ─┤  (all reserve instantly inside lock)
        thread-3 reserves t=0.33  ─┤  then sleep concurrently
        thread-4 reserves t=0.44  ─┤
        thread-5 reserves t=0.55  ─┘
        → all 5 requests fire within 0.55 s, IO overlaps → ~4-5× throughput
    """

    def __init__(self, interval: float) -> None:
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0
        self.interval = interval

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            fire_at = max(now, self._next_allowed)
            self._next_allowed = fire_at + self.interval
        wait = fire_at - time.time()
        if wait > 0:
            time.sleep(wait)


_NCBI_RATE_LIMITER = _NcbiRateLimiter(NCBI_REQUEST_INTERVAL)


def _ncbi_sleep() -> None:
    """Backward-compatible wrapper — delegates to the thread-safe rate limiter."""
    _NCBI_RATE_LIMITER.acquire()


# ── Parallel efetch workers ───────────────────────────────────────────────────
# Number of concurrent threads for PubMed efetch batches.
# Each thread respects the shared _NCBI_RATE_LIMITER, so the aggregate
# request rate stays within NCBI's limit while IO waits overlap.
# Set to 1 to restore fully serial behaviour.
try:
    EFETCH_WORKERS = max(1, int(_CONFIG.get("efetch_workers", 5) or 5))
except (TypeError, ValueError):
    EFETCH_WORKERS = 5

def _add_ncbi_key(params: dict) -> dict:
    """Add api_key and tool/email to an E-utilities params dict if key is set."""
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    params.setdefault("tool", os.getenv("NCBI_TOOL_NAME", "daily-papers"))
    params.setdefault("email", os.getenv("NCBI_TOOL_EMAIL", "daily-papers@example.com"))
    return params
BIORXIV_DETAILS_BASE = "https://api.biorxiv.org/details/biorxiv"
USER_AGENT = "daily-papers-pubmed/1.0"
FETCH_BATCH_SIZE = 100
BIORXIV_ENABLED = bool(_CONFIG.get("biorxiv_enabled", True))
BIORXIV_RETMAX = int(_CONFIG.get("biorxiv_retmax", 300))
_biorxiv_cap_raw = _CONFIG.get("biorxiv_retmax_total_cap", 3000)
try:
    BIORXIV_RETMAX_TOTAL_CAP = max(0, int(_biorxiv_cap_raw or 0))
except (TypeError, ValueError):
    BIORXIV_RETMAX_TOTAL_CAP = 3000
try:
    BIORXIV_TIMEOUT = max(5, int(_CONFIG.get("biorxiv_timeout", 30) or 30))
except (TypeError, ValueError):
    BIORXIV_TIMEOUT = 30
BIORXIV_CATEGORIES = {
    re.sub(r"\s+", " ", str(cat or "").strip()).lower()
    for cat in _CONFIG.get("biorxiv_categories", [])
    if str(cat or "").strip()
}
BIORXIV_FETCH_STATS = {
    "raw_total": 0,
    "after_category": 0,
    "after_scoring": 0,
    "fetch_failed_count": 0,
    "pagination_pages": 0,
    "daily_fallback_triggered": False,
    "daily_fallback_count": 0,
}

FORMULA_RESIDUE_RE = re.compile(r"\[\s*formula:\s*see\s*text\s*\]|\bformula:\s*see\s*text\b", re.IGNORECASE)
CTRL_RESIDUE_RE = re.compile(r"[\uFFFD\u200B-\u200D\u2060]+")

# Keyword variant aliases: empty by default.
# normalize_token already handles plurals, suffixes, and hyphen variants automatically.
# "de novo genes" → ["de","novo","gene"] == ["de","novo","gene"] (after fix: "es" rule
# skipped for 5-char words, falls through to "s" → "gene"). No manual variants needed.
CANONICAL_VARIANTS: dict[str, list[str]] = {}


def unique_keep_case(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items or []:
        clean = re.sub(r"\s+", " ", str(item or "")).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def clean_display_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    clean = FORMULA_RESIDUE_RE.sub(" ", clean)
    clean = CTRL_RESIDUE_RE.sub("", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .;:,")
    return clean


def reset_filter_audit() -> None:
    for key in FILTER_AUDIT:
        FILTER_AUDIT[key] = []


# Lock protecting all writes to FILTER_AUDIT (parse_pubmed_xml runs in parallel).
_FILTER_AUDIT_LOCK = threading.Lock()


def record_filter_event(bucket: str, paper: dict, details: dict | None = None) -> None:
    payload = {
        "title": paper.get("title", ""),
        "source": paper.get("source", ""),
        "date": paper.get("date", ""),
        "score": paper.get("score", 0),
        "matched_keywords": list(paper.get("matched_keywords", []) or []),
        "matched_boost_keywords": list(paper.get("matched_boost_keywords", []) or []),
        "profile_hits": list(paper.get("profile_hits", []) or []),
        "journal": paper.get("journal", ""),
        "pmid": paper.get("pmid", ""),
        "doi": paper.get("doi", ""),
        "url": paper.get("url", ""),
    }
    if details:
        payload.update(details)
    with _FILTER_AUDIT_LOCK:
        if bucket not in FILTER_AUDIT:
            FILTER_AUDIT[bucket] = []
        FILTER_AUDIT[bucket].append(payload)


def filter_audit_snapshot() -> dict:
    return {key: list(value) for key, value in FILTER_AUDIT.items()}


def apply_library_profile(refresh: bool = False) -> dict:
    """Load the PDF-library profile and merge its boost keywords into scoring globals.

    The profile produces a single ``domain_boost_keywords`` list (up to 30 terms,
    bigram-first).  All of these are merged into DOMAIN_BOOST_KEYWORDS alongside the
    user's manually curated list.  User-curated entries always take priority (dedup
    keeps first occurrence).

    Merge order (first wins in dedup):
        BASE_DOMAIN_BOOST_KEYWORDS   ← user-curated, highest priority
        PROFILE_BOOST_KEYWORDS       ← auto-extracted from PDF library
    """
    global KEYWORDS, NEGATIVE_KEYWORDS, REJECTED_JOURNALS, DOMAIN_BOOST_KEYWORDS
    global PROFILE_BOOST_KEYWORDS, PROFILE_PREFERRED_JOURNALS
    profile = load_or_build_library_profile(_CONFIG, refresh=refresh)
    PROFILE_BOOST_KEYWORDS = unique_keep_case(profile.get("domain_boost_keywords", []))
    PROFILE_PREFERRED_JOURNALS = unique_keep_case(profile.get("preferred_journals", []))
    KEYWORDS = unique_keep_case(BASE_KEYWORDS)
    NEGATIVE_KEYWORDS = list(BASE_NEGATIVE_KEYWORDS)
    REJECTED_JOURNALS = list(BASE_REJECTED_JOURNALS)
    DOMAIN_BOOST_KEYWORDS = unique_keep_case(BASE_DOMAIN_BOOST_KEYWORDS + PROFILE_BOOST_KEYWORDS)
    return profile


def normalize_token(token: str) -> str:
    """Lowercase + strip common English plural endings only.

    Earlier versions stripped a long suffix list (-ing/-ed/-ly/-ation/-ment …)
    which over-stemmed in ways that hurt matching:

        family     → fami     (kills "family" match)
        transition → trans    (collapses to "trans")
        analyzing  → analyz   (doesn't line up with analysis → analys)

    We now only strip plural forms (ies / es / s) with proper guards.  If a
    keyword really needs verb/adjective variants (e.g. enhancer / enhanced),
    add them explicitly to ``CANONICAL_VARIANTS``.
    """
    token = re.sub(r"[^a-z0-9]+", "", (token or "").lower())
    if len(token) <= 3:
        return token

    # studies → study, categories → category  (stem must be ≥2 before "ies")
    # Exempt Latin-origin invariant words where -ies is part of the stem, not a plural suffix.
    _IES_EXEMPT = {"species", "series", "rabies", "scabies", "facies"}
    if token.endswith("ies") and len(token) >= 5 and token not in _IES_EXEMPT:
        return token[:-3] + "y"

    # boxes / processes / dishes → box / process / dish
    # Only strip "es" when the stem ends in s / x / z / ch / sh, matching the
    # English rule that adds "es" after sibilant consonants.
    if token.endswith("es") and len(token) >= 5:
        stem = token[:-2]
        if stem.endswith(("s", "x", "z", "ch", "sh")):
            return stem
        # otherwise fall through to "s" rule ("genes" → "gene")

    # genes → gene, receptors → receptor
    # Guards:
    #   - don't strip "ss" endings (process, address, analysis… wait: analysis
    #     ends in "is", handled below)
    #   - don't strip "us" endings (virus, nucleus, locus) — plural is -i/-era
    #   - don't strip "is" endings (analysis, basis, thesis) — irregular plurals
    if (
        token.endswith("s")
        and not token.endswith(("ss", "us", "is"))
        and len(token) >= 4
    ):
        return token[:-1]

    return token


def keyword_to_norm_tokens(keyword: str) -> list[str]:
    return [normalize_token(part) for part in re.split(r"[^A-Za-z0-9]+", keyword or "") if normalize_token(part)]


def text_to_norm_tokens(text: str) -> list[str]:
    return [normalize_token(part) for part in re.split(r"[^A-Za-z0-9]+", text or "") if normalize_token(part)]


def token_matches_variant(text_token: str, keyword_token: str) -> bool:
    if not text_token or not keyword_token:
        return False
    return text_token == keyword_token


def contains_keyword_variant(text_tokens: list[str], keyword: str) -> bool:
    variants = CANONICAL_VARIANTS.get(keyword, [keyword])
    for variant in variants:
        kw_tokens = keyword_to_norm_tokens(variant)
        if not kw_tokens:
            continue
        if len(kw_tokens) == 1:
            if any(token_matches_variant(token, kw_tokens[0]) for token in text_tokens):
                return True
            continue
        for start in range(0, len(text_tokens) - len(kw_tokens) + 1):
            window = text_tokens[start : start + len(kw_tokens)]
            if all(token_matches_variant(token, kw) for token, kw in zip(window, kw_tokens)):
                return True
    return False


def contains_keyword_strict(text_tokens: list[str], keyword: str) -> bool:
    variants = CANONICAL_VARIANTS.get(keyword, [keyword])
    for variant in variants:
        kw_tokens = keyword_to_norm_tokens(variant)
        if not kw_tokens:
            continue
        if len(kw_tokens) == 1:
            if kw_tokens[0] in text_tokens:
                return True
            continue
        for start in range(0, len(text_tokens) - len(kw_tokens) + 1):
            window = text_tokens[start : start + len(kw_tokens)]
            if window == kw_tokens:
                return True
    return False


def collect_keyword_matches(paper: dict, keywords: list[str]) -> list[str]:
    title_tokens = text_to_norm_tokens(paper.get("title", ""))
    abstract_tokens = text_to_norm_tokens(paper.get("abstract", ""))
    keyword_tokens = text_to_norm_tokens(" ".join(paper.get("keywords", [])))
    all_tokens = title_tokens + abstract_tokens + keyword_tokens
    matched = []
    for kw in keywords:
        if contains_keyword_variant(all_tokens, kw):
            matched.append(kw)
    return dedupe_overlapping_keywords(matched)


def dedupe_overlapping_keywords(keywords: list[str]) -> list[str]:
    cleaned = unique_keep_case(keywords)
    ordered = sorted(
        cleaned,
        key=lambda kw: (
            -len(keyword_to_norm_tokens(kw)),
            -len(kw),
            kw.lower(),
        ),
    )
    kept: list[str] = []
    kept_token_lists: list[list[str]] = []
    for kw in ordered:
        tokens = keyword_to_norm_tokens(kw)
        if not tokens:
            continue
        overlapped = False
        for existing_tokens in kept_token_lists:
            if len(tokens) >= len(existing_tokens):
                continue
            for start in range(0, len(existing_tokens) - len(tokens) + 1):
                if existing_tokens[start : start + len(tokens)] == tokens:
                    overlapped = True
                    break
            if overlapped:
                break
        if overlapped:
            continue
        kept.append(kw)
        kept_token_lists.append(tokens)
    return sorted(kept, key=lambda kw: keywords.index(kw))


def parse_keywords_override(raw: str) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    if any(sep in raw for sep in [",", "，", ";", "；", "\n"]):
        parts = re.split(r"[,，;；\n]+", raw)
    else:
        parts = raw.split()
    return [part.strip() for part in parts if part.strip()]


def score_paper(paper: dict) -> int:
    """Score a paper purely from user-config keywords — no hardcoded domain terms.

    Scoring rules:
      +3  per keyword matched in title
      +1  per keyword matched in abstract / paper keywords (not already in title)
      +2  if ≥2 domain_boost keywords matched anywhere
      +1  if exactly 1 domain_boost keyword matched anywhere
      +1  bonus if any domain_boost keyword appears in the title
      -999 (reject) if a negative_journal matches the journal
      -999 (reject) if a negative_keyword fires
    """
    title_tokens = text_to_norm_tokens(paper.get("title", ""))
    abstract_tokens = text_to_norm_tokens(paper.get("abstract", ""))
    paper_keyword_tokens = text_to_norm_tokens(" ".join(paper.get("keywords", [])))
    journal_tokens = text_to_norm_tokens(paper.get("journal", ""))
    full_tokens = title_tokens + abstract_tokens + paper_keyword_tokens + journal_tokens

    for bad_journal in REJECTED_JOURNALS:
        if contains_keyword_strict(journal_tokens, bad_journal):
            record_filter_event("rejected_journal", paper, {"rejected_journal": bad_journal})
            return -999

    title_matches = dedupe_overlapping_keywords(
        [kw for kw in KEYWORDS if contains_keyword_variant(title_tokens, kw)]
    )
    abstract_matches = dedupe_overlapping_keywords(
        [
            kw
            for kw in KEYWORDS
            if kw not in title_matches
            and contains_keyword_variant(abstract_tokens + paper_keyword_tokens, kw)
        ]
    )

    # Domain boost keywords also act as "bridge" terms for negative-override logic.
    domain_matches = dedupe_overlapping_keywords(
        [kw for kw in DOMAIN_BOOST_KEYWORDS if contains_keyword_variant(full_tokens, kw)]
    )
    domain_title_matches = dedupe_overlapping_keywords(
        [kw for kw in DOMAIN_BOOST_KEYWORDS if contains_keyword_variant(title_tokens, kw)]
    )

    negative_hits = [neg for neg in NEGATIVE_KEYWORDS if contains_keyword_strict(full_tokens, neg)]
    if negative_hits:
        record_filter_event("rejected_negative_keyword", paper, {"negative_hits": negative_hits})
        return -999

    score = len(title_matches) * 3 + len(abstract_matches) * 1

    domain_hits = len(domain_matches)
    if domain_hits >= 2:
        score += 2
    elif domain_hits == 1:
        score += 1

    if domain_title_matches:
        score += 1

    return score


def fetch_text_with_meta(url: str, timeout: int = 60, retries: int = 3, backoff_base: float = 2.0) -> tuple[str, str]:
    last_exc = None
    last_kind = "error"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace"), "ok"
        except HTTPError as exc:
            last_exc = exc
            last_kind = f"http_{exc.code}"
            if exc.code == 404:
                break
            if exc.code < 500 and exc.code != 429:
                break
        except TimeoutError as exc:
            last_exc = exc
            last_kind = "timeout"
        except URLError as exc:
            last_exc = exc
            msg = str(getattr(exc, "reason", exc)).lower()
            last_kind = "timeout" if "timed out" in msg else "urlerror"
        except OSError as exc:
            last_exc = exc
            msg = str(exc).lower()
            # "no such file or directory" can appear on Windows when a socket
            # connection fails mid-transfer (Errno 2, not a real filesystem error).
            if "timed out" in msg or "no such file or directory" in msg:
                last_kind = "timeout"
            else:
                last_kind = "oserror"
        except Exception as exc:
            last_exc = exc
            last_kind = exc.__class__.__name__.lower()
            break
        if attempt < retries - 1:
            time.sleep(backoff_base * (attempt + 1))
    print(f"  [WARN] fetch failed {url}: {last_exc}", file=sys.stderr)
    return "", last_kind


def fetch_text(url: str, timeout: int = 60) -> str:
    raw, _kind = fetch_text_with_meta(url, timeout=timeout)
    return raw


def fetch_json_with_meta(url: str, timeout: int = 60, retries: int = 3, backoff_base: float = 2.0) -> tuple[dict, str]:
    raw, kind = fetch_text_with_meta(url, timeout=timeout, retries=retries, backoff_base=backoff_base)
    if not raw:
        return {}, kind
    try:
        return json.loads(raw), kind
    except json.JSONDecodeError as exc:
        print(f"  [WARN] JSON parse failed {url}: {exc}", file=sys.stderr)
        return {}, "json"


def fetch_json(url: str, timeout: int = 60) -> dict:
    payload, _kind = fetch_json_with_meta(url, timeout=timeout)
    return payload


def build_pubmed_query() -> str:
    """Build a domain-agnostic PubMed esearch query.

    Design mirrors the original skill's arxiv_categories approach:
      - Remote query: broadest possible net — all journal articles with abstracts
      - CAS quartile filter: applied locally after efetch (keeps only Q1/Q2/…)
      - Keyword pre-filter: applied locally — paper must have ≥1 keyword match
      - Full scoring: applied locally — produces the final ranked top-N list

    No topic terms are baked into the remote query, so this works for any
    research domain without modification.  Volume is controlled by search_retmax.
    """
    return "journal article[pt] AND hasabstract[text]"


def esearch_pubmed(start_date, end_date, retmax: int) -> list[str]:
    query = build_pubmed_query()
    PUBMED_FETCH_STATS.update({
        "query": query,
        "keywords": list(KEYWORDS),
        "esearch_count": 0,
        "fetched_id_count": 0,
        "after_scoring": 0,
    })
    base_params = _add_ncbi_key({
        "db": "pubmed",
        "term": query,
        "mindate": start_date.isoformat(),
        "maxdate": end_date.isoformat(),
        "datetype": "pdat",
        "sort": "pub date",
        "retmode": "json",
    })

    probe_params = dict(base_params)
    probe_params["retmax"] = "0"
    probe_url = f"{PUBMED_SEARCH_BASE}?{urlencode(probe_params)}"
    _ncbi_sleep()
    raw = fetch_text(probe_url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  [WARN] esearch JSON parse error: {exc}", file=sys.stderr)
        return []

    count = int(data.get("esearchresult", {}).get("count", 0))
    PUBMED_FETCH_STATS["esearch_count"] = count
    if count <= 0:
        print("  PubMed esearch returned 0 IDs", file=sys.stderr)
        return []

    target = count if retmax <= 0 else min(count, retmax)
    ids: list[str] = []
    step = 10000
    for retstart in range(0, target, step):
        page_params = dict(base_params)
        page_params["retstart"] = str(retstart)
        page_params["retmax"] = str(min(step, target - retstart))
        page_url = f"{PUBMED_SEARCH_BASE}?{urlencode(page_params)}"
        _ncbi_sleep()
        page_raw = fetch_text(page_url)
        if not page_raw:
            continue
        try:
            page_data = json.loads(page_raw)
        except json.JSONDecodeError as exc:
            print(f"  [WARN] esearch page JSON parse error at {retstart}: {exc}", file=sys.stderr)
            continue
        ids.extend(page_data.get("esearchresult", {}).get("idlist", []))

    print(f"  PubMed esearch returned {count} IDs, fetched {len(ids)} IDs", file=sys.stderr)
    PUBMED_FETCH_STATS["fetched_id_count"] = len(ids)
    return ids


def chunked(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def text_or_empty(node) -> str:
    return node.text.strip() if node is not None and node.text else ""


def normalize_journal_for_lookup(journal: str) -> list[str]:
    raw = clean_display_text(journal)
    if not raw:
        return []
    variants = [raw]
    # PubMed often appends aliases like "Current biology : CB".
    primary = re.split(r"\s+:\s+|\s*=\s*", raw, maxsplit=1)[0].strip()
    if primary and primary not in variants:
        variants.append(primary)
    # Strip trailing subtitle after semicolon if present.
    semistrip = raw.split(";", 1)[0].strip()
    if semistrip and semistrip not in variants:
        variants.append(semistrip)
    return variants


def collect_abstract(article) -> str:
    abstract = article.find("Abstract")
    if abstract is None:
        return ""
    parts = []
    for child in abstract.findall("AbstractText"):
        label = child.attrib.get("Label", "").strip()
        text = "".join(child.itertext()).strip()
        if not text:
            continue
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts).strip()


def parse_pubmed_xml(xml_text: str) -> list[dict]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"  [WARN] efetch XML parse error: {exc}", file=sys.stderr)
        return []

    # Build prefilter term list once per batch (not once per article).
    # KEYWORDS + DOMAIN_BOOST_KEYWORDS are read-only after initialisation,
    # so this is safe to compute outside the loop even under parallel efetch.
    _prefilter_terms = KEYWORDS + DOMAIN_BOOST_KEYWORDS

    papers = []
    for article in root.findall("PubmedArticle"):
        medline = article.find("MedlineCitation")
        pubmed = article.find("PubmedData")
        if medline is None:
            continue

        pmid = text_or_empty(medline.find("PMID"))
        article_node = medline.find("Article")
        if article_node is None or not pmid:
            continue

        title = clean_display_text("".join(article_node.find("ArticleTitle").itertext()) if article_node.find("ArticleTitle") is not None else "")
        abstract = collect_abstract(article_node)

        author_names = []
        affiliations = set()
        author_list = article_node.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                collective = text_or_empty(author.find("CollectiveName"))
                if collective:
                    author_names.append(collective)
                else:
                    last = text_or_empty(author.find("LastName"))
                    fore = text_or_empty(author.find("ForeName"))
                    if last or fore:
                        author_names.append(" ".join(part for part in [fore, last] if part))
                for aff in author.findall("AffiliationInfo/Affiliation"):
                    aff_text = text_or_empty(aff)
                    if aff_text:
                        affiliations.add(aff_text)

        journal = text_or_empty(article_node.find("Journal/Title"))
        cas_info = {}
        for variant in normalize_journal_for_lookup(journal):
            cas_info = lookup_journal(variant) or {}
            if cas_info:
                break
        try:
            q_value = int(cas_info.get("quartile", 99)) if cas_info else 99
        except (TypeError, ValueError):
            q_value = 99
        if q_value > MIN_QUARTILE:
            record_filter_event(
                "rejected_quartile",
                {
                    "title": title,
                    "journal": journal,
                    "pmid": pmid,
                    "date": "",
                    "score": 0,
                    "source": "pubmed",
                    "url": f"{PUBMED_UI_BASE}/{pmid}/",
                },
                {"quartile": q_value, "min_quartile": MIN_QUARTILE},
            )
            continue

        # ── Keyword pre-filter ────────────────────────────────────────────────
        # Fast gate analogous to the original skill's category-then-score design:
        # fetch broadly (all Q≤N journal articles) → drop papers with zero keyword
        # overlap before the expensive DOI/keyword/scoring pass.
        # Uses the same token normalization + CANONICAL_VARIANTS as score_paper,
        # so "genes" matches "gene" and user-defined aliases are respected.
        # Checks keywords + domain_boost_keywords (which already includes
        # profile_boost_keywords after apply_library_profile()).
        # _prefilter_terms is built once before the loop (see above).
        if _prefilter_terms:
            title_tokens = text_to_norm_tokens(title)
            abstract_tokens = text_to_norm_tokens(abstract)
            all_tokens = title_tokens + abstract_tokens
            if not any(
                contains_keyword_variant(all_tokens, kw)
                for kw in _prefilter_terms
            ):
                record_filter_event(
                    "rejected_no_keyword",
                    {
                        "title": title,
                        "journal": journal,
                        "pmid": pmid,
                        "date": "",
                        "score": 0,
                        "source": "pubmed",
                        "url": f"{PUBMED_UI_BASE}/{pmid}/",
                    },
                )
                continue

        # Date priority:
        # 1. ArticleDate[@DateType="Electronic"] — epub/online date; matches pdat esearch window.
        #    Papers found by "datetype=pdat mindate=2025-01-31" have their epub date in that
        #    range, even if the journal issue date says "2025-May".  Displaying the epub date
        #    avoids the confusing mismatch between the search window and the shown date.
        # 2. History/PubMedPubDate[@PubStatus="pubmed"] — when PubMed indexed the record.
        # 3. JournalIssue/PubDate — print publication date (may be months in the future).
        epub_date = ""
        epub_node = article_node.find("ArticleDate[@DateType='Electronic']")
        if epub_node is not None:
            year = text_or_empty(epub_node.find("Year"))
            month = text_or_empty(epub_node.find("Month"))
            day = text_or_empty(epub_node.find("Day"))
            epub_date = "-".join(part for part in [year, month, day] if part)

        entrez_date = ""
        if pubmed is not None:
            for hist in pubmed.findall("History/PubMedPubDate"):
                if hist.attrib.get("PubStatus") in {"pubmed", "entrez"}:
                    year = text_or_empty(hist.find("Year"))
                    month = text_or_empty(hist.find("Month"))
                    day = text_or_empty(hist.find("Day"))
                    entrez_date = "-".join(part for part in [year, month, day] if part)
                    if entrez_date:
                        break

        journal_pub_date = ""
        pubdate_node = article_node.find("Journal/JournalIssue/PubDate")
        if pubdate_node is not None:
            year = text_or_empty(pubdate_node.find("Year"))
            month = text_or_empty(pubdate_node.find("Month"))
            day = text_or_empty(pubdate_node.find("Day"))
            journal_pub_date = "-".join(part for part in [year, month, day] if part)

        pub_date = epub_date or entrez_date or journal_pub_date

        doi = ""
        pmc_id = ""
        if pubmed is not None:
            for article_id in pubmed.findall("ArticleIdList/ArticleId"):
                id_type = article_id.attrib.get("IdType", "")
                if id_type == "doi" and not doi:
                    doi = text_or_empty(article_id)
                elif id_type == "pmc" and not pmc_id:
                    pmc_id = text_or_empty(article_id)

        keywords = []
        for kw in medline.findall("KeywordList/Keyword"):
            kw_text = "".join(kw.itertext()).strip()
            if kw_text:
                keywords.append(kw_text)
        if not keywords:
            for mesh in medline.findall("MeshHeadingList/MeshHeading/DescriptorName")[:8]:
                mesh_text = "".join(mesh.itertext()).strip()
                if mesh_text:
                    keywords.append(mesh_text)

        url = f"{PUBMED_UI_BASE}/{pmid}/"
        pdf = f"https://doi.org/{doi}" if doi else ""
        paper = {
            "id": pmid,
            "pmid": pmid,
            "title": title,
            "authors": ", ".join(author_names),
            "affiliations": "; ".join(sorted(affiliations)),
            "abstract": abstract,
            "url": url,
            "pdf": pdf,
            "date": pub_date,
            "score": 0,
            "category": journal,
            "source": "pubmed",
            "doi": doi,
            "pmc_id": pmc_id,
            "journal": journal,
            "cas_quartile": cas_info.get("quartile", ""),
            "cas_top": cas_info.get("top", ""),
            "keywords": keywords,
            "matched_keywords": collect_keyword_matches(
                {
                    "title": title,
                    "abstract": abstract,
                    "keywords": keywords,
                },
                KEYWORDS,
            ),
            "matched_boost_keywords": collect_keyword_matches(
                {
                    "title": title,
                    "abstract": abstract,
                    "keywords": keywords,
                },
                DOMAIN_BOOST_KEYWORDS,
            ),
            "profile_hits": collect_keyword_matches(
                {
                    "title": title,
                    "abstract": abstract,
                    "keywords": keywords,
                },
                PROFILE_BOOST_KEYWORDS,
            ),
            "preferred_journal_hits": [journal] if any(contains_keyword_strict(text_to_norm_tokens(journal), kw) for kw in PROFILE_PREFERRED_JOURNALS) else [],
            "hf_upvotes": 0,
        }
        paper["score"] = score_paper(paper)
        if paper["score"] >= 0:
            papers.append(paper)
    return papers


def split_biorxiv_authors(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if ";" in text:
        parts = [part.strip() for part in text.split(";")]
    elif " and " in text:
        parts = [part.strip() for part in text.split(" and ")]
    else:
        parts = [text]
    return [part for part in parts if part]


def normalize_biorxiv_item(item: dict) -> dict | None:
    title = clean_display_text(item.get("title") or "")
    abstract = re.sub(r"\s+", " ", (item.get("abstract") or "")).strip()
    doi = (item.get("doi") or "").strip()
    if not title or not doi:
        return None
    server = (item.get("server") or "biorxiv").strip().lower()
    version = str(item.get("version") or item.get("version_num") or "1").strip()
    article_url = f"https://www.biorxiv.org/content/{doi}v{version}"
    category = (item.get("category") or "").strip()
    authors_list = split_biorxiv_authors(item.get("authors") or "")
    keywords: list[str] = []
    paper = {
        "id": f"{server}:{doi}v{version}",
        "pmid": "",
        "title": title,
        "authors": ", ".join(authors_list),
        "affiliations": "",
        "abstract": abstract,
        "url": article_url,
        "pdf": f"{article_url}.full.pdf",
        "date": (item.get("date") or "").strip(),
        "score": 0,
        "category": category or "bioRxiv",
        "source": server,
        "doi": doi,
        "journal": "bioRxiv" if server == "biorxiv" else server,
        "cas_quartile": "",
        "cas_top": "",
        "keywords": keywords,
        "matched_keywords": collect_keyword_matches({"title": title, "abstract": abstract, "keywords": keywords}, KEYWORDS),
        "matched_boost_keywords": collect_keyword_matches({"title": title, "abstract": abstract, "keywords": keywords}, DOMAIN_BOOST_KEYWORDS),
        "profile_hits": collect_keyword_matches({"title": title, "abstract": abstract, "keywords": keywords}, PROFILE_BOOST_KEYWORDS),
        "preferred_journal_hits": [],
        "hf_upvotes": 0,
    }
    paper["score"] = score_paper(paper)
    return paper if paper["score"] >= 0 else None


def biorxiv_category_param(category: str) -> str:
    text = re.sub(r"\s+", " ", str(category or "").strip()).lower()
    if not text:
        return ""
    return text.replace(" ", "_")


def reset_biorxiv_fetch_stats() -> None:
    BIORXIV_FETCH_STATS.update({
        "raw_total": 0,
        "after_category": 0,
        "after_scoring": 0,
        "fetch_failed_count": 0,
        "pagination_pages": 0,
        "daily_fallback_triggered": False,
        "daily_fallback_count": 0,
    })


def category_allowed(category: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(category or "").strip()).lower()
    if not BIORXIV_CATEGORIES:
        return True
    return normalized in BIORXIV_CATEGORIES


def fetch_biorxiv_interval(start_date, end_date, timeout: int | None = None, retries: int | None = None, retmax: int | None = None) -> tuple[list[dict], bool]:
    batch_size = 100
    papers: list[dict] = []
    seen_ids: set[str] = set()
    start = start_date.isoformat()
    end = end_date.isoformat()
    had_failure = False
    # Use the caller-supplied timeout, or the config-derived BIORXIV_TIMEOUT.
    # Multi-day runs get 2× the base timeout to compensate for larger payloads.
    base_timeout = BIORXIV_TIMEOUT
    timeout = timeout if timeout is not None else (base_timeout * 2 if start_date != end_date else base_timeout)
    retries = retries if retries is not None else (2 if start_date != end_date else 1)
    # Use caller-supplied retmax (already scaled by days + capped) or fall back to global.
    effective_retmax = max(0, retmax if retmax is not None else BIORXIV_RETMAX)
    if effective_retmax == 0:
        print("  bioRxiv disabled by biorxiv_retmax=0", file=sys.stderr)
        return [], False

    def collect_collection(collection: list[dict]) -> None:
        BIORXIV_FETCH_STATS["pagination_pages"] += 1
        BIORXIV_FETCH_STATS["raw_total"] += len(collection)
        for item in collection:
            if not isinstance(item, dict):
                continue
            category = (item.get("category") or "").strip()
            if not category_allowed(category):
                continue
            BIORXIV_FETCH_STATS["after_category"] += 1
            paper = normalize_biorxiv_item(item)
            if not paper:
                continue
            if paper["id"] in seen_ids:
                continue
            seen_ids.add(paper["id"])
            papers.append(paper)

    def fetch_cursor(cursor: int):
        path = f"{BIORXIV_DETAILS_BASE}/{start}/{end}/{cursor}"
        payload, kind = fetch_json_with_meta(path, timeout=timeout, retries=retries, backoff_base=3.0)
        return cursor, payload, kind

    cursor0, payload0, kind0 = fetch_cursor(0)
    collection0 = payload0.get("collection") if isinstance(payload0, dict) else None
    if not isinstance(collection0, list):
        if kind0 != "ok":
            BIORXIV_FETCH_STATS["fetch_failed_count"] += 1
            had_failure = True
        return papers, had_failure
    if not collection0:
        return papers, had_failure

    collect_collection(collection0)
    message0 = payload0.get("messages", [{}])[0] if isinstance(payload0, dict) else {}
    try:
        total = int(message0.get("total") or len(collection0))
    except Exception:
        total = len(collection0)
    max_items = min(total, effective_retmax)
    cursors = list(range(batch_size, max_items, batch_size))
    if not cursors:
        return papers, had_failure

    max_workers = 3 if start_date != end_date else 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_cursor, cursor) for cursor in cursors]
        for future in concurrent.futures.as_completed(futures):
            cursor, payload, kind = future.result()
            collection = payload.get("collection") if isinstance(payload, dict) else None
            if not isinstance(collection, list):
                if kind != "ok":
                    BIORXIV_FETCH_STATS["fetch_failed_count"] += 1
                    had_failure = True
                continue
            if not collection:
                continue
            collect_collection(collection)
    return papers, had_failure


def fetch_biorxiv_interval_daily_recovery(start_date, end_date) -> list[dict]:
    papers: list[dict] = []
    seen_ids: set[str] = set()
    current = start_date
    BIORXIV_FETCH_STATS["daily_fallback_triggered"] = True
    while current <= end_date:
        BIORXIV_FETCH_STATS["daily_fallback_count"] += 1
        day_papers, day_failed = fetch_biorxiv_interval(current, current, timeout=30, retries=1, retmax=BIORXIV_RETMAX)
        if day_failed:
            current += timedelta(days=1)
            continue
        for paper in day_papers:
            if paper["id"] in seen_ids:
                continue
            seen_ids.add(paper["id"])
            papers.append(paper)
        current += timedelta(days=1)
    return papers


def fetch_biorxiv_papers(start_date, end_date, days: int = 1) -> list[dict]:
    if not BIORXIV_ENABLED:
        print("  bioRxiv disabled by config", file=sys.stderr)
        return []
    # Scale retmax linearly with days (mirrors original skill's 400*days → 3000 cap).
    # biorxiv_retmax is a per-day budget; biorxiv_retmax_total_cap is the ceiling.
    effective_retmax = (BIORXIV_RETMAX * max(1, days)) if BIORXIV_RETMAX > 0 else 0
    if BIORXIV_RETMAX_TOTAL_CAP > 0 and effective_retmax > BIORXIV_RETMAX_TOTAL_CAP:
        effective_retmax = BIORXIV_RETMAX_TOTAL_CAP
        print(
            f"  bioRxiv retmax capped: {BIORXIV_RETMAX}/day × {days} day(s) → capped at {BIORXIV_RETMAX_TOTAL_CAP} (biorxiv_retmax_total_cap)",
            file=sys.stderr,
        )
    else:
        print(
            f"  bioRxiv retmax: {BIORXIV_RETMAX}/day × {days} day(s) = {effective_retmax or 'unlimited'}",
            file=sys.stderr,
        )
    reset_biorxiv_fetch_stats()
    papers, had_failure = fetch_biorxiv_interval(start_date, end_date, retmax=effective_retmax)
    if had_failure and not papers and start_date != end_date:
        papers = fetch_biorxiv_interval_daily_recovery(start_date, end_date)
    papers.sort(key=lambda item: (item.get("score", 0), item.get("date", "")), reverse=True)
    BIORXIV_FETCH_STATS["after_scoring"] = len(papers)
    print(f"  bioRxiv: {len(papers)} papers after scoring", file=sys.stderr)
    return papers


def fetch_pubmed_papers(start_date, end_date, days: int = 1) -> list[dict]:
    # Scale retmax linearly with days (mirrors original skill's 400*days approach).
    # search_retmax is treated as a per-day budget; 0 means unlimited.
    # search_retmax_total_cap applies a hard ceiling (0 = no cap), preventing
    # runaway multi-day queries (e.g. 7 days × 5000 = 35 000 without a cap).
    effective_retmax = (SEARCH_RETMAX * max(1, days)) if SEARCH_RETMAX > 0 else 0
    if SEARCH_RETMAX_TOTAL_CAP > 0 and effective_retmax > SEARCH_RETMAX_TOTAL_CAP:
        effective_retmax = SEARCH_RETMAX_TOTAL_CAP
        print(
            f"  PubMed retmax capped: {SEARCH_RETMAX}/day × {days} day(s) → capped at {SEARCH_RETMAX_TOTAL_CAP} (search_retmax_total_cap)",
            file=sys.stderr,
        )
    else:
        print(
            f"  PubMed retmax: {SEARCH_RETMAX}/day × {days} day(s) = {effective_retmax or 'unlimited'}",
            file=sys.stderr,
        )
    ids = esearch_pubmed(start_date, end_date, effective_retmax)
    batches = list(chunked(ids, FETCH_BATCH_SIZE))
    n_batches = len(batches)
    print(
        f"  PubMed efetch: {n_batches} batches × {FETCH_BATCH_SIZE} IDs, "
        f"workers={EFETCH_WORKERS} (set efetch_workers=1 for serial)",
        file=sys.stderr,
    )

    # ── Per-batch fetch helper ────────────────────────────────────────────────
    def _fetch_batch(batch: list[str]) -> list[dict]:
        params = _add_ncbi_key({
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
        })
        url = f"{PUBMED_FETCH_BASE}?{urlencode(params)}"
        _NCBI_RATE_LIMITER.acquire()          # thread-safe slot reservation
        xml_text = fetch_text(url)
        return parse_pubmed_xml(xml_text)     # parse_pubmed_xml is thread-safe
                                              # (record_filter_event uses _FILTER_AUDIT_LOCK)

    # ── Parallel efetch ───────────────────────────────────────────────────────
    papers: list[dict] = []
    if EFETCH_WORKERS <= 1:
        # Serial fallback — useful for debugging or when NCBI key is absent.
        for batch in batches:
            papers.extend(_fetch_batch(batch))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=EFETCH_WORKERS) as exe:
            futures = [exe.submit(_fetch_batch, b) for b in batches]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    papers.extend(fut.result())
                except Exception as exc:
                    print(f"  [WARN] efetch batch failed: {exc}", file=sys.stderr)

    papers.sort(key=lambda item: (item.get("score", 0), item.get("date", "")), reverse=True)
    PUBMED_FETCH_STATS["after_scoring"] = len(papers)
    print(f"  PubMed: {len(papers)} papers after scoring", file=sys.stderr)
    return papers


def load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def merge_and_dedup(papers: list[dict], target_date, days: int = 1, top_n: int = TOP_N) -> list[dict]:
    history = load_history()
    history_ids = {item.get("id"): item.get("date", "unknown") for item in history if item.get("id")}

    deduped = []
    removed = 0
    seen_keys: set[tuple] = set()
    for paper in papers:
        paper_id = paper.get("id") or paper.get("pmid") or paper.get("doi") or paper.get("url")
        if not paper_id:
            continue
        dedup_key = (
            paper.get("source", ""),
            paper.get("doi", ""),
            paper.get("pmid", ""),
            paper.get("title", "").strip().lower(),
        )
        if dedup_key in seen_keys:
            record_filter_event("removed_duplicate", paper, {"dedup_key": list(dedup_key)})
            continue
        seen_keys.add(dedup_key)
        # History dedup applies in both single-day and multi-day modes: if we
        # already recommended this paper within the rolling 30-day window,
        # don't recommend it again regardless of the current window length.
        if paper_id in history_ids:
            removed += 1
            record_filter_event("removed_history", paper, {"last_recommend_date": history_ids[paper_id]})
            continue
        paper["id"] = paper_id
        deduped.append(paper)

    candidates = []
    for paper in deduped:
        if paper.get("score", 0) >= MIN_SCORE:
            candidates.append(paper)
        else:
            record_filter_event("below_min_score", paper, {"min_score_threshold": MIN_SCORE})
    candidates.sort(
        key=lambda item: (
            1 if item.get("matched_keywords") else 0,
            len(item.get("matched_keywords", [])),
            item.get("score", 0),
            item.get("date", ""),
        ),
        reverse=True,
    )
    top = candidates[:top_n]
    print(f"  After history dedup: {len(candidates)} candidates (removed {removed})", file=sys.stderr)
    print(f"  Final: {len(top)} papers", file=sys.stderr)
    return top


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="Number of days to fetch (default: 1)")
    parser.add_argument(
        "--keywords",
        help="Override config keywords for this run. Split by comma/space/newline.",
    )
    parser.add_argument("--refresh-profile", action="store_true")
    args = parser.parse_args()

    global KEYWORDS
    reset_filter_audit()
    apply_library_profile(refresh=args.refresh_profile)
    override_keywords = parse_keywords_override(args.keywords or "")
    if override_keywords:
        KEYWORDS = override_keywords

    if args.date:
        _dp = args.date.split("-")
        target_date = date(int(_dp[0]), int(_dp[1]), int(_dp[2]))
    else:
        target_date = datetime.now().date()
    days = max(1, args.days)
    start_date = target_date - timedelta(days=days - 1)
    top_n = TOP_N * days

    print(
        f"[fetch_and_score] {target_date}, days={days} [{start_date} ~ {target_date}], top_n={top_n}, keywords={KEYWORDS}",
        file=sys.stderr,
    )

    papers = []
    if bool(_CONFIG.get("pubmed_enabled", True)):
        papers.extend(fetch_pubmed_papers(start_date, target_date, days))
    if bool(_CONFIG.get("biorxiv_enabled", True)):
        papers.extend(fetch_biorxiv_papers(start_date, target_date, days))
    top = merge_and_dedup(papers, target_date, days=days, top_n=top_n)
    output_path = temp_file_path("daily_papers_top30.json")
    output_path.write_text(json.dumps(top, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    filter_audit_path = temp_file_path("daily_papers_filter_audit.json")
    filter_audit_path.write_text(json.dumps(filter_audit_snapshot(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  Wrote: {output_path}", file=sys.stderr)
    print(f"  Wrote: {filter_audit_path}", file=sys.stderr)
    json.dump(top, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
