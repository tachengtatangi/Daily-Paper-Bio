"""
pdf_fetcher.py — Patchright-based PDF downloader + Fig1 extractor for paper-reader.

Public API (all sync):
    fetch_paper_pdf(doi_or_url, *, elsevier_api_key="", pdf_save_dir=None,
                    fig_save_dir=None, identifier="") -> dict

    extract_fig1_from_pdf_bytes(pdf_bytes, out_path) -> bool
    extract_fig1_from_pdf_path(pdf_path, out_path) -> bool

The returned dict keys are compatible with run_reader.py's merge_fulltext_record():
    full_text        str
    downloaded_pdf   str    path to saved PDF (or "" if not saved)
    figure_paths     list[str]
    figure_items     list[dict]  [{"path": str, "caption": str}]
    summary_mode     str
    acquisition_path str
    paywall          bool
    paywall_reason   str

Requirements:
    patchright   (pip install patchright && patchright install chromium)
    pymupdf      (pip install pymupdf)  -- fitz
    pypdf        (pip install pypdf)
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

# ── optional heavy deps ───────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
    _FITZ_OK = True
except ImportError:
    _FITZ_OK = False

try:
    import pypdf as _pypdf
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

# ── Chrome path detection ─────────────────────────────────────────────────────
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


# ── publisher domain map (for article-page detection) ─────────────────────────
_PUBLISHER_DOMAIN_MAP: dict[str, list[str]] = {
    "pnas.org":              ["pnas.org"],
    "science.org":           ["science.org", "sciencemag.org"],
    "sciencemag.org":        ["science.org", "sciencemag.org"],
    "cell.com":              ["cell.com", "sciencedirect.com"],
    "sciencedirect.com":     ["cell.com", "sciencedirect.com"],
    "nature.com":            ["nature.com"],
    "academic.oup.com":      ["academic.oup.com", "oup.com"],
    "springer.com":          ["springer.com"],
    "wiley.com":             ["wiley.com", "onlinelibrary.wiley.com"],
    "onlinelibrary.wiley":   ["wiley.com", "onlinelibrary.wiley.com"],
    "biorxiv.org":           ["biorxiv.org"],
    "medrxiv.org":           ["medrxiv.org"],
    "elifesciences.org":     ["elifesciences.org"],
    "frontiersin.org":       ["frontiersin.org"],
    "mdpi.com":              ["mdpi.com"],
    "plos.org":              ["plos.org"],
    "bmj.com":               ["bmj.com"],
    "thelancet.com":         ["thelancet.com"],
}

_CF_MARKERS = [
    "just a moment", "checking your browser", "请稍候",
    "enable javascript", "ray id", "verify you are human",
]


def _auto_known_domains(url: str) -> list[str]:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for key, domains in _PUBLISHER_DOMAIN_MAP.items():
        if key in netloc:
            return domains
    if netloc and "doi.org" not in netloc:
        return [netloc]
    return []


def _is_cf(title: str) -> bool:
    return any(m in title.lower() for m in _CF_MARKERS)


def _is_real_article(title: str, url: str, known: list[str]) -> bool:
    if not title or len(title) < 8 or _is_cf(title):
        return False
    if known:
        return any(d in url for d in known)
    return "doi.org" not in url


# ── PDF-link discovery ────────────────────────────────────────────────────────
_PDF_DOMAINS = [
    "nature.com", "pnas.org", "science.org", "sciencedirect.com",
    "cell.com", "oup.com", "springer.com", "wiley.com", "silverchair",
    "biorxiv.org", "elife", "frontiersin.org", "mdpi.com",
    "plos.org", "bmj.com", "nejm.org", "thelancet.com",
]


def _is_suppl(href: str) -> bool:
    return bool(re.search(r"/suppl[_/]|supplement|_sm\.pdf|supporting", href, re.I))


def _normalize_pdf_href(href: str) -> str:
    href = re.sub(r"/doi/epdf/",   "/doi/pdf/", href, flags=re.I)
    href = re.sub(r"/doi/reader/", "/doi/pdf/", href, flags=re.I)
    return href


async def _find_pdf_url(page) -> tuple[str, str]:
    """
    Scan page for the main-article PDF link.
    Returns (raw_href, normalised_href).  Both "" if not found.

    Score table (high → low):
      10  /doi/pdf/
       9  /doi/epdf/  /doi/reader/  article-pdf  /pdfft
       8  /action/showPdf  (Elsevier/Cell)
       7  .pdf ending + known publisher domain
       6  /pdf/ path  + known publisher domain
       5  link text contains "pdf"       (generic catch-all)
       4  "pdf" in href + same domain    (widest catch-all)
    """
    # citation_pdf_url meta tag (Nature / PNAS / Science / Springer)
    try:
        val = await page.get_attribute(
            'meta[name="citation_pdf_url"]', "content", timeout=2000)
        if val and not _is_suppl(val):
            return val, _normalize_pdf_href(val)
    except Exception:
        pass

    page_domain = urlparse(page.url).netloc.lower()

    links = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => ({href: e.href, "
        "text: (e.innerText||'').trim().toLowerCase()}))",
    )
    candidates: list[tuple[int, str]] = []
    for item in links:
        href = item.get("href", "")
        text = item.get("text", "")
        h    = href.lower()
        if not href or _is_suppl(href):
            continue
        score = 0
        if   re.search(r"/doi/pdf/",                   h):                   score = 10
        elif re.search(r"/doi/epdf/|/doi/reader/",      h):                   score = 9
        elif re.search(r"article-pdf|/pdfft\b",          h):                   score = 9
        elif re.search(r"/action/showpdf",               h, re.I):             score = 8
        elif h.endswith(".pdf") and any(d in h for d in _PDF_DOMAINS):         score = 7
        elif re.search(r"/pdf/", h) and any(d in h for d in _PDF_DOMAINS):     score = 6
        elif re.search(r"\bpdf\b", text) and href.startswith("http"):          score = 5
        elif "pdf" in h and page_domain and page_domain in h:                  score = 4
        if score:
            candidates.append((score, href))

    if candidates:
        candidates.sort(reverse=True)
        raw = candidates[0][1]
        return raw, _normalize_pdf_href(raw)
    return "", ""


async def _find_viewer_url(page) -> str:
    """Find reader/epdf/showPdf viewer URL (browser will request PDF internally)."""
    try:
        links = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)")
    except Exception:
        return ""
    VIEWER_PAT = re.compile(r"/doi/reader/|/doi/epdf/|/action/showpdf", re.I)
    for href in links:
        if not _is_suppl(href) and VIEWER_PAT.search(href):
            return href
    return ""


# ── core download helpers ─────────────────────────────────────────────────────

async def _js_fetch(page, url: str) -> bytes:
    """Download via in-browser JS fetch (same-origin, full cookies)."""
    try:
        arr = await page.evaluate("""async (u) => {
            try {
                const r = await fetch(u, {credentials: 'include'});
                const ct = r.headers.get('content-type') || '';
                if (!ct.toLowerCase().includes('pdf')) return null;
                const ab = await r.arrayBuffer();
                const u8 = new Uint8Array(ab);
                const CHUNK = 65536, chunks = [];
                for (let i = 0; i < u8.length; i += CHUNK)
                    chunks.push(Array.from(u8.subarray(i, i + CHUNK)));
                return chunks;
            } catch(e) { return null; }
        }""", url)
        if arr:
            flat: list[int] = []
            for c in arr:
                flat.extend(c)
            b = bytes(flat)
            if b[:4] == b"%PDF":
                return b
    except Exception:
        pass
    return b""


async def _api_fetch(context, url: str, referer: str = "") -> bytes:
    """Download via playwright context API (brings session cookies)."""
    try:
        resp = await context.request.get(
            url,
            headers={"Accept": "application/pdf,*/*",
                     "Referer": referer or url},
            timeout=60_000,
        )
        if resp.status == 200:
            body = await resp.body()
            if body[:4] == b"%PDF":
                return body
    except Exception:
        pass
    return b""


async def _navigate_and_capture(page, context, url: str,
                                 referer: str = "") -> bytes:
    """
    Navigate to url, capture the PDF response via on('response'),
    then download via JS-fetch or API re-fetch.
    Does NOT use route.fetch (causes ECONNRESET for on-the-fly signing).
    """
    detected: list[str] = []

    async def on_resp(r):
        if detected:
            return
        if "pdf" in r.headers.get("content-type", "").lower():
            detected.append(r.url)

    page.on("response", on_resp)
    is_pdf_url = bool(re.search(r"\.(pdf)(\?|$)|/pdfdirect/", url, re.I))
    wc = "commit" if is_pdf_url else "load"
    try:
        await page.goto(url, wait_until=wc, timeout=35_000)
        await page.wait_for_timeout(15_000)
    except Exception:
        await asyncio.sleep(5)
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass

    pdf_url = detected[0] if detected else url

    b = await _js_fetch(page, pdf_url)
    if b:
        return b
    b = await _api_fetch(context, pdf_url, referer=referer or url)
    return b


async def _quick_paywall_check(page, pdf_url: str) -> str:
    """
    JS-fetch the PDF URL and check content-type.
    Science paywalled articles return HTTP 200 text/html, not 403 — so we
    check CT rather than status.  Returns "" if no paywall detected.
    """
    if not pdf_url or not pdf_url.startswith("http"):
        return ""
    try:
        info = await page.evaluate("""async (u) => {
            try {
                const r = await fetch(u, {credentials: 'include'});
                const ct = (r.headers.get('content-type') || '').toLowerCase();
                return {pdf: ct.includes('pdf'), status: r.status, ct: ct};
            } catch(e) { return {pdf: false, status: 0, ct: ''}; }
        }""", pdf_url)
        if info.get("pdf"):
            return ""
        ct, status = info.get("ct", ""), info.get("status", 0)
        if "html" in ct:
            return f"需要订阅/付费（PDF请求返回 HTML，HTTP {status}）"
        if status in (401, 403):
            return f"需要订阅/付费（HTTP {status}）"
    except Exception:
        pass
    return ""


async def _accept_cookies(page):
    for lbl in ["Accept all cookies", "Accept all", "Accept Cookies",
                 "Accept", "Allow all", "I agree", "Agree and proceed"]:
        try:
            btn = page.get_by_role("button", name=re.compile(lbl, re.I))
            if await btn.count() > 0:
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass


# ── Fig1 extraction ───────────────────────────────────────────────────────────

def extract_fig1_from_pdf_bytes(pdf_bytes: bytes, out_path: Path | str) -> bool:
    """
    Extract Figure 1 from PDF bytes and save as PNG to out_path.

    Strategy:
    1. Use fitz get_text("dict") to locate caption "Fig. 1" / "Figure 1" text blocks
       and image blocks by bounding-box.
    2. Crop the combined region (figure + caption) and render at 3×.
    3. Fallback: first large embedded bitmap ≥500×400 px.
    Returns True if an image was saved.
    """
    if not _FITZ_OK or not pdf_bytes:
        return False
    out_path = Path(out_path)
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return False
    CAP_PAT      = re.compile(r"(?i)^fig(?:ure)?\.?\s*1[\.\s\)\,\:]")
    LEGENDS_PAT  = re.compile(r"(?i)^fig(?:ure)?\s*(legends?|captions?|titles?)\s*$")

    for pn, pg in enumerate(doc):
        pg_rect   = pg.rect
        blk_dict  = pg.get_text("dict")["blocks"]
        caption_rect: "fitz.Rect | None" = None
        image_rects:  list = []
        page_is_legends = False

        for blk in blk_dict:
            if blk["type"] == 0:   # text block
                full = " ".join(
                    sp["text"]
                    for ln in blk.get("lines", [])
                    for sp in ln.get("spans", [])
                ).strip()
                # Detect "Figure legends" / "Figure captions" heading pages
                if LEGENDS_PAT.match(full):
                    page_is_legends = True
                if CAP_PAT.match(full) and caption_rect is None:
                    caption_rect = fitz.Rect(blk["bbox"])
            elif blk["type"] == 1:  # image block
                r = fitz.Rect(blk["bbox"])
                if r.width > 80 and r.height > 80:
                    image_rects.append(r)

        if caption_rect is None:
            continue

        # Images above caption (y1 ≤ caption top + 30pt overlap tolerance)
        fig_rects = [r for r in image_rects
                     if r.y1 <= caption_rect.y0 + 30 and r.y0 < caption_rect.y0]

        if fig_rects:
            combined = fig_rects[0]
            for r in fig_rects[1:]:
                combined = combined | r
            combined = combined | caption_rect
            clip = fitz.Rect(
                max(0,          combined.x0 - 8),
                max(0,          combined.y0 - 8),
                min(pg_rect.x1, combined.x1 + 8),
                min(pg_rect.y1, combined.y1 + 50),
            )
            pix = pg.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
            pix.save(str(out_path))
            doc.close()
            return True

        # Vector figure (no image blocks above caption): crop region above caption.
        # Skip if this page is a dedicated "Figure legends" section (text-only,
        # no actual figure above the caption) or if there's not enough space above
        # the caption to hold a real figure.
        fig_height = min(caption_rect.y0, 500)
        if page_is_legends or fig_height < 200:
            continue   # no real figure on this page — keep searching

        clip = fitz.Rect(
            max(0,          caption_rect.x0 - 20),
            max(0,          caption_rect.y0 - fig_height),
            min(pg_rect.x1, caption_rect.x1 + 20),
            min(pg_rect.y1, caption_rect.y1 + 60),
        )
        pix = pg.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
        pix.save(str(out_path))
        doc.close()
        return True

    # Fallback: first large embedded bitmap (original resolution)
    for pn, pg in enumerate(doc):
        for img_info in pg.get_images(full=True):
            try:
                base = doc.extract_image(img_info[0])
                raw  = base["image"]
                w, h = base["width"], base["height"]
                if w >= 500 and h >= 400 and len(raw) >= 50_000:
                    out_path.write_bytes(raw)
                    doc.close()
                    return True
            except Exception:
                pass

    doc.close()
    return False


def extract_fig1_from_pdf_path(pdf_path: Path | str, out_path: Path | str) -> bool:
    """Convenience wrapper that reads a PDF file then calls extract_fig1_from_pdf_bytes."""
    try:
        data = Path(pdf_path).read_bytes()
        return extract_fig1_from_pdf_bytes(data, out_path)
    except Exception:
        return False


# ── full-text extraction from PDF bytes ──────────────────────────────────────

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if _FITZ_OK:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            text = "\n".join(pg.get_text() for pg in doc)
            doc.close()
            if text.strip():
                return text
        except Exception:
            pass
    if _PYPDF_OK:
        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            return "\n".join(pg.extract_text() or "" for pg in reader.pages)
        except Exception:
            pass
    return ""


# ── bioRxiv / medRxiv direct HTTP fetch (open access) ───────────────────────

def _fetch_biorxiv_direct(url: str) -> dict:
    """Fetch bioRxiv/medRxiv PDF directly over HTTP — no browser required.

    bioRxiv and medRxiv are fully open access.  The canonical PDF URL is the
    abstract URL with '.full.pdf' appended (version suffix is optional):

        https://www.biorxiv.org/content/10.1101/2021.01.01.123456v2.full.pdf

    Accepts abstract page URL, versioned or unversioned, with or without
    existing '.full' / '.pdf' suffix.  Also accepts plain DOI 10.1101/...
    which is resolved to the biorxiv URL.
    """
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    empty: dict = {
        "full_text": "", "downloaded_pdf": "", "figure_paths": [],
        "figure_items": [], "summary_mode": "", "acquisition_path": "",
        "paywall": False, "paywall_reason": "",
    }

    # Resolve plain DOI 10.1101/... → canonical biorxiv abstract URL
    plain_doi = re.match(r"^(10\.1101/\S+)", url.strip())
    if plain_doi:
        url = "https://www.biorxiv.org/content/" + plain_doi.group(1)

    # Normalise to .full.pdf URL
    clean = url.rstrip("/")
    clean = re.sub(r"(\.full)?(\.pdf)?$", "", clean, flags=re.IGNORECASE)
    pdf_url = clean + ".full.pdf"

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; research-reader/1.0)",
        "Accept": "application/pdf, */*",
    }
    try:
        req = Request(pdf_url, headers=headers)
        with urlopen(req, timeout=40) as resp:
            pdf_bytes = resp.read()
    except (URLError, OSError):
        return empty
    except Exception:
        return empty

    # Verify we actually got a PDF
    if not pdf_bytes or not pdf_bytes[:8].lstrip().startswith(b"%PDF"):
        return empty

    text = _extract_text_from_pdf_bytes(pdf_bytes)
    if not text.strip():
        return empty

    result = dict(empty)
    result["full_text"]        = text[:60_000]
    result["summary_mode"]     = "基于全文/PDF文本提取（直接HTTP）"
    result["acquisition_path"] = f"bioRxiv/medRxiv direct HTTP: {pdf_url}"
    return result


# ── Elsevier full-text via curl subprocess ────────────────────────────────────

def _elsevier_fetch_xml_via_curl(doi: str, api_key: str,
                                  timeout: int = 60) -> str:
    """
    Fetch Elsevier article XML via subprocess curl (avoids Python SSL proxy issues).
    Returns raw XML string or "".
    """
    api_url = (
        f"https://api.elsevier.com/content/article/doi/{quote(doi, safe='/')}"
        f"?apiKey={quote(api_key, safe='')}&httpAccept=text/xml&view=FULL"
    )
    env = {**os.environ,
           "https_proxy": os.environ.get("https_proxy", ""),
           "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "")}
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-H", f"X-ELS-APIKey: {api_key}",
             "-H", "Accept: application/xml,text/xml;q=0.9,*/*;q=0.8",
             api_url],
            capture_output=True,
            env=env,
            timeout=timeout + 10,
        )
        xml = result.stdout.decode("utf-8", errors="replace")
        if "<" in xml and ("article" in xml.lower() or "error" not in xml[:200].lower()):
            return xml
    except Exception:
        pass
    return ""


def _parse_elsevier_xml_simple(xml_text: str) -> tuple[str, str]:
    """
    Quick regex-based extraction of (abstract, body_text) from Elsevier XML.
    Returns (abstract, full_text).  Does NOT require BeautifulSoup.
    """
    xml = xml_text or ""

    # abstract: content of <ce:abstract-sec> / <dc:description> / <abstract>
    abstract = ""
    for pat in [
        r"<ce:abstract(?:[^>]*)>(.*?)</ce:abstract>",
        r"<dc:description>(.*?)</dc:description>",
        r"<prism:teaser>(.*?)</prism:teaser>",
    ]:
        m = re.search(pat, xml, re.S | re.I)
        if m:
            abstract = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
            abstract = re.sub(r"\s+", " ", abstract)
            if len(abstract) > 50:
                break

    # body: collect all <ce:para> text (excluding references sections)
    paras = re.findall(r"<ce:para[^>]*>(.*?)</ce:para>", xml, re.S | re.I)
    in_refs = False
    body_parts: list[str] = []
    for p in paras:
        clean = re.sub(r"<[^>]+>", " ", p).strip()
        clean = re.sub(r"\s+", " ", clean)
        if not clean or len(clean) < 15:
            continue
        low = clean.lower()
        if re.match(r"^references?\s*$", low):
            in_refs = True
        if not in_refs:
            body_parts.append(clean)

    full_text = "\n\n".join(filter(None, [abstract] + body_parts))
    return abstract, full_text


# ── async core pipeline ───────────────────────────────────────────────────────

async def _run_patchright_pipeline(
    article_url: str,
    *,
    pdf_save_path: Path | None,
    fig_save_path: Path | None,
) -> dict:
    """
    Open article_url in Chrome (CDP via patchright), find PDF link,
    download PDF, extract text and Fig1.
    Returns result dict.
    """
    from patchright.async_api import async_playwright  # imported here to avoid
                                                        # mandatory dep at module load

    result: dict = {
        "full_text": "",
        "downloaded_pdf": "",
        "figure_paths": [],
        "figure_items": [],
        "summary_mode": "",
        "acquisition_path": "",
        "paywall": False,
        "paywall_reason": "",
    }

    chrome_exe = _find_chrome()
    if not chrome_exe:
        result["paywall_reason"] = "Chrome not found"
        return result

    known_domains = _auto_known_domains(article_url)
    port   = _find_free_port(9240)

    # ── Prefer user's real Chrome profile so institutional cookies are available ──
    # Priority:
    #   1. User's default Chrome User Data directory, if Chrome is NOT currently
    #      running (no SingletonLock).  Carries cookies / institutional login.
    #   2. Temp directory fallback — always safe; no cookies.
    # Rationale for SingletonLock check: if Chrome is already running with that
    # profile, launching a second instance with --remote-debugging-port will
    # silently delegate the URL to the existing Chrome and then exit, leaving
    # our CDP port unbound.  _wait_for_cdp would timeout.  Falling back to a
    # temp profile avoids the conflict (at the cost of no institutional cookies
    # for that one call).
    _local_app_data = os.environ.get("LOCALAPPDATA", "")
    _real_profile = os.path.join(_local_app_data, "Google", "Chrome", "User Data") if _local_app_data else ""
    _profile_in_use = os.path.exists(os.path.join(_real_profile, "SingletonLock")) if _real_profile else False
    if _real_profile and os.path.isdir(_real_profile) and not _profile_in_use:
        user_data_dir = _real_profile
    else:
        tmp = os.path.join(tempfile.gettempdir(), f"pdf_fetcher_cdp_{port}")
        os.makedirs(tmp, exist_ok=True)
        user_data_dir = tmp

    proc = subprocess.Popen(
        [chrome_exe,
         f"--remote-debugging-port={port}",
         f"--user-data-dir={user_data_dir}",
         "--no-first-run", "--no-default-browser-check",
         article_url],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    pdf_bytes   = b""
    real_url    = ""

    try:
        async with async_playwright() as pw:
            if not await _wait_for_cdp(port):
                result["paywall_reason"] = "CDP timeout"
                return result

            browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0]
            page    = context.pages[0] if context.pages else await context.new_page()

            # Navigate to article if needed
            try:
                cur = page.url
                if not any(d in cur for d in (known_domains or ["__NONE__"])):
                    await page.goto(article_url,
                                    wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Wait for real article page (up to 120 s, handles Cloudflare)
            for i in range(60):
                try:
                    t, u = await page.title(), page.url
                    if not known_domains and "doi.org" not in u:
                        known_domains = _auto_known_domains(u)
                    if _is_real_article(t, u, known_domains):
                        real_url = u
                        break
                except Exception:
                    pass
                await asyncio.sleep(2)

            if not real_url:
                result["paywall_reason"] = "Article page load timeout"
                await browser.close()
                return result

            await asyncio.sleep(2)
            await _accept_cookies(page)
            await asyncio.sleep(1)

            # Discover PDF / viewer URLs
            _, pdf_url   = await _find_pdf_url(page)
            viewer_url   = await _find_viewer_url(page)

            if not pdf_url and not viewer_url:
                result["paywall_reason"] = "No PDF link found on page"
                await browser.close()
                return result
            if not pdf_url:
                pdf_url = viewer_url

            # ── Step 1: API direct fetch (fastest, works for OUP/Nature CDN) ──
            pdf_bytes = await _api_fetch(context, pdf_url, referer=real_url)

            # ── Step 2: Browser navigate to PDF URL + on("response") capture ──
            if not pdf_bytes:
                pdf_bytes = await _navigate_and_capture(
                    page, context, pdf_url, referer=real_url)
                if not pdf_bytes:
                    try:
                        await page.goto(real_url,
                                        wait_until="domcontentloaded", timeout=20_000)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass

            # ── Step 3: Browser navigate to viewer (reader/epdf/showPdf) ──────
            if not pdf_bytes and viewer_url:
                pdf_bytes = await _navigate_and_capture(
                    page, context, viewer_url, referer=real_url)

            # ── Paywall diagnosis ─────────────────────────────────────────────
            if not pdf_bytes:
                if not viewer_url:
                    pw_reason = await _quick_paywall_check(page, pdf_url)
                    if pw_reason:
                        result["paywall"]        = True
                        result["paywall_reason"] = pw_reason
                        await browser.close()
                        return result
                result["paywall_reason"] = "PDF download failed (all steps)"
                await browser.close()
                return result

            await browser.close()

    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        result["paywall_reason"] = "Response is not a valid PDF"
        return result

    # ── Save PDF ──────────────────────────────────────────────────────────────
    if pdf_save_path:
        try:
            pdf_save_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_save_path.write_bytes(pdf_bytes)
            result["downloaded_pdf"] = str(pdf_save_path)
        except Exception:
            pass

    # ── Extract full text ─────────────────────────────────────────────────────
    full_text = _extract_text_from_pdf_bytes(pdf_bytes)
    if full_text.strip():
        result["full_text"]        = full_text
        result["summary_mode"]     = "基于全文/PDF文本提取（patchright）"
        result["acquisition_path"] = "patchright Chrome CDP + PDF"

    # ── Extract Fig1 ──────────────────────────────────────────────────────────
    if fig_save_path:
        try:
            fig_save_path.parent.mkdir(parents=True, exist_ok=True)
            ok = extract_fig1_from_pdf_bytes(pdf_bytes, fig_save_path)
            if ok:
                result["figure_paths"] = [str(fig_save_path)]
                result["figure_items"] = [{"path": str(fig_save_path), "caption": "Figure 1"}]
        except Exception:
            pass

    return result


# ── public sync entry point ───────────────────────────────────────────────────

def fetch_paper_pdf(
    doi_or_url: str,
    *,
    elsevier_api_key: str = "",
    pdf_save_dir: "Path | str | None" = None,
    fig_save_dir: "Path | str | None" = None,
    identifier: str = "",
) -> dict:
    """
    Download PDF and extract full text + Fig1 for any publisher article.

    Parameters
    ----------
    doi_or_url
        DOI string (e.g. "10.1016/j.cell.2023.05.033"), DOI URL, or
        any https article page URL.
    elsevier_api_key
        If provided and doi starts with "10.1016/", the Elsevier Article
        Retrieval API is tried first (via subprocess curl) for full text.
        patchright is still used afterwards to obtain the PDF and Fig1.
    pdf_save_dir
        Directory to save the downloaded PDF.  If None, PDF is not saved to
        disk (bytes are processed in-memory).
    fig_save_dir
        Directory to save the extracted Fig1 PNG.  If None, no figure is saved.
    identifier
        Safe filename base for the saved files.  If "", a timestamp is used.

    Returns
    -------
    dict with keys: full_text, downloaded_pdf, figure_paths, figure_items,
                    summary_mode, acquisition_path, paywall, paywall_reason
    """
    result: dict = {
        "full_text": "",
        "downloaded_pdf": "",
        "figure_paths": [],
        "figure_items": [],
        "summary_mode": "",
        "acquisition_path": "",
        "paywall": False,
        "paywall_reason": "",
    }

    # ── Resolve URL ───────────────────────────────────────────────────────────
    url = doi_or_url.strip()
    if not url:
        return result

    # Normalise plain DOI to URL
    is_doi = bool(re.match(r"^10\.\d{4,}/", url))
    if is_doi and not url.startswith("http"):
        url = f"https://doi.org/{quote(url, safe='/')}"

    # ── Elsevier API path (text only, for 10.1016/ DOIs) ─────────────────────
    doi_plain = ""
    m = re.search(r"(10\.\d{4,}/\S+)", doi_or_url)
    if m:
        doi_plain = m.group(1).rstrip(".,;)")

    if elsevier_api_key and doi_plain.startswith("10.1016/"):
        xml = _elsevier_fetch_xml_via_curl(doi_plain, elsevier_api_key)
        if xml:
            abstract, full_text = _parse_elsevier_xml_simple(xml)
            if full_text.strip():
                result["full_text"]        = full_text[:60_000]
                result["summary_mode"]     = "基于 Elsevier API 全文/XML"
                result["acquisition_path"] = "Elsevier API(view=FULL) via curl"
                # Still fall through to patchright for PDF/Fig1

    # ── bioRxiv / medRxiv direct HTTP (open access, skip patchright) ─────────
    _is_biorxiv = (
        "biorxiv.org" in url or "medrxiv.org" in url
        or doi_plain.startswith("10.1101/")
    )
    if _is_biorxiv and not result["full_text"]:
        biorxiv_result = _fetch_biorxiv_direct(doi_or_url)
        if biorxiv_result.get("full_text"):
            result.update(biorxiv_result)
            # Full text obtained directly — skip heavy patchright pipeline
            return result

    # ── Build save paths ──────────────────────────────────────────────────────
    ident = identifier or f"paper_{int(time.time())}"
    # sanitise: keep alphanum, dash, underscore, dot; replace others with _
    safe_ident = re.sub(r"[^\w\-.]", "_", ident)[:120]

    pdf_save_path: Path | None = None
    if pdf_save_dir:
        pdf_save_path = Path(pdf_save_dir) / (safe_ident + ".pdf")

    fig_save_path: Path | None = None
    if fig_save_dir:
        fig_save_path = Path(fig_save_dir) / (safe_ident + "-fig1.png")

    # ── patchright pipeline ───────────────────────────────────────────────────
    try:
        patchright_result = asyncio.run(
            _run_patchright_pipeline(
                url,
                pdf_save_path=pdf_save_path,
                fig_save_path=fig_save_path,
            )
        )
    except RuntimeError as e:
        # Already running event loop (shouldn't happen in sync code, but guard)
        if "cannot be called from a running event loop" in str(e).lower():
            result["paywall_reason"] = "Cannot run in async context; call _run_patchright_pipeline directly"
            return result
        raise

    # Merge patchright result, preferring Elsevier API text if we got it
    if patchright_result.get("downloaded_pdf"):
        result["downloaded_pdf"] = patchright_result["downloaded_pdf"]
    if patchright_result.get("figure_paths"):
        result["figure_paths"] = patchright_result["figure_paths"]
        result["figure_items"] = patchright_result["figure_items"]
    if patchright_result.get("paywall"):
        result["paywall"]        = patchright_result["paywall"]
        result["paywall_reason"] = patchright_result["paywall_reason"]
    elif patchright_result.get("paywall_reason"):
        result["paywall_reason"] = patchright_result["paywall_reason"]

    # Use patchright full_text only if we don't already have better (Elsevier API) text
    if not result["full_text"] and patchright_result.get("full_text"):
        result["full_text"]        = patchright_result["full_text"]
        result["summary_mode"]     = patchright_result.get("summary_mode", "")
        result["acquisition_path"] = patchright_result.get("acquisition_path", "")
    elif result["full_text"] and patchright_result.get("downloaded_pdf"):
        # Elsevier API text + PDF obtained → upgrade description
        result["summary_mode"]     = "基于 Elsevier API 全文/XML + PDF（patchright）"
        result["acquisition_path"] = "Elsevier API(view=FULL) via curl + patchright PDF"

    return result
