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
import base64
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

from pypdf import PdfReader

from publisher_rules import (
    DOI_PREFIX_SLUG,
    PDF_DOMAINS,
    PUBLISHER_DOMAIN_MAP,
    auto_known_domains,
    doi_from_url_or_text,
    expand_publisher_image_sources,
    publisher_slug_from_url,
    springer_pdfdirect_url,
    wiley_pdfdirect_url,
)


from browser_runtime import (
    _chrome_process_running,
    _copy_chrome_cookies,
    _existing_cdp_url,
    _find_chrome,
    _find_free_port,
    _wait_for_cdp,
)
from html_figures import (
    _extract_html_fig_candidates,
    _extract_wiley_html_text,
    _figure_source_candidates,
    _html_abs_url,
    _score_html_figure_candidate,
)
from pdf_tools import (
    _extract_text_from_pdf_bytes,
    extract_fig1_from_pdf_bytes,
    extract_fig1_from_pdf_path,
)
# ── publisher domain map (for article-page detection) ─────────────────────────
_PUBLISHER_DOMAIN_MAP = PUBLISHER_DOMAIN_MAP
_DOI_PREFIX_SLUG = DOI_PREFIX_SLUG

_CF_MARKERS = [
    "just a moment", "checking your browser", "请稍候",
    "enable javascript", "ray id", "verify you are human",
]


def _publisher_slug_from_url(url: str) -> str:
    return publisher_slug_from_url(url)


def _auto_known_domains(url: str) -> list[str]:
    return auto_known_domains(url)


def _is_cf(title: str) -> bool:
    return any(m in title.lower() for m in _CF_MARKERS)


def _is_real_article(title: str, url: str, known: list[str]) -> bool:
    if not title or len(title) < 8 or _is_cf(title):
        return False
    if known:
        return any(d in url for d in known)
    return "doi.org" not in url


async def _collect_dom_fig_candidates(page, base_url: str) -> list[dict]:
    try:
        rows = await page.evaluate(
            """
            (baseUrl) => {
              const abs = (value) => {
                if (!value) return '';
                try { return new URL(value, baseUrl || location.href).href; } catch (e) { return value || ''; }
              };
              const pick = (img) => {
                if (!img) return '';
                for (const attr of ['data-full-size-image-src', 'currentSrc', 'src', 'data-original', 'data-img-src', 'data-lazy-src', 'data-src', 'srcset', 'data-srcset']) {
                  let raw = attr === 'currentSrc' ? img.currentSrc : (attr === 'src' ? img.src : img.getAttribute(attr));
                  if (!raw) continue;
                  if (attr.endsWith('srcset')) {
                    const parts = raw.split(',').map(x => x.trim().split(/\s+/)[0]).filter(Boolean);
                    raw = parts.length ? parts[parts.length - 1] : '';
                  }
                  const url = abs(raw);
                  if (url) return url;
                }
                return '';
              };
              const out = [];
              const push = (row) => {
                if (!row.src && !row.href) return;
                out.push(row);
              };
              Array.from(document.querySelectorAll('figure, .figure, .fig, [class*="figure"], [id*="fig"]')).slice(0, 40).forEach((node, idx) => {
                const img = node.querySelector('img, source');
                const cap = node.querySelector('figcaption, .caption, [class*="caption"]');
                const link = node.querySelector('a[href*="/view-large/figure/"], a[href*="figure"], a[href]');
                push({
                  index: idx,
                  src: pick(img),
                  href: link ? abs(link.getAttribute('href')) : '',
                  caption: (cap ? cap.innerText : node.innerText || '').trim().slice(0, 700),
                  alt: img ? (img.alt || img.getAttribute('aria-label') || '').trim().slice(0, 500) : '',
                  source: 'dom_figure'
                });
              });
              Array.from(document.querySelectorAll('a[href*="/view-large/figure/"]')).slice(0, 20).forEach((a, idx) => {
                push({
                  index: idx,
                  src: '',
                  href: abs(a.getAttribute('href')),
                  caption: (a.innerText || '').trim().slice(0, 700),
                  alt: '',
                  source: 'dom_view_large'
                });
              });
              let attrIndex = 0;
              Array.from(document.querySelectorAll('*')).slice(0, 3000).forEach((node) => {
                for (const attr of Array.from(node.attributes || [])) {
                  const value = attr.value || '';
                  if (!value.includes('/view-large/figure/')) continue;
                  const match = value.match(/[^\s"'<>]*\/view-large\/figure\/[^\s"'<>]+/);
                  if (!match) continue;
                  push({
                    index: attrIndex++,
                    src: '',
                    href: abs(match[0]),
                    caption: (node.innerText || '').trim().slice(0, 700),
                    alt: '',
                    source: 'dom_view_large_attr'
                  });
                }
              });
              return out;
            }
            """,
            base_url,
        )
        if "academic.oup.com" in (base_url or "").lower() and not any(
            "/view-large/figure/" in f"{row.get('href', '')} {row.get('src', '')}"
            for row in (rows or [])
        ):
            original_url = page.url
            for selector in (
                "img[src*='f1']",
                "img[src*='fig1']",
                "img[src*='m_']",
                "figure img",
            ):
                try:
                    loc = page.locator(selector).first
                    if not await loc.count():
                        continue
                    await loc.scroll_into_view_if_needed(timeout=5_000)
                    await loc.click(timeout=5_000)
                    await page.wait_for_timeout(2_000)
                    extra = await page.evaluate(
                        """
                        (baseUrl) => {
                          const abs = (value) => {
                            if (!value) return '';
                            try { return new URL(value, baseUrl || location.href).href; } catch (e) { return value || ''; }
                          };
                          const out = [];
                          const push = (href, caption, source) => {
                            if (!href) return;
                            out.push({ index: out.length, src: '', href: abs(href), caption: caption || '', alt: '', source });
                          };
                          Array.from(document.querySelectorAll('a[href*="/view-large/figure/"]')).forEach((a) => {
                            push(a.getAttribute('href'), (a.innerText || '').trim().slice(0, 700), 'oup_viewer_anchor');
                          });
                          Array.from(document.querySelectorAll('*')).slice(0, 3000).forEach((node) => {
                            for (const attr of Array.from(node.attributes || [])) {
                              const value = attr.value || '';
                              if (!value.includes('/view-large/figure/')) continue;
                              const match = value.match(/[^\\s"'<>]*\/view-large\/figure\/[^\\s"'<>]+/);
                              if (match) push(match[0], (node.innerText || '').trim().slice(0, 700), 'oup_viewer_attr');
                            }
                          });
                          return out;
                        }
                        """,
                        base_url,
                    )
                    if extra:
                        rows.extend(extra)
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    if page.url != original_url:
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                        except Exception:
                            pass
                    if extra:
                        break
                except Exception:
                    continue
    except Exception:
        return []
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows or []:
        item = {
            "index": int(row.get("index", 0) or 0),
            "src": _html_abs_url(str(row.get("src", "") or ""), base_url),
            "href": _html_abs_url(str(row.get("href", "") or ""), base_url),
            "caption": str(row.get("caption", "") or ""),
            "alt": str(row.get("alt", "") or ""),
            "source": str(row.get("source", "dom_figure") or "dom_figure"),
        }
        item["source_candidates"] = _figure_source_candidates(item)
        if not item["source_candidates"]:
            continue
        item["score"] = _score_html_figure_candidate(item)
        key = (item["source_candidates"][0], item.get("caption", ""))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)
    candidates.sort(key=_score_html_figure_candidate, reverse=True)
    return candidates[:8]


async def _page_fetch_image_bytes(page, url: str) -> tuple[int, str, bytes]:
    try:
        payload = await page.evaluate(
            """
            async (url) => {
              const resp = await fetch(url, { credentials: 'include', headers: { 'Accept': 'image/*,*/*' } });
              const ct = resp.headers.get('content-type') || '';
              const buf = await resp.arrayBuffer();
              const bytes = new Uint8Array(buf);
              let binary = '';
              const chunk = 0x8000;
              for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
              }
              return { status: resp.status, contentType: ct, b64: btoa(binary) };
            }
            """,
            url,
        )
        data = base64.b64decode(payload.get("b64", "") or "")
        return int(payload.get("status", 0) or 0), str(payload.get("contentType", "") or ""), data
    except Exception:
        return 0, "", b""


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if pos + 2 > len(data):
                break
            seg_len = int.from_bytes(data[pos:pos + 2], "big")
            if seg_len < 2 or pos + seg_len > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[pos + 3:pos + 5], "big")
                width = int.from_bytes(data[pos + 5:pos + 7], "big")
                return width, height
            pos += seg_len
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
    return 0, 0


def _acceptable_image_bytes(data: bytes, content_type: str) -> bool:
    content_type = (content_type or "").lower()
    if not content_type.startswith("image/") or len(data) <= 5_000:
        return False
    # Figure assets must be raster files. Publisher pages often return an SVG
    # placeholder/spinner with HTTP 200 and image/svg+xml; accepting it creates
    # a misleading .jpg path that later existence-only checks report as success.
    raster_signature = (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )
    if not raster_signature:
        return False
    width, height = _image_dimensions_from_bytes(data)
    if width and height and max(width, height) < 700:
        return False
    return True


def _image_extension_from_response(url: str, content_type: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        ct = (content_type or "").lower()
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        elif "gif" in ct:
            ext = ".gif"
        else:
            ext = ".jpg"
    if "jpeg" in (content_type or "").lower() and ext not in {".jpg", ".jpeg"}:
        ext = ".jpg"
    return ext


def _plain_http_image_bytes(url: str, referer: str) -> tuple[int, str, bytes]:
    try:
        from urllib.request import Request, urlopen

        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": referer or url,
            },
        )
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            ct = resp.headers.get("content-type", "") or ""
            return int(getattr(resp, "status", 200) or 200), ct, data
    except Exception:
        return 0, "", b""


async def _try_download_image_url(
    page,
    context,
    url: str,
    referer: str,
    out_path: "Path",
) -> "tuple[bool, str, str]":
    try:
        resp = await context.request.get(
            url,
            headers={"Referer": referer, "Accept": "image/*,*/*"},
            timeout=20_000,
        )
        data = await resp.body()
        ct = resp.headers.get("content-type", "") or ""
        if resp.status == 200 and _acceptable_image_bytes(data, ct):
            actual = Path(out_path).with_suffix(_image_extension_from_response(url, ct))
            actual.parent.mkdir(parents=True, exist_ok=True)
            actual.write_bytes(data)
            return True, str(actual), url
        if resp.status == 200 and "html" in ct.lower() and len(data) > 500:
            html = data.decode("utf-8", errors="replace")
            for nested in _image_urls_from_html(html, url):
                ok, saved, source = await _try_download_image_url(page, context, nested, url, out_path)
                if ok:
                    return ok, saved, source
    except Exception:
        pass

    status, ct, data = _plain_http_image_bytes(url, referer)
    if status == 200 and _acceptable_image_bytes(data, ct):
        actual = Path(out_path).with_suffix(_image_extension_from_response(url, ct))
        actual.parent.mkdir(parents=True, exist_ok=True)
        actual.write_bytes(data)
        return True, str(actual), url

    try:
        status, ct, data = await asyncio.wait_for(_page_fetch_image_bytes(page, url), timeout=15)
    except Exception:
        status, ct, data = 0, "", b""
    if status == 200 and _acceptable_image_bytes(data, ct):
        actual = Path(out_path).with_suffix(_image_extension_from_response(url, ct))
        actual.parent.mkdir(parents=True, exist_ok=True)
        actual.write_bytes(data)
        return True, str(actual), url
    return False, "", ""


async def _try_download_html_fig1(
    page,
    context,
    item: dict,
    referer: str,
    out_path: "Path",
) -> "tuple[bool, str, str]":
    """Download one real image file for the top HTML figure candidate."""
    for src in _figure_source_candidates(item):
        ok, saved, source = await _try_download_image_url(page, context, src, referer, out_path)
        if ok:
            return ok, saved, source
    return False, "", ""


def _doi_from_url_or_text(value: str) -> str:
    return doi_from_url_or_text(value)


def _wiley_pdfdirect_url(article_url: str) -> str:
    return wiley_pdfdirect_url(article_url)


def _springer_pdfdirect_url(article_url: str) -> str:
    return springer_pdfdirect_url(article_url)


async def _try_fetch_wiley_html_fig1_via_context(
    page,
    context,
    article_url: str,
    out_path: "Path",
) -> "tuple[bool, str, str, str, str]":
    """Fetch Wiley article HTML through the browser context and download Fig1.

    Wiley's CMS image URLs are Cloudflare-protected for plain HTTP clients, but
    the Playwright context can reuse the persistent cf_clearance cookie.  This
    path avoids waiting on the full page/PDF workflow just to obtain Fig1.
    """
    if "onlinelibrary.wiley.com" not in (article_url or "").lower():
        return False, "", "", "", ""
    try:
        resp = await context.request.get(
            article_url,
            headers={"Accept": "text/html,*/*"},
            timeout=20_000,
        )
        data = await resp.body()
        ct = resp.headers.get("content-type", "") or ""
        if resp.status != 200 or "html" not in ct.lower() or len(data) < 5_000:
            return False, "", "", "", ""
        html = data.decode("utf-8", errors="replace")
        lower = html.lower()
        if any(marker in lower for marker in _CF_MARKERS):
            return False, "", "", "", ""
        full_text = _extract_wiley_html_text(html)
        candidates = sorted(
            _extract_html_fig_candidates(html, article_url),
            key=_score_html_figure_candidate,
            reverse=True,
        )
        for cand in candidates[:6]:
            ok, saved, source = await _try_download_html_fig1(
                page, context, cand, article_url, out_path
            )
            if ok:
                caption = cand.get("caption") or cand.get("alt") or "Figure 1"
                return True, saved, source, caption, full_text
    except Exception:
        pass
    return False, "", "", "", ""


# ── PDF-link discovery ────────────────────────────────────────────────────────
_PDF_DOMAINS = PDF_DOMAINS


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

    try:
        b = await asyncio.wait_for(_js_fetch(page, pdf_url), timeout=30)
    except Exception:
        b = b""
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


async def _safe_close_handle(handle, timeout: int = 10) -> None:
    try:
        await asyncio.wait_for(handle.close(), timeout=timeout)
    except Exception:
        pass

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


def _elsevier_fetch_pdf_via_curl(
    doi: str,
    api_key: str,
    save_path: Path,
    timeout: int = 90,
) -> str:
    """Download an entitled Elsevier PDF through Article Retrieval API."""
    api_url = (
        f"https://api.elsevier.com/content/article/doi/{quote(doi, safe='/')}"
        f"?apiKey={quote(api_key, safe='')}&httpAccept=application/pdf&view=FULL"
    )
    try:
        completed = subprocess.run(
            [
                "curl", "-L", "-sS", "--max-time", str(timeout),
                "-H", f"X-ELS-APIKey: {api_key}",
                "-H", "Accept: application/pdf",
                api_url,
            ],
            capture_output=True,
            timeout=timeout + 10,
        )
        payload = completed.stdout or b""
        try:
            page_count = len(PdfReader(io.BytesIO(payload), strict=False).pages)
        except Exception:
            page_count = 0
        if (
            completed.returncode != 0
            or not payload[:8].lstrip().startswith(b"%PDF")
            or page_count < 2
        ):
            return ""
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(payload)
        return str(save_path)
    except Exception:
        return ""


def _elsevier_fetch_fig1_via_curl(
    xml_text: str,
    save_path: Path,
    timeout: int = 60,
) -> tuple[str, str]:
    """Download Elsevier Figure 1 from the stable PII-backed image CDN."""
    pii = _compact_elsevier_pii(_elsevier_pii_from_xml(xml_text))
    if not pii:
        return "", ""
    source_url = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}-gr1.jpg"
    try:
        completed = subprocess.run(
            ["curl", "-L", "-sS", "--max-time", str(timeout), source_url],
            capture_output=True,
            timeout=timeout + 10,
        )
        payload = completed.stdout or b""
        is_image = payload.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
        if completed.returncode != 0 or len(payload) < 2_000 or not is_image:
            return "", ""
        target = save_path.with_suffix(".jpg") if payload.startswith(b"\xff\xd8\xff") else save_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return str(target), source_url
    except Exception:
        return "", ""



_CELL_PRESS_DOI_SLUGS: dict[str, str] = {
    "cell": "cell",
    "celrep": "cell-reports",
    "heliyon": "heliyon",
    "isci": "iscience",
    "cub": "current-biology",
    "ajhg": "ajhg",
    "neuron": "neuron",
    "molcel": "molecular-cell",
    "devcel": "developmental-cell",
    "stem": "cell-stem-cell",
    "immuni": "immunity",
    "cmet": "cell-metabolism",
    "chom": "cell-host-microbe",
    "xgen": "cell-genomics",
    "xcrm": "cell-reports-methods",
}

_CELL_PRESS_PUBLICATION_SLUGS: dict[str, str] = {
    "cell": "cell",
    "cell reports": "cell-reports",
    "heliyon": "heliyon",
    "iscience": "iscience",
    "current biology": "current-biology",
    "the american journal of human genetics": "ajhg",
    "neuron": "neuron",
    "molecular cell": "molecular-cell",
    "developmental cell": "developmental-cell",
    "cell stem cell": "cell-stem-cell",
    "immunity": "immunity",
    "cell metabolism": "cell-metabolism",
    "cell host & microbe": "cell-host-microbe",
    "cell genomics": "cell-genomics",
    "cell reports methods": "cell-reports-methods",
}


def _elsevier_xml_text(xml_text: str, tag: str) -> str:
    patterns = [
        rf"<{re.escape(tag)}[^>]*>(.*?)</{re.escape(tag)}>",
        rf"<[^:>]+:{re.escape(tag)}[^>]*>(.*?)</[^:>]+:{re.escape(tag)}>",
    ]
    for pattern in patterns:
        match = re.search(pattern, xml_text or "", re.S | re.I)
        if match:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()
    return ""


def _compact_elsevier_pii(pii: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", pii or "")


def _elsevier_pii_from_xml(xml_text: str) -> str:
    pii = _elsevier_xml_text(xml_text, "pii")
    if pii:
        return pii
    for pattern in (r"/pii/(S\d+)", r"1-s2\.0-(S\d+)"):
        match = re.search(pattern, xml_text or "", re.I)
        if match:
            return match.group(1)
    return ""


def _cell_press_slug_for_elsevier(doi: str, publication_name: str) -> str:
    doi_match = re.match(r"10\.1016/j\.([A-Za-z0-9]+)", doi or "", re.I)
    if doi_match:
        slug = _CELL_PRESS_DOI_SLUGS.get(doi_match.group(1).lower())
        if slug:
            return slug
    key = re.sub(r"\s+", " ", (publication_name or "").strip().lower())
    return _CELL_PRESS_PUBLICATION_SLUGS.get(key, "")


def _elsevier_article_url_from_xml(doi: str, xml_text: str) -> str:
    pii = _elsevier_pii_from_xml(xml_text)
    if not pii:
        return ""
    publication = _elsevier_xml_text(xml_text, "publicationName")
    cell_slug = _cell_press_slug_for_elsevier(doi, publication)
    if cell_slug:
        return f"https://www.cell.com/{cell_slug}/fulltext/{quote(pii, safe='()-')}"
    compact = _compact_elsevier_pii(pii)
    if compact:
        return f"https://www.sciencedirect.com/science/article/pii/{compact}"
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
    Open article_url with patchright, find PDF link, download PDF, extract
    text and Fig1.  Returns result dict.

    Browser strategy (priority order):
      1. Existing CDP Chrome (PAPER_READER_CDP_URL / ports 9222-9240)
         → attaches to user's running Chrome; all session cookies available.
      2. patchright launch_persistent_context  ← NEW DEFAULT
         → patchright launches its own Chromium with full fingerprint patches;
           navigator.webdriver is hidden, headless markers removed.
         → Cloudflare Turnstile passes automatically (~15s first visit,
           ~5s on repeats because cf_clearance is stored in the profile).
         → Profile is keyed by publisher slug so each site accumulates its own
           clearance cookies independently.
    """
    from patchright.async_api import async_playwright  # lazy import

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

    known_domains = _auto_known_domains(article_url)

    # ── Priority 1: attach to an already-running CDP-enabled Chrome ──────────
    existing_cdp = _existing_cdp_url()

    pdf_bytes = b""
    real_url  = ""
    _html_fig1_saved = False

    try:
        async with async_playwright() as pw:

            if existing_cdp:
                # ── CDP path: connect to user's Chrome ────────────────────────
                browser = await pw.chromium.connect_over_cdp(existing_cdp)
                context = browser.contexts[0]
                page    = context.pages[0] if context.pages else await context.new_page()
                result["cookie_source"] = "existing_cdp_browser"
                _close_context = False   # don't close browser we didn't open

                if fig_save_path and not _html_fig1_saved:
                    _ok, _saved, _source_url, _caption, _wiley_text = await _try_fetch_wiley_html_fig1_via_context(
                        page, context, article_url, fig_save_path
                    )
                    if _ok:
                        _html_fig1_saved = True
                        result["figure_paths"] = [_saved]
                        result["figure_items"] = [{
                            "path":       _saved,
                            "caption":    _caption,
                            "source_url": _source_url,
                        }]
                        if _wiley_text:
                            result["full_text"] = _wiley_text
                            result["summary_mode"] = "基于 Wiley HTML 全文/网页正文"
                            result["acquisition_path"] = "Wiley HTML + Fig1 via browser context"
                            result["page_url"] = article_url
                            # Keep going: Wiley HTML/Fig1 is useful, but the
                            # production reader must still try pdfdirect.

                try:
                    cur = page.url
                    if not any(d in cur for d in (known_domains or ["__NONE__"])):
                        await page.goto(article_url,
                                        wait_until="domcontentloaded", timeout=60_000)
                        await page.wait_for_timeout(3000)
                except Exception:
                    pass

            else:
                # ── patchright launch_persistent_context ──────────────────────
                # Use a persistent profile keyed by publisher slug.
                # cf_clearance and institutional session cookies survive
                # across runs; explicit captcha pages still need manual verification.
                _slug = _publisher_slug_from_url(article_url)
                _profile_dir = os.path.join(
                    tempfile.gettempdir(),
                    f"pdf_fetcher_pw_{_slug}",
                )
                os.makedirs(_profile_dir, exist_ok=True)
                result["cookie_source"] = (
                    "patchright_persistent_profile"
                    if os.path.isdir(_profile_dir) else
                    "patchright_new_profile"
                )

                context = await asyncio.wait_for(
                    pw.chromium.launch_persistent_context(
                        _profile_dir,
                        headless=False,       # visible window; Cloudflare JS checks pass
                        no_viewport=True,
                        args=[
                            "--lang=en-US,en",
                            "--disable-blink-features=AutomationControlled",
                            "--no-first-run",
                        ],
                    ),
                    timeout=180,
                )
                _close_context = True

                page = context.pages[0] if context.pages else await context.new_page()

                if fig_save_path and not _html_fig1_saved:
                    _ok, _saved, _source_url, _caption, _wiley_text = await _try_fetch_wiley_html_fig1_via_context(
                        page, context, article_url, fig_save_path
                    )
                    if _ok:
                        _html_fig1_saved = True
                        result["figure_paths"] = [_saved]
                        result["figure_items"] = [{
                            "path":       _saved,
                            "caption":    _caption,
                            "source_url": _source_url,
                        }]
                        if _wiley_text:
                            result["full_text"] = _wiley_text
                            result["summary_mode"] = "基于 Wiley HTML 全文/网页正文"
                            result["acquisition_path"] = "Wiley HTML + Fig1 via browser context"
                            result["page_url"] = article_url
                            # Keep going: Wiley HTML/Fig1 is useful, but the
                            # production reader must still try pdfdirect.

                try:
                    await page.goto(article_url,
                                    wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

            # ── Wait for real article page (up to 120s, handles Cloudflare) ──
            _direct_pdf_url = _wiley_pdfdirect_url(article_url) or _springer_pdfdirect_url(article_url)
            if _direct_pdf_url:
                # Some publishers expose stable PDF endpoints.  Do not spend two
                # minutes trying to prove the page is "real" before using them.
                try:
                    real_url = page.url or article_url
                except Exception:
                    real_url = article_url
            else:
                _real_wait_ticks = 20 if (
                    _html_fig1_saved and "onlinelibrary.wiley.com" in article_url.lower()
                ) else 60
                for i in range(_real_wait_ticks):
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
                    if _close_context:
                        await _safe_close_handle(context)
                    else:
                        await _safe_close_handle(browser)
                    return result

            await asyncio.sleep(2)
            await _accept_cookies(page)
            await asyncio.sleep(1)

            # Scroll to trigger lazy-loading of figures before capturing HTML
            try:
                for _ in range(5):
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(400)
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Capture rendered HTML (after scroll, CDN img URLs are resolved)
            _html_content = ""
            try:
                _html_content = await page.content()
                result["page_html"] = _html_content[:300_000]
                result["page_url"]  = real_url
            except Exception:
                pass

            # ── HTML Fig1 extraction (preferred: CDN download via context) ────
            # Try to download Figure 1 straight from the rendered page HTML.
            # This works even when PDF is paywalled, and preserves full
            # resolution without re-encoding through fitz.
            if not _html_fig1_saved and _html_content and fig_save_path:
                _dom_fig_cands = await _collect_dom_fig_candidates(page, real_url)
                _fig_cands = _dom_fig_cands + _extract_html_fig_candidates(_html_content, real_url)
                _deduped_fig_cands = []
                _seen_fig_sources = set()
                for _cand in sorted(_fig_cands, key=_score_html_figure_candidate, reverse=True):
                    _sources = _figure_source_candidates(_cand)
                    if not _sources or _sources[0] in _seen_fig_sources:
                        continue
                    _seen_fig_sources.add(_sources[0])
                    _deduped_fig_cands.append(_cand)
                _fig_cands = _deduped_fig_cands
                for _fig_cand in _fig_cands[:4]:
                    _ok, _saved, _source_url = await _try_download_html_fig1(
                        page, context, _fig_cand, real_url, fig_save_path
                    )
                    if _ok:
                        _html_fig1_saved = True
                        result["figure_paths"] = [_saved]
                        result["figure_items"] = [{
                            "path":       _saved,
                            "caption":    _fig_cand.get("caption", "Figure 1"),
                            "source_url": _source_url,
                        }]
                        break

            # ── Discover PDF / viewer URLs ────────────────────────────────────
            if _direct_pdf_url:
                pdf_url = _direct_pdf_url
                viewer_url = ""
            else:
                _, pdf_url  = await _find_pdf_url(page)
                viewer_url  = await _find_viewer_url(page)

            if not pdf_url and not viewer_url:
                if _html_fig1_saved:
                    result["paywall_reason"] = "No PDF link found on page (Fig1 obtained from HTML)"
                else:
                    result["paywall_reason"] = "No PDF link found on page"
                if _close_context:
                    await _safe_close_handle(context)
                else:
                    await _safe_close_handle(browser)
                return result
            if not pdf_url:
                pdf_url = viewer_url

            # ── Step 1: API direct fetch ──────────────────────────────────────
            pdf_bytes = await _api_fetch(context, pdf_url, referer=real_url)

            # ── Step 2: Browser navigate to PDF URL + response capture ────────
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

            # ── Step 3: Browser navigate to viewer (epdf/showPdf) ────────────
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
                        if _close_context:
                            await _safe_close_handle(context)
                        else:
                            await _safe_close_handle(browser)
                        return result
                if _html_fig1_saved:
                    result["paywall_reason"] = "PDF download failed (Fig1 obtained from HTML)"
                else:
                    result["paywall_reason"] = "PDF download failed (all steps)"
                if _close_context:
                    await _safe_close_handle(context)
                else:
                    await _safe_close_handle(browser)
                return result

            if _close_context:
                await _safe_close_handle(context)
            else:
                await _safe_close_handle(browser)

    except Exception as _outer_exc:
        if not result["paywall_reason"]:
            result["paywall_reason"] = f"patchright error: {_outer_exc}"
        return result

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

    # ── Extract Fig1 from PDF (fitz/PyMuPDF) ─────────────────────────────────
    # Only run fitz extraction if the HTML CDN download did not already succeed.
    # HTML CDN images are higher resolution and lossless; fitz rendering is the
    # fallback for publishers that lazy-load figures or hide them behind auth.
    if fig_save_path and not _html_fig1_saved:
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

    elsevier_xml = ""
    # Extract DOI before URL normalization so publisher-specific DOI
    # shortcuts can avoid slow or challenge-prone doi.org redirects.
    doi_plain = ""
    m = re.search(r"(10\.\d{4,}/\S+)", doi_or_url)
    if m:
        doi_plain = m.group(1).rstrip(".,;)")

    # Normalise Wiley DOI/DOI URL directly to the publisher page. run_reader.py
    # calls this helper with a doi.org URL, so this must not be limited to bare DOI
    # input; otherwise Wiley falls back to the slow doi.org/browser path.
    if doi_plain.startswith(("10.1111/", "10.1002/")):
        url = f"https://onlinelibrary.wiley.com/doi/{quote(doi_plain, safe='/')}"
    elif doi_plain.startswith(("10.1186/", "10.1007/")):
        url = f"https://link.springer.com/article/{quote(doi_plain, safe='/')}"

    # Normalise plain DOI to URL.
    is_doi = bool(re.match(r"^10\.\d{4,}/", url))
    if is_doi and not url.startswith("http"):
        url = f"https://doi.org/{quote(url, safe='/')}"

    # ── Elsevier API path (text only, for 10.1016/ DOIs) ─────────────────────

    if elsevier_api_key and doi_plain.startswith("10.1016/"):
        xml = _elsevier_fetch_xml_via_curl(doi_plain, elsevier_api_key)
        if xml:
            elsevier_xml = xml
            article_url = _elsevier_article_url_from_xml(doi_plain, xml)
            if article_url:
                url = article_url
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

    # Elsevier's Article Retrieval API can return the entitled PDF directly.
    # The same XML response exposes the PII used by the stable Figure 1 CDN.
    # Prefer these deterministic assets over a challenge-prone browser page.
    if elsevier_xml and elsevier_api_key:
        if pdf_save_path:
            saved_pdf = _elsevier_fetch_pdf_via_curl(
                doi_plain, elsevier_api_key, pdf_save_path
            )
            if saved_pdf:
                result["downloaded_pdf"] = saved_pdf
                result["summary_mode"] = "基于 Elsevier API 全文/XML + PDF"
                result["acquisition_path"] = "Elsevier API(view=FULL XML + PDF)"
        if fig_save_path:
            saved_fig, fig_url = _elsevier_fetch_fig1_via_curl(
                elsevier_xml, fig_save_path
            )
            if saved_fig:
                result["figure_paths"] = [saved_fig]
                result["figure_items"] = [{
                    "path": saved_fig,
                    "caption": "Figure 1",
                    "source_url": fig_url,
                }]
        if result["downloaded_pdf"] and (not fig_save_path or result["figure_paths"]):
            return result

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
