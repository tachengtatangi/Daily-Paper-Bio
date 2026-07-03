"""Text, Markdown, and model-output cleanup helpers for paper-reader."""

from __future__ import annotations

import re

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

def safe_filename(text: str, maxlen: int = 110) -> str:
    text = re.sub(r'[\\/:*""<>|]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maxlen].strip(" .") or "untitled"

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
    # Publisher URLs that embed the DOI in their path after /doi/, e.g.:
    #   https://www.science.org/doi/10.1126/science.adk4858
    #   https://www.pnas.org/doi/10.1073/pnas.2024XYZ
    #   https://royalsocietypublishing.org/doi/10.1098/rsbl.2024.XXX
    m = re.search(r"/doi/(?:abs/|full/|pdf/)?(10\.\d{4,9}/[^\s?#\"'\]]+)", value, re.IGNORECASE)
    if m:
        return m.group(1).rstrip("/.,;)")
    return ""

def find_doi_in_text(text: str) -> str:
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text or "", re.IGNORECASE)
    return match.group(0).rstrip(").,;]") if match else ""

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
    # Strip leading bullet markers: "- ", "• ", "1. ", "1) ", "* " (single asterisk bullet).
    # IMPORTANT: do NOT strip "**" — that is Markdown bold syntax, not a bullet marker.
    stripped = re.sub(r"^\s*(?:[-•]\s+|\d+[.)]\s+|\*(?!\*)\s+)", "", stripped)
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
        rendered_items = []
        for idx, item in enumerate(takeaways[:3]):
            if count_cjk_chars(item) < 10 and count_latin_tokens(item) > 6:
                prefix = f"{figure_label}：" if (figure_label and idx == 0) else ""
                rendered_items.append(f"{prefix}图注为英文，请结合原文核对图示细节。")
            else:
                if idx == 0 and figure_label and not item.startswith(figure_label):
                    item = f"{figure_label}：{item}"
                rendered_items.append(item)
        return chr(10).join(f"- {item}" for item in rendered_items)

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

def count_cjk_chars(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")

def count_latin_tokens(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{3,}", text or ""))

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
