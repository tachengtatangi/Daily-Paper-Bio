"""HTML figure candidate extraction for publisher article pages."""

from __future__ import annotations

import re

from publisher_rules import expand_publisher_image_sources

def _html_attr_map(tag_text: str) -> dict:
    from html import unescape

    attrs: dict[str, str] = {}
    for match in re.finditer(r'''([A-Za-z0-9_:\-]+)\s*=\s*(["'])(.*?)\2''', tag_text, re.S):
        attrs[match.group(1).lower()] = unescape(match.group(3).strip())
    return attrs

def _html_abs_url(value: str, base_url: str) -> str:
    from urllib.parse import urljoin as _urljoin

    value = (value or "").strip()
    if not value or value.startswith("data:"):
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return _urljoin(base_url, value)

def _pick_src_from_attrs(attrs: dict, base_url: str) -> str:
    for key in (
        "data-full-size-image-src", "data-original", "data-img-src",
        "data-lazy-src", "data-src", "src", "srcset", "data-srcset",
    ):
        raw = (attrs.get(key) or "").strip()
        if not raw:
            continue
        if key.endswith("srcset"):
            parts = [item.strip().split()[0] for item in raw.split(",") if item.strip()]
            raw = parts[-1] if parts else ""
        src = _html_abs_url(raw, base_url)
        if src:
            return src
    return ""

def _expand_wiley_cms_sources(src: str) -> list[str]:
    return expand_publisher_image_sources(src)

def _figure_source_candidates(item: dict) -> list[str]:
    sources: list[str] = []

    def add(value: str) -> None:
        for expanded in _expand_wiley_cms_sources(value):
            if expanded and expanded not in sources:
                sources.append(expanded)

    for value in item.get("source_candidates", []) or []:
        add(value)
    for key in ("src", "href"):
        add(item.get(key, ""))
    return sources

def _score_html_figure_candidate(item: dict) -> int:
    src_hay = " ".join(str(item.get(key, "") or "") for key in ("src", "href")).lower()
    text_hay = " ".join(str(item.get(key, "") or "") for key in ("caption", "alt")).lower()
    hay = f"{src_hay} {text_hay}"
    score = 0
    if item.get("source") in {"figure", "dom_figure", "dom_view_large"}:
        score += 20
    if re.search(r"(?:^|[-_/])(?:fig(?:ure)?[-_]?0?1|f0?1|gr1)(?:\.|[-_/]|$)", src_hay):
        score += 220
    elif re.search(r"[A-Za-z0-9]+f0?1\.(?:jpe?g|png|webp|gif)", src_hay):
        score += 180
    elif re.match(r"\s*(?:fig(?:ure)?\.?\s*1\b|1[.)])", text_hay):
        score += 120
    elif re.search(r"\bfig(?:ure)?\.?\s*1\b", text_hay):
        score += 35
    if "/view-large/figure/" in src_hay:
        score += 100
    if re.search(r"(?:^|[-_/])(?:fig(?:ure)?[-_]?[2-9]|f[2-9]|gr[2-9])(?:\.|[-_/]|$)", src_hay):
        score -= 120
    elif re.search(r"[A-Za-z0-9]+f[2-9]\.(?:jpe?g|png|webp|gif)", src_hay):
        score -= 120
    if re.search(r"graphical abstract|ga1\.|[-_/]fa\.|unfig", hay):
        score -= 90
    if any(token in hay for token in ("logo", "spinner", "icon", "avatar", "advert")):
        score -= 200
    return score

def _image_urls_from_html(html: str, base_url: str) -> list[str]:
    urls: list[str] = []

    def add(value: str) -> None:
        value = _html_abs_url(value, base_url)
        if value and value not in urls:
            urls.append(value)

    for tag_match in re.finditer(r"<(?:img|source)\b[^>]*>", html, re.S | re.I):
        attrs = _html_attr_map(tag_match.group(0))
        add(_pick_src_from_attrs(attrs, base_url))
    for match in re.finditer(r"https?://[^\"'<>\s]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\"'<>\s]+)?", html, re.I):
        add(match.group(0))
    urls.sort(key=lambda value: ("/m_" in value.lower(), -_score_html_figure_candidate({"src": value})))
    return urls

def _extract_wiley_html_text(html: str) -> str:
    """Extract usable article text from Wiley HTML without waiting on the page."""
    from html import unescape

    html = html or ""
    if not html:
        return ""
    parts: list[str] = []

    for pattern in (
        r'<meta\s+name=["\']citation_abstract["\']\s+content=["\'](.*?)["\']',
        r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']',
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
    ):
        match = re.search(pattern, html, re.S | re.I)
        if match:
            text = re.sub(r"\s+", " ", unescape(match.group(1))).strip()
            if len(text) > 80:
                parts.append(text)
                break

    body = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<(nav|footer|header|aside)\b.*?</\1>", " ", body, flags=re.S | re.I)
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", body, flags=re.S | re.I)
    in_refs = False
    for para in paras:
        clean = re.sub(r"<[^>]+>", " ", para)
        clean = re.sub(r"\s+", " ", unescape(clean)).strip()
        if len(clean) < 50:
            continue
        low = clean.lower()
        if re.match(r"^(references|literature cited|supporting information)\b", low):
            in_refs = True
        if in_refs:
            continue
        if any(skip in low for skip in ("cookie", "privacy policy", "advertisement", "sign in to access")):
            continue
        parts.append(clean)
        if sum(len(p) for p in parts) > 80_000:
            break

    text = "\n\n".join(parts)
    return text[:80_000]

def _extract_html_fig_candidates(html: str, base_url: str) -> list[dict]:
    """Return ranked image candidates for the article's primary figure.

    The candidates are still just URLs and captions; actual bytes are downloaded
    later.  That lets us use publisher-specific fallbacks such as OUP view-large
    pages and page-context fetches for Cloudflare-protected CMS assets.
    """
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    tag_strip = re.compile(r"<[^>]+>")

    def add_candidate(item: dict) -> None:
        sources = _figure_source_candidates(item)
        if not sources:
            return
        key = (sources[0], item.get("caption", ""))
        if key in seen:
            return
        seen.add(key)
        item["source_candidates"] = sources
        item["score"] = _score_html_figure_candidate(item)
        if item["score"] > -100:
            candidates.append(item)

    for idx, fm in enumerate(re.finditer(r"<figure\b[^>]*>(.*?)</figure>", html, re.S | re.I)):
        inner = fm.group(1)
        cap_m = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", inner, re.S | re.I)
        caption = ""
        if cap_m:
            caption = re.sub(r"\s+", " ", tag_strip.sub(" ", cap_m.group(1))).strip()[:700]
        href = ""
        href_m = re.search(r'''\bhref\s*=\s*(["'])(.*?)\1''', inner, re.S | re.I)
        if href_m:
            href = _html_abs_url(href_m.group(2), base_url)
        srcs: list[str] = []
        alts: list[str] = []
        for tag_match in re.finditer(r"<(?:img|source)\b[^>]*>", inner, re.S | re.I):
            attrs = _html_attr_map(tag_match.group(0))
            src = _pick_src_from_attrs(attrs, base_url)
            if src and src not in srcs:
                srcs.append(src)
            alt = (attrs.get("alt") or attrs.get("aria-label") or "").strip()
            if alt and alt not in alts:
                alts.append(alt)
        add_candidate({
            "index": idx,
            "src": srcs[0] if srcs else "",
            "href": href,
            "caption": caption,
            "alt": " ".join(alts)[:500],
            "source": "figure",
            "source_candidates": srcs + ([href] if href else []),
        })

    # Some publishers render key figures as image blocks rather than semantic
    # <figure> elements.  Keep this broad but rank aggressively so logos and
    # graphical abstracts do not beat Fig. 1.
    for idx, tag_match in enumerate(re.finditer(r"<img\b[^>]*>", html, re.S | re.I)):
        attrs = _html_attr_map(tag_match.group(0))
        src = _pick_src_from_attrs(attrs, base_url)
        if not src:
            continue
        alt = (attrs.get("alt") or attrs.get("aria-label") or "").strip()
        hay = f"{src} {alt}".lower()
        if not any(token in hay for token in ("fig", "figure", "mediaobjects", "cms/", "gr1", "f1", "silverchair-cdn")):
            continue
        add_candidate({
            "index": idx,
            "src": src,
            "href": "",
            "caption": "",
            "alt": alt[:500],
            "source": "image",
            "source_candidates": [src],
        })

    # OUP/Silverchair may keep the full-size figure page in viewer config rather
    # than a visible anchor.
    for idx, match in enumerate(re.finditer(r"""[^\s"'<>]*/view-large/figure/[^\s"'<>]+""", html, re.I)):
        href = _html_abs_url(match.group(0), base_url)
        add_candidate({
            "index": idx,
            "src": "",
            "href": href,
            "caption": "",
            "alt": "",
            "source": "html_view_large",
            "source_candidates": [href],
        })

    # OUP/Silverchair commonly exposes figure image URLs in JSON-LD/meta fields
    # instead of visible <figure> nodes in the captured HTML.
    for idx, src in enumerate(_image_urls_from_html(html, base_url)):
        hay = src.lower()
        if not any(token in hay for token in ("fig", "figure", "mediaobjects", "cms/", "gr1", "f1", "silverchair-cdn")):
            continue
        add_candidate({
            "index": idx,
            "src": src,
            "href": "",
            "caption": "",
            "alt": "",
            "source": "html_image_url",
            "source_candidates": [src],
        })

    candidates.sort(key=_score_html_figure_candidate, reverse=True)
    return candidates[:8]
