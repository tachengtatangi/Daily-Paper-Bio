#!/usr/bin/env python3
"""Enrich PubMed / bioRxiv papers with metadata from HTML pages.

Design mirrors original dailypaper-skills enrich_papers.py (arXiv version):
  - asyncio + curl subprocess for concurrent HTTP, no token cost
  - Pure regex HTML parsing, no external deps beyond stdlib
  - Graceful fallback: unenriched fields keep original values

Sources:
  PubMed  → PubMed Central HTML  (https://pmc.ncbi.nlm.nih.gov/articles/PMC{id}/)
           → Europe PMC fulltext  (https://europepmc.org/article/MED/{pmid})
  bioRxiv → HTML abstract page   (https://www.biorxiv.org/content/{doi}v{version})
  fallback → abstract text only (regex extraction, works always)

Usage:
    # stdin → stdout
    cat top30.json | python enrich_papers.py /tmp/enriched.json

    # cross-platform explicit paths
    python enrich_papers.py input.json output.json
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import sys
from collections import Counter
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

_SHARED_DIR = _SCRIPT_DIR.parent / "_shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from user_config import temp_file_path, sources_config

from pipeline_guard import PipelineGuardError, require_top30_ready

SEMAPHORE_LIMIT  = 8
CURL_TIMEOUT     = 25   # seconds for curl's own --max-time flag
ASYNCIO_TIMEOUT  = 35   # asyncio.wait_for timeout (curl + process overhead)
MAX_RETRIES      = 2


# ── Method-name stop words (biology-specific) ────────────────────────────────

METHOD_STOP = {
    # Generic section labels
    "Abstract", "Introduction", "Methods", "Method", "Results", "Conclusion",
    "Conclusions", "Discussion", "Review", "Study", "Analysis", "Background",
    "Supplementary", "References", "Acknowledgements", "Acknowledgments",
    # Biology common terms that look like acronyms but aren't method names
    "DNA", "RNA", "PCR", "SNP", "QTL", "GWAS", "GO", "KEGG",
    "USA", "UK", "EU", "IBM", "DOI", "PMID", "NCBI", "UCSC",
    "ML", "AI", "CI", "OR", "HR",
    # Roman numerals
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    # Web/page chrome often found in publisher pages
    "PDF", "HTML", "XML", "RIS", "ORCID", "CrossRef", "PubMedSearch",
    "ScholarFind", "BibTeX", "Bookends", "EasyBib", "EndNote", "Medlars",
    "Mendeley", "Papers", "RefWorks", "Zotero", "Tweet", "Facebook",
    "Google",
}

PAGE_CHROME_TOKENS = (
    "bibtex", "bookends", "easybib", "endnote", "medlars", "mendeley",
    "refworks", "ref manager", "ris", "zotero", "citation manager",
    "citation manager formats", "download citation", "tweet widget",
    "facebook like", "google plus one", "share this article",
)

# ── Real-world (in vivo / clinical) keywords ─────────────────────────────────

REAL_WORLD_KEYWORDS = [
    "patient", "patients", "clinical trial", "cohort study", "human subject",
    "in vivo", "animal model", "mouse model", "rat model", "field study",
    "field collection", "wild population", "natural population", "museum specimen",
]

# ── Institution keywords for affiliation extraction ───────────────────────────

INST_KEYWORDS = [
    "university", "universite", "università", "universität",
    "institute", "laboratory", "college", "school of",
    "center for", "centre for", "academy", "polytechnic",
    "department of", "faculty of", "research center", "national lab",
    "hospital", "clinic", "medical center",
    "google", "microsoft", "meta", "amazon", "apple", "ibm",
    "mit ", "stanford", "harvard", "cambridge", "oxford",
    "tsinghua", "peking", "chinese academy", "fudan", "zju",
]


# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ══════════════════════════════════════════════════════════════════════════════

async def curl_fetch(url: str, sem: asyncio.Semaphore,
                     timeout: int = CURL_TIMEOUT,
                     retries: int = MAX_RETRIES) -> str:
    """Fetch URL via curl subprocess. Returns empty string on failure."""
    for attempt in range(1, retries + 1):
        async with sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sL", "--max-time", str(timeout),
                    "-H", "User-Agent: daily-papers/1.0 (academic-paper-aggregation; non-commercial)",
                    url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=ASYNCIO_TIMEOUT
                )
                content = stdout.decode("utf-8", errors="replace") if stdout else ""
                if content and len(content) > 200:
                    return content
            except (asyncio.TimeoutError, Exception) as e:
                print(f"  [curl] attempt {attempt}/{retries} {url}: {e}", file=sys.stderr)
        if attempt < retries:
            await asyncio.sleep(2 * attempt)
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# HTML regex extractors (biology-adapted)
# ══════════════════════════════════════════════════════════════════════════════

def strip_tags(html: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", html))


def looks_like_page_chrome(text: str) -> bool:
    """Return True when extracted text is publisher navigation/export chrome."""
    low = re.sub(r"\s+", " ", html_lib.unescape(text or "")).strip().lower()
    hits = sum(1 for token in PAGE_CHROME_TOKENS if token in low)
    return hits >= 2 or "citation manager formats" in low


def extract_figure_url(html: str) -> str:
    """Extract the first meaningful figure image URL from HTML."""
    figures = re.findall(
        r"<(?:img|figure)[^>]+src=[\"']([^\"'>]+\.(png|jpg|jpeg|gif|svg|webp))[\"']",
        html, re.DOTALL | re.IGNORECASE
    )
    skip = {"icon", "logo", "badge", "orcid", "creative", "arrow", "button",
            "check", "expand", "close", "menu", "spinner", "loading",
            "twitter", "tweet", "facebook", "fb-", "fb_", "google",
            "share", "social", "linkedin", "reddit", "pinterest",
            "mendeley", "zotero", "endnote", "refworks", "citation",
            "email", "print", "rss", "altmetric"}
    for fig, _ in figures:
        low = fig.lower()
        if any(s in low for s in skip):
            continue
        if fig.startswith("/"):
            fig = "https:" + fig if fig.startswith("//") else fig
        return fig
    return ""


def extract_authors_html(html: str) -> list[str]:
    """Extract author names from common HTML patterns."""
    # Pattern 1: <meta name="citation_author" content="Name">
    authors = re.findall(
        r'<meta\s+name=["\']citation_author["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if authors:
        return [a.strip() for a in authors if a.strip()]
    # Pattern 2: schema.org author
    names = re.findall(
        r'"author"[^}]*?"name"\s*:\s*"([^"]+)"', html
    )
    if names:
        return [n.strip() for n in names if n.strip()]
    return []


def extract_affiliations_html(html: str) -> list[str]:
    """Extract institution affiliations from HTML."""
    affils: set[str] = set()
    # Pattern 1: <meta name="citation_author_institution">
    for m in re.findall(
        r'<meta\s+name=["\']citation_author_institution["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        if m.strip():
            affils.add(m.strip())
    # Pattern 2: class attributes common in PMC / Europe PMC
    for cls in ("aff", "affiliation", "contrib-aff", "institution"):
        for m in re.finditer(
            rf'class="[^"]*{cls}[^"]*"[^>]*>(.*?)</(?:span|div|p|li)',
            html, re.DOTALL | re.IGNORECASE
        ):
            text = strip_tags(m.group(1)).strip(" ,;.")
            if text and 5 < len(text) < 300:
                if any(kw in text.lower() for kw in INST_KEYWORDS):
                    affils.add(text)
    return list(affils)


def extract_section_headers(html: str) -> list[str]:
    headers = []
    for m in re.finditer(r"<h[2-4][^>]*>(.*?)</h[2-4]>", html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        text = re.sub(r"^\d+(\.\d+)*\.?\s*", "", text)
        if text and len(text) < 200 and not looks_like_page_chrome(text):
            headers.append(text)
    return headers[:20]


def extract_captions(html: str) -> list[str]:
    captions = []
    for m in re.finditer(
        r"<(?:figcaption|caption)[^>]*>(.*?)</(?:figcaption|caption)>",
        html, re.DOTALL
    ):
        text = strip_tags(m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        if 10 <= len(text) <= 300 and not looks_like_page_chrome(text):
            captions.append(text)
    return captions[:8]


def extract_has_real_world(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in REAL_WORLD_KEYWORDS)


def extract_method_names_from_html(html: str, title: str) -> list[str]:
    """Extract method/tool names from full HTML text (frequency ≥ 2)."""
    text = strip_tags(html)
    return _method_names_from_text(text, title)


def _method_names_from_text(text: str, title: str) -> list[str]:
    """Extract method names from plain text using CamelCase + ALLCAPS patterns."""
    camel     = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]*)+(?:V?\d+)?)\b", text)
    allcaps   = re.findall(r"\b([A-Z]{2,}(?:[-_]\d+)?)\b", text)
    camel_num = re.findall(r"\b([A-Z][a-z]+[A-Z][a-z]*\d+[a-z]?)\b", text)
    hyphen    = re.findall(r"\b([A-Z][a-z]+-[A-Z][a-z]+(?:-[A-Z][a-z]+)?)\b", text)

    cnt = Counter(camel + allcaps + camel_num + hyphen)
    title_words = set(re.findall(r"\b[A-Za-z]+\b", title))
    stop = METHOD_STOP | {w for w in title_words if len(w) >= 3}

    results: list[str] = []
    seen: set[str] = set()
    for name, count in cnt.most_common(40):
        if count < 2:
            continue
        if name in stop or len(name) < 2:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        results.append(name)
        if len(results) >= 15:
            break
    return results


def extract_method_summary(html: str) -> str:
    """Extract 300-500 char method summary from Methods / Materials section."""
    m = re.search(
        r"<h[2-4][^>]*>.*?(?:Method|Material|Approach|Protocol).*?</h[2-4]>(.*?)(?:<h[2-4]|$)",
        html, re.DOTALL | re.IGNORECASE
    )
    if not m:
        # Fallback: abstract last paragraph
        m = re.search(
            r"<(?:p|div)[^>]*class=[\"'][^\"']*abstract[^\"']*[\"'][^>]*>(.*?)</(?:p|div)>",
            html, re.DOTALL | re.IGNORECASE
        )
    if not m:
        return ""
    section = re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()
    section = re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", section)
    if looks_like_page_chrome(section):
        return ""
    if len(section) > 500:
        end = section.rfind(". ", 300, 550)
        section = section[:end + 1] if end > 0 else section[:500].rsplit(" ", 1)[0] + "..."
    return section if len(section) >= 80 else ""


# ══════════════════════════════════════════════════════════════════════════════
# Source-specific URL builders
# ══════════════════════════════════════════════════════════════════════════════

def _pmc_url(paper: dict) -> str:
    """Return PubMed Central HTML URL if we have a PMC ID."""
    pmc = str(paper.get("pmc_id") or paper.get("pmc") or "").strip()
    if pmc:
        pmc = re.sub(r"^PMC", "", pmc, flags=re.IGNORECASE)
        return f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc}/"
    return ""


def _europepmc_url(paper: dict) -> str:
    pmid = str(paper.get("id") or "").strip()
    if pmid.isdigit():
        return f"https://europepmc.org/article/MED/{pmid}"
    return ""


def _biorxiv_html_url(paper: dict) -> str:
    doi = str(paper.get("doi") or "").strip()
    # Traditional bioRxiv DOI prefix (10.1101/...)
    m = re.search(r"10\.1101/(\S+)", doi)
    if m:
        return f"https://www.biorxiv.org/content/10.1101/{m.group(1)}"
    # Newer DOI prefixes (e.g. 10.64898/...) — use the pre-built URL already
    # stored on the paper object (set by normalize_biorxiv_item).
    url = str(paper.get("url") or "").strip()
    if url and "biorxiv.org" in url:
        return url
    return ""


# ── Elsevier / ScienceDirect helpers ─────────────────────────────────────────

_ELSEVIER_PREFIXES = (
    "10.1016/",  # Elsevier flagship (Cell, Lancet, etc.)
    "10.1006/",  # old Academic Press
    "10.1053/",  # Saunders / Mosby
    "10.1054/",  # Elsevier Health Sciences
    "10.1067/",  # Mosby
    "10.1078/",  # Elsevier imprint
    "10.1383/",  # Elsevier imprint
)


def _is_elsevier_doi(doi: str) -> bool:
    """Return True if the DOI belongs to an Elsevier journal."""
    d = doi.strip().lower()
    return any(d.startswith(p) for p in _ELSEVIER_PREFIXES)


async def _elsevier_enrich(doi: str, api_key: str,
                           sem: asyncio.Semaphore) -> dict:
    """Fetch Elsevier Abstract Retrieval API and return extracted fields.

    Endpoint: GET https://api.elsevier.com/content/abstract/doi/{doi}
    Returns an empty dict on any failure (graceful fallback).
    """
    url = f"https://api.elsevier.com/content/abstract/doi/{doi}"
    async with sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sL", "--max-time", str(CURL_TIMEOUT),
                "-H", f"X-ELS-APIKey: {api_key}",
                "-H", "Accept: application/json",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=ASYNCIO_TIMEOUT
            )
            raw = stdout.decode("utf-8", errors="replace") if stdout else ""
        except Exception as e:
            print(f"  [elsevier] {doi}: {e}", file=sys.stderr)
            return {}

    if not raw or len(raw) < 100:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    # Elsevier may return either flat dict or wrapper; handle both
    if not isinstance(data, dict):
        return {}

    # Root can be the response directly or wrapped under 'abstracts-retrieval-response'
    root = data.get("abstracts-retrieval-response", data)
    if not isinstance(root, dict):
        return {}

    core = root.get("coredata", {})
    if not isinstance(core, dict):
        core = {}

    result: dict = {}

    # Abstract — field name varies by article type
    for abstract_key in ("dc:description", "prism:description", "abstract"):
        abstract = str(core.get(abstract_key) or "").strip()
        if abstract:
            result["abstract"] = abstract
            break

    # Authors — dc:creator may be str, dict, or list
    authors_raw = core.get("dc:creator") or []
    if isinstance(authors_raw, str):
        authors_raw = [{"$": authors_raw}]
    elif isinstance(authors_raw, dict):
        authors_raw = [authors_raw]
    author_names = []
    for a in authors_raw:
        if not isinstance(a, dict):
            continue
        name = str(a.get("$", "") or a.get("ce:indexed-name", "") or "").strip()
        if name:
            author_names.append(name)
    if author_names:
        result["authors"] = ", ".join(author_names)

    # Affiliations — may be list or single dict
    affil_raw = root.get("affiliation", [])
    if isinstance(affil_raw, dict):
        affil_raw = [affil_raw]
    elif not isinstance(affil_raw, list):
        affil_raw = []
    affils = []
    for a in affil_raw:
        if not isinstance(a, dict):
            continue
        name    = str(a.get("affilname", "") or "").strip()
        city    = str(a.get("affiliation-city", "") or "").strip()
        country = str(a.get("affiliation-country", "") or "").strip()
        parts   = [p for p in (name, city, country) if p]
        if parts:
            affils.append(", ".join(parts))
    if affils:
        result["affiliations"] = " | ".join(affils)

    # Journal / source title
    for journal_key in ("prism:publicationName", "dc:publisher", "prism:aggregationType"):
        src_title = str(core.get(journal_key) or "").strip()
        if src_title and len(src_title) < 200:
            result["journal"] = src_title
            break

    return result


def _abs_html_url(paper: dict) -> str:
    """Best available HTML URL for metadata enrichment.

    Priority:
      1. Elsevier DOIs → handled separately by _elsevier_enrich (return "")
      2. bioRxiv / medrxiv papers → bioRxiv HTML directly (no PMC needed,
         preprints are freely accessible on the preprint server)
      3. PubMed papers → PMC first (free, legal, best metadata quality)
      4. Fallback → Europe PMC HTML (covers PubMed papers without PMC record)
    """
    doi = str(paper.get("doi") or "").strip()
    if doi and _is_elsevier_doi(doi):
        return ""  # handled by _elsevier_enrich

    src = str(paper.get("source") or "").lower()

    # bioRxiv / medrxiv: go directly to preprint server (most up-to-date, free)
    if "biorxiv" in src or "medrxiv" in src or "preprint" in src:
        url = _biorxiv_html_url(paper)
        if url:
            return url

    # PubMed papers: prefer PMC (free full text, rich metadata)
    url = _pmc_url(paper)
    if url:
        return url

    # Fallback: Europe PMC covers most remaining PubMed papers
    return _europepmc_url(paper)


# ══════════════════════════════════════════════════════════════════════════════
# Lightweight abstract-only fallback (no network needed)
# ══════════════════════════════════════════════════════════════════════════════

def _enrich_from_abstract(paper: dict) -> dict:
    """Derive enrichment fields purely from title + abstract text (no network)."""
    text = f"{paper.get('title', '')} {paper.get('abstract', '')}"
    result = dict(paper)
    result.setdefault("figure_url", "")
    result.setdefault("affiliations", paper.get("affiliations", ""))
    result.setdefault("section_headers", [])
    result.setdefault("captions", [])
    result["has_real_world"]  = extract_has_real_world(text)
    result["method_names"]    = _method_names_from_text(text, paper.get("title", ""))
    result["method_summary"]  = ""
    result.setdefault("keywords", paper.get("keywords", []))
    result.setdefault("journal", paper.get("journal", paper.get("category", "")))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Per-paper async enrichment
# ══════════════════════════════════════════════════════════════════════════════

async def enrich_one(paper: dict, sem: asyncio.Semaphore,
                     elsevier_api_key: str = "") -> dict:
    """Enrich a single paper. Falls back gracefully at each step.

    Priority:
      1. Elsevier Abstract API  (for 10.1016/… DOIs, requires api_key)
      2. PMC HTML               (free full-text, best coverage)
      3. bioRxiv HTML           (for preprints)
      4. Europe PMC HTML        (fallback for PubMed papers without PMC)
      5. Abstract-only          (always works, no network needed)
    """
    doi = str(paper.get("doi") or "").strip()
    title = paper.get("title", "")
    result = dict(paper)

    # ── Elsevier branch ───────────────────────────────────────────────────────
    if doi and _is_elsevier_doi(doi) and elsevier_api_key:
        try:
            els = await _elsevier_enrich(doi, elsevier_api_key, sem)
            if els:
                result.update(els)
                # Fill remaining fields from abstract text
                text = f"{paper.get('title', '')} {result.get('abstract', paper.get('abstract', ''))}"
                result.setdefault("figure_url", "")
                result.setdefault("section_headers", [])
                result.setdefault("captions", [])
                result["has_real_world"] = extract_has_real_world(text)
                result["method_names"]   = _method_names_from_text(text, title)
                result["method_summary"] = ""
                result.setdefault("keywords", paper.get("keywords", []))
                print(f"  [elsevier] enriched {doi}", file=sys.stderr)
                return result
        except Exception as e:
            print(f"  [elsevier] {paper.get('id', '')} error: {e}", file=sys.stderr)
        # Elsevier API failed — fall through to abstract-only
        return _enrich_from_abstract(paper)

    # ── HTML branch (PMC / bioRxiv / EuropePMC) ───────────────────────────────
    html_url = _abs_html_url(paper)
    if not html_url:
        return _enrich_from_abstract(paper)

    try:
        html = await curl_fetch(html_url, sem)
        if html and len(html) > 1000:
            # HTML enrichment
            result["figure_url"]      = extract_figure_url(html) or paper.get("figure_url", "")
            html_authors              = extract_authors_html(html)
            html_affiliations         = extract_affiliations_html(html)
            result["section_headers"] = extract_section_headers(html)
            result["captions"]        = extract_captions(html)
            result["has_real_world"]  = extract_has_real_world(html)
            result["method_names"]    = extract_method_names_from_html(html, title)
            result["method_summary"]  = extract_method_summary(html)
            if html_authors:
                result["authors"] = ", ".join(html_authors)
            if html_affiliations:
                result["affiliations"] = ", ".join(html_affiliations)
        else:
            # HTML fetch failed — abstract-only
            return _enrich_from_abstract(paper)

    except Exception as e:
        print(f"  [enrich] {paper.get('id', '')} error: {e}", file=sys.stderr)
        return _enrich_from_abstract(paper)

    # Ensure keys exist
    result.setdefault("figure_url", "")
    result.setdefault("affiliations", paper.get("affiliations", ""))
    result.setdefault("section_headers", [])
    result.setdefault("captions", [])
    result.setdefault("has_real_world", False)
    result.setdefault("method_names", [])
    result.setdefault("method_summary", "")
    result.setdefault("keywords", paper.get("keywords", []))
    result.setdefault("journal", paper.get("journal", paper.get("category", "")))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Batch entry points
# ══════════════════════════════════════════════════════════════════════════════

async def enrich_all(papers: list[dict],
                     elsevier_api_key: str = "") -> list[dict]:
    sem     = asyncio.Semaphore(SEMAPHORE_LIMIT)
    total   = len(papers)
    done    = 0
    results = [None] * total

    async def _wrap(idx: int, paper: dict) -> None:
        nonlocal done
        try:
            results[idx] = await enrich_one(paper, sem, elsevier_api_key)
        except Exception as e:
            print(f"  [enrich] paper #{idx} failed: {e}", file=sys.stderr)
            results[idx] = _enrich_from_abstract(paper)
        done += 1
        if done % 5 == 0 or done == total:
            print(f"  [enrich] {done}/{total} done", file=sys.stderr)

    tasks = [asyncio.create_task(_wrap(i, p)) for i, p in enumerate(papers)]
    await asyncio.gather(*tasks)
    return results  # type: ignore[return-value]


def enrich_papers(papers: list[dict]) -> list[dict]:
    """Synchronous wrapper used by fetch/review workflows."""
    if not papers:
        return []
    api_key = str(sources_config().get("elsevier_api_key") or "").strip()
    if api_key:
        print(f"[enrich] Elsevier API key found — will use for 10.1016/… DOIs",
              file=sys.stderr)
    return asyncio.run(enrich_all(papers, elsevier_api_key=api_key))


def enrich_paper(paper: dict) -> dict:
    """Enrich a single paper (synchronous)."""
    return enrich_papers([paper])[0]


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _write_output(data: str, output_path: str | None) -> None:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(data)
        print(f"[enrich_papers] Written to {output_path}", file=sys.stderr)
    else:
        sys.stdout.write(data)
        sys.stdout.flush()


def main() -> None:
    output_path = None
    input_path  = None
    auto_input = False

    if len(sys.argv) >= 2 and sys.argv[1].endswith(".json"):
        input_path = sys.argv[1]
    if len(sys.argv) >= 3 and sys.argv[2].endswith(".json"):
        output_path = sys.argv[2]

    # Auto-detect from temp dir (Windows/Linux compatible)
    if not input_path:
        auto = temp_file_path("daily_papers_top30.json")
        if auto.exists():
            input_path = str(auto)
            auto_input = True
            print(f"[enrich_papers] Auto input: {input_path}", file=sys.stderr)

    if input_path:
        try:
            with open(input_path, "r", encoding="utf-8", errors="replace") as f:
                input_data = f.read()
        except FileNotFoundError:
            print(f"Error: {input_path} not found", file=sys.stderr)
            _write_output("[]", output_path)
            sys.exit(1)
    else:
        input_data = sys.stdin.read()

    if not input_data.strip():
        _write_output("[]", output_path)
        return

    try:
        papers = json.loads(input_data)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}", file=sys.stderr)
        _write_output("[]", output_path)
        sys.exit(1)

    if not papers:
        if auto_input:
            try:
                require_top30_ready(
                    top30_path=temp_file_path("daily_papers_top30.json"),
                    status_path=temp_file_path("daily_papers_fetch_status.json"),
                )
            except PipelineGuardError as exc:
                print(f"[enrich_papers] refusing stale or failed fetch output: {exc}", file=sys.stderr)
                sys.exit(2)
        if not output_path:
            output_path = str(temp_file_path("daily_papers_enriched.json"))
            print(f"[enrich_papers] Auto output: {output_path}", file=sys.stderr)
        _write_output("[]\n", output_path)
        return

    if auto_input:
        try:
            guarded = require_top30_ready(
                top30_path=temp_file_path("daily_papers_top30.json"),
                status_path=temp_file_path("daily_papers_fetch_status.json"),
            )
        except PipelineGuardError as exc:
            print(f"[enrich_papers] refusing stale or failed fetch output: {exc}", file=sys.stderr)
            sys.exit(2)
        if len(guarded) != len(papers):
            print("[enrich_papers] refusing input: top30 changed during guard check", file=sys.stderr)
            sys.exit(2)

    print(f"Enriching {len(papers)} papers...", file=sys.stderr)
    enriched = enrich_papers(papers)
    print(f"Done. Enriched {len(enriched)} papers.", file=sys.stderr)

    output = json.dumps(enriched, ensure_ascii=False, indent=2) + "\n"

    if not output_path:
        output_path = str(temp_file_path("daily_papers_enriched.json"))
        print(f"[enrich_papers] Auto output: {output_path}", file=sys.stderr)

    _write_output(output, output_path)


if __name__ == "__main__":
    main()
