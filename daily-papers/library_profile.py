#!/usr/bin/env python3
"""Build and load a lightweight interest profile from a local PDF library."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import nltk
from nltk.stem import PorterStemmer as _PorterStemmer
from pypdf import PdfReader

_PORTER = _PorterStemmer()

from user_config import set_daily_papers_profile_fields

LIGATURE_MAP = str.maketrans({
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬀ": "ff",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
})

# ── STOPWORDS ─────────────────────────────────────────────────────────────────
# Contains ONLY:
#   (a) Pure function words (prepositions, articles, conjunctions, pronouns,
#       auxiliary verbs) — grammatically obligatory but semantically empty.
#   (b) Vague quantifiers / qualifiers whose presence in ANY phrase adds no
#       specificity (e.g. "novel species" is no better than "species").
#
# Domain biology terms (gene, species, protein, cell, data, system, …) are
# intentionally NOT here so that phrases like "gene flow", "species tree",
# "protein interaction" are not silently blocked.
STOPWORDS = {
    # Articles, prepositions, conjunctions, adverbs
    "the", "and", "for", "with", "from", "into", "across", "using", "use", "via", "through",
    "of", "in", "on", "to", "by", "at", "or", "an", "as", "well", "than",
    "toward", "towards", "during", "among", "between", "under", "over", "about",
    # Auxiliary verbs / copula
    "have", "has", "had", "been", "being", "were", "was", "are", "is", "be",
    # Pronouns and determiners
    "our", "their", "these", "which", "this", "that", "those", "its",
    # Generic connectives / hedges
    "also", "only", "both", "but", "however", "here", "we", "overall",
    "including", "based", "whether", "while", "whereas", "such", "when",
    "even", "yet", "thus", "hence", "therefore", "thereby", "therein",
    # Pronouns not already listed
    "they", "them", "we", "he", "she", "it", "his", "her", "him",
    # Comparative / degree adverbs (no keyword value alone)
    "more", "less", "most", "least", "highly", "high", "well", "just",
    "very", "quite", "rather", "further", "largely", "mainly", "primarily",
    # Prepositions missed above
    "within", "without", "beyond", "below", "above", "along", "around",
    "before", "after", "since", "until", "upon", "per", "like", "than",
    "some", "any", "all", "each", "every", "both", "either", "neither",
    "no", "not", "nor",
    # Vague quantifiers — never make a phrase more informative
    "other", "different", "various", "several", "multiple", "many", "few",
    # Vague qualifiers — equally true of every paper, so information-free
    "novel", "new", "recent", "current", "potential", "possible",
    "important", "major", "significant",
    # Generic past-participle / verb fillers
    "used", "identified", "observed", "shown", "found", "reported",
    "revealed", "suggested", "indicate", "indicates", "indicated",
    "include", "includes", "involve", "involves", "involved",
    "provide", "provides", "provided",
    "show", "shows", "showed", "demonstrate", "demonstrates", "demonstrated",
    "identify", "characterized", "contrast", "contrasts",
    "likely", "unlikely", "similar", "same", "given",
    # Cardinal numbers spelled out — never useful as keywords
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    # Vague length / size descriptors
    "long", "short", "large", "small", "high", "low",
    # Vague adjectives / determiners with no discriminating power alone
    "distinct", "certain", "respective", "additional", "particular",
    # Geographic terms too broad to be keywords on their own
    "world", "global", "local", "widespread",
    # Discourse verbs / adjectives that are never standalone keywords
    "underlying", "represent", "represents",
}

# ── GENERIC_SINGLE_TERMS ──────────────────────────────────────────────────────
# Filtered ONLY when they appear as a STANDALONE 1-gram keyword.
# As part of a 2–3 gram phrase they are fine ("genomic analysis", "molecular
# mechanism", "functional annotation").
# Domain terms never belong here.
GENERIC_SINGLE_TERMS = {
    # ── Universal academic discourse words ────────────────────────────────────
    # These words are meaningless as standalone keywords in EVERY research field.
    # DO NOT add domain-specific terms here (e.g. "expression", "protein",
    # "gene" are fine standalone keywords for a cell biologist or geneticist).
    # The frequency-based filter in apply_library_profile() handles field-specific
    # generality without hardcoding.
    #
    # Academic process / method nouns
    "study", "studies", "analysis", "analyses", "research",
    "result", "results", "approach", "approaches",
    "method", "methods", "technique", "techniques",
    "finding", "findings", "investigation",
    # Generic mechanisms / roles (fine in compounds; useless alone)
    "mechanism", "mechanisms", "function", "functions",
    "process", "processes", "effect", "effects",
    "role", "roles", "impact", "context",
    # Generic adjectival fillers
    "functional", "related", "associated", "known",
    # Generic quantitative / categorical nouns
    "data", "group", "groups", "type", "types", "level", "levels",
    "number", "numbers", "factor", "factors", "model", "models",
    "evidence", "pattern", "patterns", "network", "networks",
    # Generic structural descriptors (fine in compounds)
    "sequence", "sequences", "region", "regions", "domain", "domains",
    "site", "sites", "locus", "loci",
    # Generic taxonomic / organismal nouns (fine in compounds)
    "family", "families",
    # Generic discourse nouns
    "change", "changes", "understanding", "system", "systems",
    "form", "forms", "feature", "features", "basis",
}

# Standalone 1-gram biology terms that are still too broad to be useful as
# profile boosts. They may remain useful inside longer phrases.
PROFILE_SINGLE_TERM_BLACKLIST = {
    "species", "gene", "genes", "genome", "genomes", "genomic", "genetic",
    "evolution", "evolutionary", "selection", "molecular", "diversity",
    "lineage", "lineages", "structural",
}

# Exact phrases that are consistently methodological or narrative noise in this
# library and should never become boost terms.
PROFILE_PHRASE_BLACKLIST = {
    "deep learning",
    "functional assays",
    "genome assemblies",
    "host plant",
    "closely related",
    "extant species",
    "million years ago",
    "million years",
    "diverse group",
    "evolutionary history",
    "genetic basis",
    "years ago",
    "protein structures",
    "evolutionary biology",
    "genome derived",
    "mammals capable",
    "gene expression",
    "transcription factor",
    "evolutionary characteristics",
    "genomic analysis reveals",
    "genomics sheds light",
    "conclusion large-scale comparative",
    "protein-coupled receptors gpcrs",
    "generates mammalian lineage",
}

# Generic academic noise filtered from explicit "Keywords:" section lines.
GENERIC_TERMS = {
    "preprint", "article", "supplementary",
}

NOISE_PATTERNS = (
    "http", "https", "doi", "www", "@", ".edu", ".ac.", ".com", "copyright",
)

LINE_NOISE_PATTERNS = (
    "correspondence", "author information", "authors", "highlights", "editor", "received",
    "accepted", "published", "copyright", "all rights reserved", "open access",
    "supplementary", "www.", "http", "https", "doi", "@", ".com", "university", "department",
    "laboratory", "institute", "college", "school", "faculty", "academy",
    # Journal metadata patterns that appear before the real title in many PDFs
    "cite as", "first release", "first published", "advance online",
    "check for updates", "science.org", "nature.com", "cell.com",
    "elife sciences", "biorxiv.org", "plos.org",
)

SKIP_FILE_PATTERNS = (
    "supplement", "supplementary", "esm", "appendix", "review", "table s", "fig s", "sm"
)

SECTION_STOP_MARKERS = (
    "keywords", "key words", "introduction", "background", "results", "discussion", "conclusion",
    "materials and methods", "methods", "highlights", "author contributions", "correspondence",
    "significance", "importance", "main", "body", "references",
)

JOURNAL_TITLE_HINTS = (
    "nature reviews genetics", "molecular ecology", "journal of", "pnas", "cell",
    "science", "nature", "genetics", "genomics", "biology", "ecology", "annual review",
    "contents lists available", "wileyonlinelibrary",
)

METHOD_HINTS = {
    "model", "models", "test", "tests", "analysis", "analyses", "assay", "pipeline",
    "method", "methods", "approach", "approaches",
}

WEAK_LEADING_TOKENS = {
    "research", "article", "original", "review", "annual", "available", "contents",
    "introduction",
}

NARRATIVE_TOKENS = {
    "current", "knowledge", "greater", "depth", "poorly", "understood", "such",
    "because", "among", "overall", "however", "although", "despite", "including",
    "example", "examples", "suggested", "fascinating", "complex", "pattern",
    "common", "central", "role", "most", "diverse",
}

# Intentionally empty — no universally "boundary" tokens exist across research domains.
# The STOPWORDS + GENERIC_SINGLE_TERMS mechanism handles all necessary filtering.
# Domain-specific organism/taxon names should never be hardcoded here.
BOUNDARY_TOKENS: frozenset[str] = frozenset()

def _clean_text(text: str) -> str:
    text = (text or "").translate(LIGATURE_MAP)
    text = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _cjk_ratio(text: str) -> float:
    raw = text or ""
    if not raw:
        return 0.0
    return len(re.findall(r"[\u4e00-\u9fff]", raw)) / max(1, len(raw))


def _clean_line(line: str) -> str:
    line = (line or "").replace("\x0c", " ").translate(LIGATURE_MAP)
    line = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1\2", line)
    return re.sub(r"\s+", " ", line).strip()


def _section_key(line: str) -> str:
    low = _clean_line(line).lower()
    return re.sub(r"^[\d\s|.:()/-]+", "", low)


def _normalized_lines(text: str) -> list[str]:
    return [_clean_line(line) for line in (text or "").splitlines() if _clean_line(line)]


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z-]{2,}", text or "")


def _normalize_token(token: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", (token or "").lower())
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    for suffix in ("ing", "ers", "er", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    return token


# PROFILE_DOMAIN_ANCHORS is built dynamically at runtime from the user's own
# configured keywords + domain_boost_keywords (see _profile_domain_anchors()).
# This ensures library_profile.py is domain-agnostic and works for any research
# field — evolutionary genomics, cell biology, neuroscience, etc.
_PROFILE_DOMAIN_ANCHORS_CACHE: frozenset[str] | None = None


def _build_profile_domain_anchors() -> frozenset[str]:
    """Derive domain anchor tokens from user-configured keywords and boost keywords.

    Falls back to an empty set if config cannot be loaded (anchor check is then
    skipped in _boost_eligible, allowing all non-narrative terms through).
    """
    try:
        import sys as _sys
        _shared = Path(__file__).resolve().parent.parent / "_shared"
        if str(_shared) not in _sys.path:
            _sys.path.insert(0, str(_shared))
        from user_config import daily_papers_config as _cfg_fn
        cfg = _cfg_fn()
        terms = list(cfg.get("keywords") or []) + list(cfg.get("domain_boost_keywords") or [])
    except Exception:
        terms = []
    anchors: set[str] = set()
    for term in terms:
        for word in re.findall(r"[A-Za-z][A-Za-z-]{2,}", term):
            tok = _normalize_token(word)
            if tok:
                anchors.add(tok)
    return frozenset(anchors)


def _profile_domain_anchors() -> frozenset[str]:
    global _PROFILE_DOMAIN_ANCHORS_CACHE
    if _PROFILE_DOMAIN_ANCHORS_CACHE is None:
        _PROFILE_DOMAIN_ANCHORS_CACHE = _build_profile_domain_anchors()
    return _PROFILE_DOMAIN_ANCHORS_CACHE


NARRATIVE_ROOTS = {_normalize_token(token) for token in NARRATIVE_TOKENS}
BOUNDARY_ROOTS = {_normalize_token(token) for token in BOUNDARY_TOKENS}
PRONOUN_ROOTS = {_normalize_token(x) for x in {"we", "our", "this", "these", "those"}}
CONNECTOR_ROOTS = {_normalize_token(x) for x in {"such", "because", "however", "although", "despite"}}


def _phrase_tokens(phrase: str) -> list[str]:
    return [_normalize_token(part) for part in re.findall(r"[A-Za-z][A-Za-z-]{2,}", phrase or "") if _normalize_token(part)]


def _looks_like_split_fragment(tokens: list[str]) -> bool:
    """Detect phrases that look like a single word split across a PDF line break.

    E.g. ["modifica", "tion"] → "modification" (bad_suffix exact match)
         ["specif",   "ication"] → "specification" (short right token + combined ends in suffix)
    Full words that happen to end in -tion/-ment are NOT fragments:
         ["bat",      "echolocation"] → right is 12 chars, not a suffix fragment.
    Threshold: only apply the combined-endswith check when the right token is
    shorter than 9 characters (all genuine suffix fragments are ≤8 chars).
    """
    bad_suffixes = (
        "tion", "tions", "ation", "ations", "fication", "ization",
        "ment", "ments", "ness", "ality", "ative", "fied", "ified", "fying",
    )
    for idx in range(len(tokens) - 1):
        left = tokens[idx]
        right = tokens[idx + 1]
        combined = f"{left}{right}"
        # right IS the suffix fragment itself
        if right in bad_suffixes:
            return True
        # right is short (≤8 chars) AND the merged word ends in a bad suffix —
        # likely a hyphenation break.  Full words (≥9 chars) are excluded so that
        # e.g. "bat echolocation" or "gene expression" don't trigger.
        if len(right) < 9 and any(combined.endswith(suffix) for suffix in bad_suffixes):
            return True
    return False


def _extract_with_pdftotext(path: Path) -> str:
    for candidate in ("pdftotext.exe", "pdftotext"):
        try:
            out_txt = Path(tempfile.mkdtemp(prefix="profile_pdf_")) / "out.txt"
            subprocess.run(
                [candidate, "-f", "1", "-l", "2", str(path), str(out_txt)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if out_txt.exists():
                return out_txt.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
    return ""


def _extract_pdf_text(path: Path) -> str:
    text = _extract_with_pdftotext(path)
    if _clean_text(text):
        return text
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception:
        return ""


def _looks_like_main_article(path: Path, text: str) -> bool:
    stem = path.stem.lower()
    if any(token in stem for token in SKIP_FILE_PATTERNS):
        return False
    compact = _clean_text(text).lower()
    if not compact:
        return False
    if compact.startswith("supplementary materials"):
        return False
    if compact.startswith("cite as:"):
        return False
    if "materials and methods figs. s" in compact:
        return False
    return True


def _title_from_pdf_text(text: str) -> str:
    def valid_title(candidate: str) -> bool:
        candidate = _clean_text(candidate)
        if not candidate or len(candidate) < 20 or len(candidate) > 180:
            return False
        if _cjk_ratio(candidate) > 0.15:
            return False
        if "," in candidate or candidate.endswith("."):
            return False
        first_word = candidate.split()[0].lower()
        if first_word in {"in", "we", "here", "the", "this", "these", "however", "by", "using", "with", "from", "based", "through", "to", "for"}:
            return False
        last_word = candidate.split()[-1].lower().strip(".,;:()[]{}")
        if last_word in {"that", "which", "and", "or", "for", "from", "with", "by", "to", "of", "in", "on", "as", "using"}:
            return False
        tokens = _tokenize_words(candidate)
        return 4 <= len(tokens) <= 16

    lines = _normalized_lines(text)
    title, _, _ = _find_title_block(lines)
    if valid_title(title):
        return title
    boundary = min(len(lines), 100)
    for idx in range(max(0, boundary - 45), boundary - 1):
        first = _clean_line(lines[idx])
        second = _clean_line(lines[idx + 1]) if idx + 1 < boundary else ""
        if not first or not second or "," in first or "," in second:
            continue
        if len(_tokenize_words(first)) not in range(3, 9) or len(_tokenize_words(second)) not in range(2, 8):
            continue
        if any(token in _section_key(first) for token in LINE_NOISE_PATTERNS):
            continue
        if any(token in _section_key(second) for token in LINE_NOISE_PATTERNS):
            continue
        if not (first[:1].isupper() and second[:1].islower()):
            continue
        combo = _clean_text(f"{first} {second}")
        if valid_title(combo):
            return combo
    for idx in range(30, max(30, boundary - 1)):
        first = _clean_line(lines[idx])
        second = _clean_line(lines[idx + 1]) if idx + 1 < boundary else ""
        if not first or not second:
            continue
        if "," in first or "," in second:
            continue
        if len(first) < 20 or len(first) > 110 or len(second) < 8 or len(second) > 90:
            continue
        if first.endswith(".") or second.endswith("."):
            continue
        if any(token in _section_key(first) for token in LINE_NOISE_PATTERNS):
            continue
        if any(token in _section_key(second) for token in LINE_NOISE_PATTERNS):
            continue
        first_word = first.split()[0].lower()
        if first_word in {"in", "we", "here", "the", "this", "these"}:
            continue
        if len(_tokenize_words(first)) > 8 or len(_tokenize_words(second)) > 8:
            continue
        if first[0].isupper() and second[0].islower():
            combo = _clean_text(f"{first} {second}")
            if valid_title(combo):
                return combo
    return title if valid_title(title) else ""


def _find_title_block(lines: list[str]) -> tuple[str, int, int]:
    boundary = min(len(lines), 100)
    for idx, line in enumerate(lines[:40]):
        low = _section_key(line)
        if low.startswith(("abstract", "introduction", "supplementary materials", "cite as:")):
            boundary = idx
            break

    def looks_like_journal_shell(line: str) -> bool:
        low = _section_key(line)
        # DOI / citation string (e.g. "Cite as: ... Science 10.1126/...  (2026)")
        if re.search(r"\b10\.\d{4,}/", line):
            return True
        # "First release: 8 January 2026 ..." or "First published ..."
        if re.match(r"(?:first release|first published|advance online|cite as)[:\s]", low):
            return True
        # Pure date line: "8 January 2026" / "January 2026" / "2026-01-08"
        if re.fullmatch(r"\d{1,2}\s+[A-Z][a-z]+\s+\d{4}(?:\s+\S+)?", line.strip()):
            return True
        # Journal metadata slug: looks like "science.org" / "nature.com" etc.
        if re.search(r"\b(?:science|nature|cell|elife)\.(?:org|com)\b", low):
            return True
        if re.search(r"\b\d{4}\b", line):
            return True
        if re.search(r"\|\s*\d+\s+of\s+\d+", low):
            return True
        return low in JOURNAL_TITLE_HINTS or any(low.startswith(hint) for hint in JOURNAL_TITLE_HINTS)

    def looks_like_title_line(line: str) -> bool:
        low = _section_key(line)
        if not (8 <= len(line) <= 180):
            return False
        if low.startswith(("abstract", "introduction", "review article", "check for updates")):
            return False
        if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
            return False
        if looks_like_journal_shell(line):
            return False
        return True

    def looks_like_non_title(line: str) -> bool:
        low = _section_key(line)
        if low.startswith(("abstract", "introduction")):
            return True
        if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
            return True
        if re.search(r"\bcorresponding author\b", low):
            return True
        if looks_like_author_line(line):
            return True
        if line.count(",") >= 3:
            return True
        if re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+)", line) and any(token in line for token in [",", "&", "|"]):
            return True
        return False

    def looks_like_author_line(line: str) -> bool:
        low = _section_key(line)
        if any(token in low for token in ["@", "correspondence"]):
            return True
        if "&" in line and re.search(r"\b[A-Z][a-z]+", line):
            return True
        if line.count(",") >= 2 and re.search(r"\b[A-Z][a-z]+", line):
            return True
        if re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+)", line) and re.search(r"\d", line):
            return True
        return False

    def clean_title_line(line: str) -> str:
        line = re.sub(r"^(?:RESEARCH ARTICLE|ORIGINAL ARTICLE|ARTICLE|Review article|Leading Edge|Review)\s+", "", line, flags=re.IGNORECASE).strip()
        line = re.sub(r"\bCheck for updates\b", "", line, flags=re.IGNORECASE).strip()
        line = re.sub(r"\s+", " ", line).strip()
        return line

    def title_starter_score(line: str) -> int:
        cleaned = clean_title_line(line)
        low = _section_key(cleaned)
        if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
            return -10
        if looks_like_journal_shell(cleaned):
            return -10
        if not (12 <= len(cleaned) <= 160):
            return -10
        if cleaned.endswith("."):
            return -5
        words = re.findall(r"[A-Za-z][A-Za-z-]*", cleaned)
        if not words:
            return -10
        score = 0
        if cleaned[0].isupper():
            score += 2
        if sum(1 for w in words if w[0].isupper()) >= 2:
            score += 2
        if re.search(r"[:\-]", cleaned):
            score += 1
        if low.startswith(("the ", "here ", "to ", "in ", "we ")):
            score -= 2
        if cleaned.endswith("."):
            score -= 2
        lower_words = [w.lower() for w in words]
        if any(w in {"john", "sudhir", "author", "authors"} for w in lower_words):
            score -= 4
        return score

    for idx in range(1, boundary):
        if not looks_like_author_line(lines[idx]):
            continue
        prev_idx = idx - 1
        if prev_idx < 0:
            continue
        prev = clean_title_line(lines[prev_idx])
        prev_score = title_starter_score(prev)
        candidate_parts: list[str] = []
        start_idx = prev_idx
        if prev_score >= 2:
            candidate_parts = [prev]
            if prev_idx - 1 >= 0:
                prev2 = clean_title_line(lines[prev_idx - 1])
                prev2_score = title_starter_score(prev2)
                if prev and prev[0].islower() and prev2_score >= 1:
                    candidate_parts = [prev2, prev]
                    start_idx = prev_idx - 1
                elif prev2_score >= 2:
                    candidate_parts = [prev2, prev]
                    start_idx = prev_idx - 1
        elif prev and prev[0].islower() and prev_idx - 1 >= 0:
            prev2 = clean_title_line(lines[prev_idx - 1])
            prev2_score = title_starter_score(prev2)
            if prev2_score >= 2:
                candidate_parts = [prev2, prev]
                start_idx = prev_idx - 1
        title = _clean_text(" ".join(candidate_parts))
        if len(title) >= 20:
            return title, start_idx, prev_idx

    for idx in range(max(0, boundary - 50), boundary - 1):
        first = clean_title_line(lines[idx])
        second = clean_title_line(lines[idx + 1])
        if title_starter_score(first) < 1:
            continue
        second_score = title_starter_score(second)
        second_ok = second_score >= 0 or (
            second and second[0].islower() and 10 <= len(second) <= 90 and not second.endswith(".")
        )
        if not second_ok or looks_like_non_title(second):
            continue
        combo = _clean_text(f"{first} {second}")
        if not (25 <= len(combo) <= 170):
            continue
        if combo.endswith("."):
            continue
        words = _tokenize_words(combo)
        if len(words) < 5:
            continue
        return combo, idx, idx + 1

    candidates: list[tuple[float, str, int, int]] = []
    for idx, line in enumerate(lines[:boundary]):
        if not looks_like_title_line(line):
            continue
        cleaned = clean_title_line(line)
        parts = [cleaned]
        end_idx = idx
        for nxt in lines[idx + 1 : boundary]:
            if looks_like_non_title(nxt):
                break
            if not (8 <= len(nxt) <= 140):
                break
            if looks_like_journal_shell(nxt):
                break
            parts.append(nxt)
            end_idx += 1
            if len(" ".join(parts)) >= 180:
                break
        title = _clean_text(" ".join(parts))
        score = 0.0
        if 15 <= len(title) <= 180:
            score += 2
        if len(parts) >= 2:
            score += 1
        after = lines[end_idx + 1 : min(boundary, end_idx + 5)]
        if any(looks_like_author_line(x) for x in after):
            score += 4
        if any(any(k in _section_key(x) for k in ["university", "department", "institute", "school"]) for x in after):
            score += 2
        if title.endswith("."):
            score -= 2
        if re.search(r"\b(we|here|to characterize|the order|the accurate|computational protein design methods)\b", title, re.IGNORECASE):
            score -= 4
        if re.search(r"\b\d{4}\b", title):
            score -= 2
        candidates.append((score, title, idx, end_idx))
    if not candidates:
        return "", -1, -1
    candidates.sort(key=lambda item: (-item[0], item[2]))
    best = candidates[0]
    return best[1], best[2], best[3]


def _abstract_from_pdf_text(text: str) -> str:
    lines = _normalized_lines(text)
    buffer: list[str] = []
    inside = False
    for line in lines:
        low = _section_key(line)
        if not inside:
            if re.fullmatch(r"abstract[:\s-]*", low) or low.startswith("abstract "):
                inside = True
                inline = re.sub(r"^abstract[:\s-]*", "", line, flags=re.IGNORECASE).strip()
                if inline:
                    buffer.append(inline)
            continue
        if any(low.startswith(marker) for marker in SECTION_STOP_MARKERS):
            break
        if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
            continue
        buffer.append(line)
        if len(_clean_text(" ".join(buffer))) >= 2600:
            break
    abstract = _clean_text(" ".join(buffer))
    if 80 <= len(abstract) <= 2600:
        return abstract
    return _fallback_abstract_from_lines(lines)


def _is_content_line(line: str) -> bool:
    low = _section_key(line)
    if not low:
        return False
    if any(low.startswith(marker) for marker in SECTION_STOP_MARKERS):
        return False
    if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
        return False
    if re.fullmatch(r"[A-Z][A-Za-z\s-]{0,25}", line):
        return False
    return len(line) >= 35


def _looks_like_author_or_affiliation(line: str) -> bool:
    low = _section_key(line)
    if not low:
        return False
    if any(token in low for token in ["@", "university", "department", "institute", "school", "hospital", "laboratory", "correspondence", "equal contribution", "these authors", "present address"]):
        return True
    if re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+){0,2}\b", line) and (
        line.count(",") >= 1 or "&" in line or re.search(r"\b\d+\b", line)
    ):
        return True
    return False


def _collect_content_blocks(lines: list[str], search_limit: int = 140) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_start = -1
    for idx, line in enumerate(lines[:search_limit]):
        low = _section_key(line)
        if any(low.startswith(marker) for marker in SECTION_STOP_MARKERS):
            if current:
                blocks.append((current_start, idx - 1, _clean_text(" ".join(current))))
                current = []
                current_start = -1
            continue
        if _is_content_line(line):
            if not current:
                current_start = idx
            current.append(line)
        else:
            if current:
                blocks.append((current_start, idx - 1, _clean_text(" ".join(current))))
                current = []
                current_start = -1
    if current:
        blocks.append((current_start, min(search_limit, len(lines)) - 1, _clean_text(" ".join(current))))
    return blocks


def _author_region_end(lines: list[str], title_end: int) -> int:
    idx = title_end + 1
    limit = min(len(lines), title_end + 18)
    while idx < limit:
        line = lines[idx]
        if _looks_like_author_or_affiliation(line) or (len(line) < 35 and not _is_content_line(line)):
            idx += 1
            continue
        break
    return idx - 1


def _fallback_abstract_from_lines(lines: list[str]) -> str:
    title, start_idx, end_idx = _find_title_block(lines)
    blocks = _collect_content_blocks(lines)
    scored: list[tuple[float, str]] = []
    author_end = _author_region_end(lines, end_idx) if end_idx >= 0 else -1
    for b_start, b_end, block in blocks:
        if len(block) < 180:
            continue
        score = len(block) / 120.0
        if end_idx >= 0 and b_start > author_end:
            gap = max(0, b_start - author_end)
            score += max(0, 6 - gap)
        if start_idx >= 0 and b_end < start_idx:
            distance = start_idx - b_end
            score += max(0, 5 - distance) if start_idx >= 18 else -1
        if re.search(r"\b(we|here|our work|this study|we generated|we used)\b", block, re.IGNORECASE):
            score += 1
        if re.search(r"\b(results|discussion|conclusion|references|materials and methods)\b", block, re.IGNORECASE):
            score -= 2
        if re.search(r"\b(zygosity|contig|scaffold|n50|assembly|assembled|genome size)\b", block, re.IGNORECASE):
            score -= 2
        if re.search(r"\b(bats are|here we|we show|we report|in this study|this review)\b", block, re.IGNORECASE):
            score += 1.5
        scored.append((score, block[:2600]))
    scored.sort(key=lambda item: -item[0])
    return scored[0][1] if scored else ""


def _keywords_from_pdf_text(text: str) -> list[str]:
    lines = _normalized_lines(text)
    raw_parts: list[str] = []
    capture = False
    for line in lines:
        low = _section_key(line)
        if not capture and re.match(r"^(?:keywords|key words)\b", low):
            capture = True
            stripped = re.sub(r"^(?:keywords|key words)[:\s-]*", "", line, flags=re.IGNORECASE).strip()
            if stripped:
                raw_parts.append(stripped)
            continue
        if not capture:
            continue
        if any(low.startswith(marker) for marker in SECTION_STOP_MARKERS if marker not in {"keywords", "key words"}):
            break
        if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
            break
        raw_parts.append(line)
        if len(" ".join(raw_parts)) >= 400:
            break
    raw = " ".join(raw_parts)[:400]
    if not raw:
        return []
    parts = [re.sub(r"[^A-Za-z0-9 \-]", "", part).strip().lower() for part in re.split(r"[;,/|\u2022]", raw)]
    cleaned: list[str] = []
    for part in parts:
        if not (3 <= len(part) <= 50):
            continue
        if part in STOPWORDS or part in GENERIC_TERMS:
            continue
        if any(token in part for token in NOISE_PATTERNS):
            continue
        tokens = _phrase_tokens(part)
        if len(tokens) > 4:
            continue
        if any(token in {"this", "that", "these", "those", "here", "there", "among", "finding"} for token in tokens):
            continue
        if any(token.isdigit() for token in tokens):
            continue
        if tokens and tokens[0] in {"as", "by", "with", "from"}:
            continue
        cleaned.append(_clean_text(part))
    return cleaned


ALLOWED_POS_PREFIXES = ("NN", "JJ")
ALLOWED_POS_EXACT = {"VBN", "VBG"}
DISALLOWED_POS_PREFIXES = ("RB", "IN", "DT", "PRP", "WP", "WRB", "CC", "TO", "MD", "VB")


def _phrase_pos_ok(phrase: str) -> bool:
    """Return True if *phrase* looks like a noun/adjective phrase.

    Strategy: count noun-like, adjective-like, and disallowed-POS tokens.
    Do NOT reject on the first disallowed tag — NLTK assigns VBP to many
    biological nouns when they are seen without sentence context:
        nltk.pos_tag(["species", "tree"])  →  [("species", "NNS"), ("tree", "VBP")]
        nltk.pos_tag(["gene",    "flow"])  →  [("gene",    "NN"),  ("flow", "VBP")]
        nltk.pos_tag(["phylogenetic", "tree"]) → [("phylogenetic", "JJ"), ("tree", "VBP")]
    Rejection rules (applied AFTER counting all tokens):
      • noun_like >= 1           → accept  (genuine noun present, mistag irrelevant)
      • adj_like >= 1 and        → accept  (JJ + at-most-equal bad tags, e.g.
        bad_count <= adj_like              "phylogenetic tree" JJ+VBP)
      • otherwise                → reject  (pure function-word / verb phrase)
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z-]*", phrase or "")
    if not tokens:
        return False
    try:
        tagged = nltk.pos_tag(tokens)
    except Exception:
        return True
    noun_like = 0
    adj_like  = 0
    bad_count = 0
    for _, tag in tagged:
        if tag.startswith("NN"):
            noun_like += 1
        elif tag.startswith("JJ"):
            adj_like += 1
        elif tag in ALLOWED_POS_EXACT:          # VBN, VBG — participial / gerund
            pass
        elif tag.startswith(DISALLOWED_POS_PREFIXES):  # RB, IN, DT, PRP, WP, WRB, CC, TO, MD, VB
            bad_count += 1
        # Unknown tags (CD, SYM, FW, LS, etc.): allow through — bio names often
        # contain digits or unusual tokens (TAS2R38, NF-κB, Mus musculus).
    if len(tokens) == 1:
        return True
    # A confirmed noun makes it a noun phrase regardless of other mistaggings
    if noun_like >= 1:
        return True
    # JJ + VBP (likely adjective + mistagged noun): allow when bad tags ≤ adjective count
    if adj_like >= 1 and bad_count <= adj_like:
        return True
    # Nothing noun-like at all → not a noun phrase
    return False


def _is_narrative_phrase(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if all(token in NARRATIVE_ROOTS for token in tokens):
        return True
    if tokens[0] in PRONOUN_ROOTS:
        return True
    if any(token in CONNECTOR_ROOTS for token in tokens):
        return True
    return False


def _is_boundary_phrase(tokens: list[str]) -> bool:
    if not tokens:
        return False
    return all(token in BOUNDARY_ROOTS for token in tokens)


def _phrase_ok(phrase: str) -> bool:
    phrase = _clean_text(phrase.lower())
    if not phrase or phrase in GENERIC_TERMS:
        return False
    if phrase in PROFILE_PHRASE_BLACKLIST:
        return False
    if any(token in phrase for token in NOISE_PATTERNS):
        return False
    parts = phrase.split()
    if not (1 <= len(parts) <= 3):
        return False
    if any(part in STOPWORDS for part in parts):
        return False
    # For single-word terms, also filter generic academic discourse words that
    # are uninformative alone but perfectly fine inside a longer phrase.
    if len(parts) == 1 and parts[0] in GENERIC_SINGLE_TERMS:
        return False
    if len(parts) == 1 and parts[0] in PROFILE_SINGLE_TERM_BLACKLIST:
        return False
    tokens = _phrase_tokens(phrase)
    if _looks_like_split_fragment(tokens):
        return False
    if _is_narrative_phrase(tokens):
        return False
    if tokens and tokens[0] in WEAK_LEADING_TOKENS:
        return False
    if any(token in {"this", "that", "these", "those", "here", "there"} for token in tokens):
        return False
    if any(re.fullmatch(r"\d{4}", token) for token in tokens):
        return False
    if sum(1 for part in parts if len(part) >= 4) == 0:
        return False
    return _phrase_pos_ok(phrase)


def _candidate_phrases_from_text(text: str, sizes: tuple[int, ...]) -> set[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]*", text or "") if len(w) >= 2]
    phrases: set[str] = set()
    for size in sizes:
        for idx in range(0, len(words) - size + 1):
            phrase = " ".join(words[idx : idx + size])
            if _phrase_ok(phrase):
                phrases.add(phrase)
    return phrases


def _document_terms(title: str, abstract: str, keywords: list[str]) -> tuple[set[str], set[str], set[str]]:
    keyword_terms = {kw for kw in keywords if _phrase_ok(kw)}
    title_terms    = _candidate_phrases_from_text(title,    (1, 2, 3))
    # Abstract 1-grams are much noisier than title/keyword signals for a boost
    # profile. Keep only 2-3 word phrases from the abstract.
    abstract_terms = _candidate_phrases_from_text(abstract, (2, 3))
    return keyword_terms, title_terms, abstract_terms


def _method_penalty(term: str) -> int:
    return 1 if any(token in METHOD_HINTS for token in _phrase_tokens(term)) else 0


def _stem_set(tokens: list[str]) -> frozenset[str]:
    """Porter-stem a token list for morphological deduplication.

    "structure" and "structural" both stem to "structur" → detected as duplicate.
    "evolution" and "evolutionary" both stem to "evolut" → detected as duplicate.
    """
    return frozenset(_PORTER.stem(t) for t in tokens if t)


def _is_too_similar(term: str, selected: list[str]) -> bool:
    """Return True if *term* is too similar to any already-selected term.

    Uses Porter-stemmed token sets so that morphological variants
    (structure/structural, speciate/speciation, evolve/evolution) are treated
    as the same concept and deduplicated.
    """
    term_tokens  = set(_phrase_tokens(term))
    term_stemmed = _stem_set(list(term_tokens))
    if not term_tokens:
        return True
    for existing in selected:
        ex_tokens  = set(_phrase_tokens(existing))
        ex_stemmed = _stem_set(list(ex_tokens))
        # Exact match (after stemming)
        if term_stemmed == ex_stemmed:
            return True
        # One is a strict subset of the other (after stemming) → duplicate concept
        if term_stemmed and ex_stemmed:
            if term_stemmed.issubset(ex_stemmed) or ex_stemmed.issubset(term_stemmed):
                return True
        # Significant raw-token overlap (≥ half the smaller set)
        raw_overlap = term_tokens & ex_tokens
        if raw_overlap and len(raw_overlap) >= max(1, min(len(term_tokens), len(ex_tokens)) - 1):
            return True
    return False


def _is_variant_of(term: str, others: list[str]) -> bool:
    term_tokens = set(_phrase_tokens(term))
    if not term_tokens:
        return True
    for other in others:
        other_tokens = set(_phrase_tokens(other))
        if not other_tokens:
            continue
        if term_tokens == other_tokens:
            return True
        if term_tokens.issubset(other_tokens) or other_tokens.issubset(term_tokens):
            return True
        if len(term_tokens & other_tokens) >= max(1, min(len(term_tokens), len(other_tokens))):
            return True
    return False


def _keyword_eligible(term: str) -> bool:
    tokens = _phrase_tokens(term)
    if not tokens:
        return False
    if _is_narrative_phrase(tokens):
        return False
    if _is_boundary_phrase(tokens):
        return False
    return True


def _boost_eligible(term: str) -> bool:
    tokens = _phrase_tokens(term)
    if not tokens:
        return False
    if _is_narrative_phrase(tokens):
        return False
    anchors = _profile_domain_anchors()
    # Only apply anchor gate when the user has configured keywords; if anchors
    # is empty (e.g. first run, empty config) allow all non-narrative terms.
    if anchors and not any(token in anchors for token in tokens):
        return False
    return True


def _select_diverse_terms(ordered_terms: list[str], limit: int) -> list[str]:
    """Select up to *limit* diverse terms from the ranked list.

    Rules (in order):
    1. If the new term's stem-set is a SUBSET of an already-chosen term's stem-set
       → skip (redundant, shorter version of something already selected).
    2. If the new term's stem-set is a SUPERSET of an already-chosen term's stem-set
       → REPLACE the shorter term with the longer, more specific one.
    3. Otherwise, apply _is_too_similar for broader overlap deduplication.
    4. After building the list, run a final pass to remove any stragglers where
       one term's stems are a strict subset of another's.
    """
    chosen: list[str] = []

    for term in ordered_terms:
        term_tokens  = set(_phrase_tokens(term))
        term_stemmed = _stem_set(list(term_tokens))
        if not term_stemmed:
            continue

        replaced     = False
        skip         = False
        for idx, existing in enumerate(list(chosen)):
            ex_tokens  = set(_phrase_tokens(existing))
            ex_stemmed = _stem_set(list(ex_tokens))

            # Exact (after stemming)
            if term_stemmed == ex_stemmed:
                # Keep the longer surface form (more descriptive)
                if len(term_tokens) > len(ex_tokens):
                    chosen[idx] = term
                    replaced = True
                else:
                    skip = True
                break

            # New term is MORE SPECIFIC (superset of existing stems) → upgrade
            if ex_stemmed and ex_stemmed.issubset(term_stemmed) and len(term_stemmed) > len(ex_stemmed):
                chosen[idx] = term
                replaced = True
                break

            # New term is LESS SPECIFIC (subset of existing stems) → skip it
            if term_stemmed and term_stemmed.issubset(ex_stemmed):
                skip = True
                break

        if skip or replaced:
            continue
        if _is_too_similar(term, chosen):
            continue
        chosen.append(term)
        if len(chosen) >= limit:
            break

    # ── Final dedup pass ───────────────────────────────────────────────────────
    # Any term whose stems are a strict subset of another's stems should be removed.
    stem_sets = [_stem_set(list(set(_phrase_tokens(t)))) for t in chosen]
    result: list[str] = []
    for i, term in enumerate(chosen):
        dominated = any(
            i != j and stem_sets[i] and stem_sets[i].issubset(stem_sets[j])
            for j in range(len(chosen))
        )
        if not dominated:
            result.append(term)

    return result[:limit]


def build_library_profile(pdf_folder: Path) -> dict:
    pdf_files = sorted(pdf_folder.rglob("*.pdf"))
    any_docs: Counter[str] = Counter()
    explicit_keyword_docs: Counter[str] = Counter()
    title_docs: Counter[str] = Counter()
    abstract_docs: Counter[str] = Counter()
    journal_counter: Counter[str] = Counter()
    titles: list[str] = []
    parsed_examples: list[dict] = []

    for path in pdf_files[:500]:
        text = _extract_pdf_text(path)
        if len(_clean_text(text)) < 120 or not _looks_like_main_article(path, text):
            continue
        title = _title_from_pdf_text(text) or ""
        abstract = _abstract_from_pdf_text(text)
        if not abstract:
            continue
        if _cjk_ratio(title) > 0.15 or _cjk_ratio(abstract) > 0.10:
            continue
        keywords = _keywords_from_pdf_text(text)
        if title:
            titles.append(title)

        keyword_terms, title_terms, abstract_terms = _document_terms(title, abstract, keywords)
        all_terms = keyword_terms | title_terms | abstract_terms
        for term in all_terms:
            any_docs[term] += 1
        for term in keyword_terms:
            explicit_keyword_docs[term] += 1
        for term in title_terms:
            title_docs[term] += 1
        for term in abstract_terms:
            abstract_docs[term] += 1

        journal = ""
        lines = _normalized_lines(text)[:30]
        for line in lines:
            low = line.lower()
            if len(line) < 8 or len(line) > 120:
                continue
            if any(pattern in low for pattern in LINE_NOISE_PATTERNS):
                continue
            if re.search(r"\b(journal|nature|science|genetics|genomics|biology|evolution|cell|pnas|molecular)\b", low):
                journal = line
                break
        if journal:
            journal_counter[journal] += 1

        if len(parsed_examples) < 12:
            parsed_examples.append({
                "file": str(path),
                "title": title,
                "keywords": keywords[:8],
                "abstract_preview": abstract[:240],
            })

    # ── Unified scoring with phrase-length bonus ──────────────────────────────
    # 1-gram: ×1.0  |  2-gram: ×1.5  |  3-gram: ×1.5
    # Bigrams/trigrams get a stronger boost so field-specific phrases can
    # compete with high-frequency single words in the same pool.
    def _length_bonus(term: str) -> float:
        n = len(_phrase_tokens(term))
        if n >= 2:
            return 1.5
        return 1.0

    # Single ranked list — terms with enough evidence to be domain signals.
    # Criterion: appeared in ≥2 docs, or in at least one title or abstract.
    # (docs≥1-only terms are too noisy to be reliable boost signals.)
    candidate_ranked: list[tuple[float, int, int, int, str]] = []
    for term, docs in any_docs.items():
        tt_docs = title_docs.get(term, 0)
        ab_docs = abstract_docs.get(term, 0)
        kw_docs = explicit_keyword_docs.get(term, 0)
        if not (docs >= 2 or tt_docs >= 1 or ab_docs >= 1):
            continue
        if len(_phrase_tokens(term)) == 1:
            if term in PROFILE_SINGLE_TERM_BLACKLIST:
                continue
            if tt_docs == 0 and kw_docs == 0:
                continue
        method_penalty = _method_penalty(term)
        base_score = docs * 2 + tt_docs * 3 + ab_docs - method_penalty
        weighted   = base_score * _length_bonus(term)
        candidate_ranked.append((weighted, docs, tt_docs, ab_docs, term))

    candidate_ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4]))

    # ── Bigram-first selection (no fixed ratio) ───────────────────────────────
    # Profile produces ONE output list: domain_boost_keywords (up to 30 terms).
    # This list is merged directly into DOMAIN_BOOST_KEYWORDS in fetch_and_score.
    # Bigrams/trigrams fill slots first; unigrams fill only what's left.
    MAX_BOOST = 30

    boost_bigrams:  list[str] = []
    boost_unigrams: list[str] = []
    for _, docs, tt_docs, ab_docs, term in candidate_ranked:
        if not _boost_eligible(term):
            continue
        if len(_phrase_tokens(term)) >= 2:
            boost_bigrams.append(term)
        else:
            boost_unigrams.append(term)

    bigram_top  = _select_diverse_terms(boost_bigrams,  MAX_BOOST)
    remaining   = max(0, MAX_BOOST - len(bigram_top))
    unigram_top = _select_diverse_terms(boost_unigrams, remaining)
    profile_boost_keywords = bigram_top + unigram_top

    return {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "pdf_count": len(pdf_files),
        "sampled_titles": titles[:30],
        "parsed_examples": parsed_examples,
        "domain_boost_keywords": profile_boost_keywords,
        "candidate_ranking": [
            {"term": term, "score": round(score, 2), "docs": docs, "title_docs": tt_docs, "abstract_docs": ab_docs,
             "n_gram": len(_phrase_tokens(term))}
            for score, docs, tt_docs, ab_docs, term in candidate_ranked[:80]
        ],
        "preferred_journals": [journal for journal, count in journal_counter.most_common(10) if count >= 2][:6],
    }


def load_or_build_library_profile(config: dict, refresh: bool = False) -> dict:
    enabled = bool(config.get("profile_enabled", False))
    folder_raw = (config.get("profile_pdf_folder") or "").strip()
    if not enabled:
        return {
            "keywords": [],
            "domain_boost_keywords": [],
            "preferred_journals": [],
        }
    folder = Path(folder_raw).expanduser() if folder_raw else None
    if folder is None or not folder.exists():
        return {
            "keywords": [],
            "domain_boost_keywords": [],
            "preferred_journals": [],
        }
    profile = build_library_profile(folder)
    set_daily_papers_profile_fields(profile.get("domain_boost_keywords", []))
    return profile
