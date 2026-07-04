"""
Publisher asset rules for paper-reader.

This module keeps publisher-specific URL and DOI rules out of pdf_fetcher.py so
F12-derived patterns can be reviewed and updated in one place.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlparse


# Article-page domains that should share a persistent browser profile and URL
# matching hints.  Values are intentionally broad enough to cover common CDN and
# branded hosts used by the same publisher family.
PUBLISHER_DOMAIN_MAP: dict[str, list[str]] = {
    "pnas.org": ["pnas.org"],
    "science.org": ["science.org", "sciencemag.org"],
    "sciencemag.org": ["science.org", "sciencemag.org"],
    "cell.com": ["cell.com", "sciencedirect.com"],
    "sciencedirect.com": ["cell.com", "sciencedirect.com"],
    "nature.com": ["nature.com"],
    "academic.oup.com": ["academic.oup.com", "oup.com"],
    "springer.com": ["springer.com"],
    "wiley.com": ["wiley.com", "onlinelibrary.wiley.com"],
    "onlinelibrary.wiley": ["wiley.com", "onlinelibrary.wiley.com"],
    "biorxiv.org": ["biorxiv.org"],
    "medrxiv.org": ["medrxiv.org"],
    "elifesciences.org": ["elifesciences.org"],
    "frontiersin.org": ["frontiersin.org"],
    "mdpi.com": ["mdpi.com"],
    "plos.org": ["plos.org"],
    "bmj.com": ["bmj.com"],
    "thelancet.com": ["thelancet.com"],
}


# DOI-prefix to persistent browser-profile slug.  Keep one value per prefix:
# duplicate keys are silent in Python dicts and make troubleshooting harder.
DOI_PREFIX_SLUG: dict[str, str] = {
    "10.1126": "science_org",
    "10.1038": "nature_com",
    "10.1002": "wiley_com",
    "10.1093": "oup_com",
    "10.1016": "sciencedirect_com",
    "10.1371": "plos_org",
    "10.1073": "pnas_org",
    "10.7554": "elifesciences_org",
    "10.1101": "biorxiv_org",
    "10.3389": "frontiersin_org",
    "10.1136": "bmj_com",
    "10.1056": "nejm_org",
    "10.1007": "springer_com",
    "10.1017": "cambridge_org",
    "10.1098": "royalsociety_org",
    "10.1534": "genetics_org",
    "10.7717": "peerj_com",
    "10.3354": "int_res_com",
    "10.1111": "wiley_com",
    "10.1242": "biologists_com",
    "10.1083": "rupress_org",
    "10.1084": "rupress_org",
    "10.1085": "rupress_org",
}


PDF_DOMAINS = [
    "nature.com",
    "pnas.org",
    "science.org",
    "sciencedirect.com",
    "cell.com",
    "oup.com",
    "springer.com",
    "wiley.com",
    "silverchair",
    "biorxiv.org",
    "elife",
    "frontiersin.org",
    "mdpi.com",
    "plos.org",
    "bmj.com",
    "nejm.org",
    "thelancet.com",
]


PUBLISHER_ASSET_RULES: dict[str, dict[str, object]] = {
    "wiley": {
        "hosts": ["onlinelibrary.wiley.com"],
        "fig1": "cms/asset/.../<article>-fig-0001-m.jpg; high-res usually drops the -m suffix.",
        "pdf": "https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true",
        "notes": "Plain HTTP often sees Cloudflare; use browser context cookies/request.",
    },
    "oup_silverchair": {
        "hosts": ["academic.oup.com", "oup.silverchair-cdn.com"],
        "fig1": "/view-large/figure/... plus signed CDN URL; thumbnail m_*.jpeg can often be upgraded by removing m_.",
        "pdf": "Publisher page PDF links vary by journal.",
        "notes": "Signed CDN query strings expire; record path pattern, not stale full URLs.",
    },
    "cell_elsevier": {
        "hosts": ["cell.com", "sciencedirect.com"],
        "fig1": "Cell: /cms/<doi>/asset/<uuid>/main.assets/gr1.jpg; ScienceDirect: https://ars.els-cdn.com/content/image/1-s2.0-<compactPII>-gr1.jpg.",
        "pdf": "Elsevier API first when configured; Cell Press PDF often works from cell.com fulltext pages; ScienceDirect PDF may require subscription.",
        "notes": "Use Elsevier API PII to resolve 10.1016 DOI directly to cell.com or sciencedirect.com, avoiding doi.org/linkinghub captcha where possible. Prefer gr1 over graphical abstract ga1.",
    },
    "nature_springer": {
        "hosts": ["nature.com", "springer.com", "media.springernature.com"],
        "fig1": "media.springernature.com image assets are usually direct-downloadable.",
        "pdf": "Springer/BMC direct PDF routes for 10.1007 and 10.1186.",
        "notes": "Use publisher page discovery before falling back to PDF figure extraction.",
    },
    "science_pnas": {
        "hosts": ["science.org", "pnas.org"],
        "fig1": "DOM exposes real image URLs; direct HTTP may return 403.",
        "pdf": "Use page PDF discovery with browser context when needed.",
        "notes": "Use in-page fetch/context request with credentials for protected images.",
    },
}


def doi_from_url_or_text(value: str) -> str:
    match = re.search(r"(10\.\d{4,9}/[^\s?#\"'<>]+)", value or "", re.IGNORECASE)
    return match.group(1).rstrip(".,;)") if match else ""


def publisher_slug_from_url(url: str) -> str:
    """Return a stable publisher slug for the persistent site profile."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().lstrip("www.")
        if "doi.org" in netloc:
            path = parsed.path.lstrip("/")
            prefix = ".".join(path.split("/")[0].split(".")[:2])
            return DOI_PREFIX_SLUG.get(prefix, "doi_org")
        slug = re.sub(r"[^a-z0-9]", "_", netloc.split(":")[0])[:40]
        return slug or "generic"
    except Exception:
        return "generic"


def auto_known_domains(url: str) -> list[str]:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for key, domains in PUBLISHER_DOMAIN_MAP.items():
        if key in netloc:
            return domains
    if netloc and "doi.org" not in netloc:
        return [netloc]
    return []


def expand_publisher_image_sources(src: str) -> list[str]:
    """Return publisher-specific candidate image URLs, best first."""
    src = (src or "").strip()
    if not src:
        return []
    sources: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in sources:
            sources.append(value)

    parsed = urlparse(src)
    path = parsed.path or ""
    netloc = parsed.netloc.lower()

    if "onlinelibrary.wiley.com" in netloc and "/cms/asset/" in path.lower():
        match = re.search(r"(.+-fig-\d{4})-[a-z](\.(?:jpe?g|png|gif|webp))$", path, re.I)
        if match:
            base_path = match.group(1)
            for ext in (".jpg", ".png", ".gif", ".webp"):
                add(parsed._replace(path=base_path + ext, query="", fragment="").geturl())

    if "silverchair-cdn.com" in netloc:
        upgraded_path = re.sub(r"/m_([^/]+\.(?:jpe?g|png|webp|gif))$", r"/\1", path, flags=re.I)
        if upgraded_path != path:
            add(parsed._replace(path=upgraded_path).geturl())

    add(src)
    return sources


def wiley_pdfdirect_url(article_url: str) -> str:
    if "onlinelibrary.wiley.com" not in (article_url or "").lower():
        return ""
    doi = doi_from_url_or_text(article_url)
    if not doi.startswith(("10.1111/", "10.1002/")):
        return ""
    return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{quote(doi, safe='/')}?download=true"


def springer_pdfdirect_url(article_url: str) -> str:
    doi = doi_from_url_or_text(article_url)
    if doi.startswith("10.1186/"):
        return f"https://link.springer.com/content/pdf/{quote(doi, safe='/')}_reference.pdf"
    if doi.startswith("10.1007/"):
        return f"https://link.springer.com/content/pdf/{quote(doi, safe='/')}.pdf"
    return ""
