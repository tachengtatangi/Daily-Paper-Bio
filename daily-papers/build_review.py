#!/usr/bin/env python3
"""Build a lightweight daily recommendation draft.

This mirrors the original skill architecture:
- Python only handles sorting, sectioning, and Markdown scaffolding.
- The agent remains responsible for the final commentary pass.
- No mandatory LLM calls are performed here.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date
from pathlib import Path

import sys

SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from user_config import daily_papers_config, paper_notes_dir, temp_file_path

LABEL_MUST = "必读"
LABEL_WORTH = "值得看"
LABEL_SKIP = "可跳过"
LABEL_REVIEW = "推荐锐评"
LABEL_RANK = "排名列表"
LABEL_SPLIT = "分流表"
LABEL_LEVEL = "分级"
LABEL_SCORE = "得分"
LABEL_SOURCE = "来源"
LABEL_DATE = "日期"
LABEL_JOURNAL = "期刊/平台"
LABEL_KEYWORDS = "关键词"
LABEL_REASON = "推荐理由"
LABEL_ABSTRACT = "摘要短评"
LABEL_LINKS = "链接"
LABEL_NOTES = "笔记"
LABEL_APPENDIX = "低信号候选（附录）"
LABEL_APPENDIX_NOTE = "主题信号偏弱，仅作备查，不列入今日主要推荐。"
REPORT_SUFFIX = "论文推荐.md"
DRAFT_SUFFIX = "论文推荐.draft.md"
SEP = "、"

REVIEW_CONFIG = daily_papers_config()
MUST_SCORE_MIN = max(0, int(REVIEW_CONFIG.get("build_review_must_score_min", 4) or 4))
SHOW_APPENDIX = bool(REVIEW_CONFIG.get("build_review_show_appendix", False))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_title(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r'[\uFFFD\u200B-\u200D\u2060]+', "", text)
    return text.strip(" .;:,")


def unique_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = normalize_text(item)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def abstract_excerpt(abstract: str, n_sentences: int = 2, max_chars: int = 420) -> str:
    clean = normalize_text(abstract)
    if not clean:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    excerpt = " ".join(sentences[:n_sentences]).strip()
    return excerpt[:max_chars].strip()


def safe_filename(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:110].strip(" .") or "untitled"


def display_keywords(paper: dict) -> list[str]:
    return unique_keep_order(
        list(paper.get("matched_keywords", []))
        + list(paper.get("matched_boost_keywords", []))
    )


def is_primary(paper: dict) -> bool:
    return bool(paper.get("matched_keywords")) or bool(paper.get("matched_boost_keywords"))


def classify_sections(papers: list[dict]) -> dict[str, list[dict]]:
    total = len(papers)
    if total == 0:
        return {LABEL_MUST: [], LABEL_WORTH: [], LABEL_SKIP: []}

    must_n = max(0, math.ceil(total * 0.3))
    worth_n = max(0, math.ceil(total * 0.4))
    if must_n + worth_n > total:
        worth_n = max(0, total - must_n)

    top_pool = papers[:must_n]
    rest_pool = papers[must_n:]
    must_papers = [paper for paper in top_pool if paper.get("score", 0) >= MUST_SCORE_MIN]
    demoted = [paper for paper in top_pool if paper.get("score", 0) < MUST_SCORE_MIN]
    worth_pool = demoted + rest_pool
    worth_papers = worth_pool[:worth_n]
    skip_papers = worth_pool[worth_n:]
    return {
        LABEL_MUST: must_papers,
        LABEL_WORTH: worth_papers,
        LABEL_SKIP: skip_papers,
    }


def note_name_for_identifier(identifier: str) -> str | None:
    notes_root = paper_notes_dir()
    if not notes_root.exists() or not identifier:
        return None
    for path in notes_root.rglob("*.md"):
        if path.name.startswith(f"{identifier} - ") or identifier in path.stem:
            return path.stem
    return None


def build_source_links(paper: dict) -> str:
    parts: list[str] = []
    url = normalize_text(paper.get("url", ""))
    doi = normalize_text(paper.get("doi", ""))
    pmid = normalize_text(paper.get("id", ""))
    if url:
        parts.append(f"[Primary]({url})")
    if doi:
        parts.append(f"[DOI](https://doi.org/{doi})")
    if pmid.isdigit():
        parts.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)")
    return " | ".join(parts) if parts else "-"


def _field(label: str, value: str, bold: bool = False) -> str:
    head = f"**{label}**" if bold else label
    return f"- {head}: {value}"


def _build_split_table(sections: dict[str, list[dict]], note_cache: dict[int, str | None]) -> list[str]:
    rows: list[str] = []
    for label in (LABEL_MUST, LABEL_WORTH, LABEL_SKIP):
        papers = sections[label]
        if not papers:
            continue
        items: list[str] = []
        for paper in papers:
            note_name = note_cache.get(id(paper))
            kws = display_keywords(paper)
            hint = kws[0] if kws else ""
            if note_name:
                # 已生成笔记文件 → 用 wiki-link，Obsidian 可跳转
                entry = f"[[{note_name}]]"
            else:
                # 尚未生成笔记 → 纯文本，不显示下划线
                title = clean_title(paper.get("title", ""))
                short_title = safe_filename(title)[:60].strip()
                entry = short_title if short_title else (paper.get("id") or paper.get("pmid") or "unknown")
            items.append(f"{entry}（{hint}）" if hint else entry)
        rows.append(f"| {label} | {' · '.join(items)} |")
    return [
        f"## {LABEL_SPLIT}",
        "",
        "| 等级 | 论文 |",
        "|------|------|",
        *rows,
    ]


def _default_reason(paper: dict) -> str:
    bits: list[str] = []
    kws = display_keywords(paper)
    if kws:
        bits.append(f"命中主题词 {SEP.join(kws[:3])}")
    if paper.get("score", 0):
        bits.append(f"当前筛选得分 {paper.get('score', 0)}")
    journal = normalize_text(paper.get("journal", ""))
    if journal:
        bits.append(f"来源于 {journal}")
    if paper.get("has_real_world"):
        bits.append("含真实样本或实验信号")
    if not bits:
        return "与当前主题存在一定相关性，建议人工复核全文与方法细节。"
    return "；".join(bits) + "。"


def _default_abstract_comment(paper: dict) -> str:
    title = clean_title(paper.get("title", ""))
    kws = display_keywords(paper)
    journal = normalize_text(paper.get("journal", ""))
    source = normalize_text(paper.get("source", "unknown"))
    parts: list[str] = []
    if title:
        parts.append(f"这篇工作围绕“{title}”展开。")
    if kws:
        parts.append(f"从自动匹配结果看，它主要落在 {SEP.join(kws[:3])} 这条主题线上。")
    if journal:
        parts.append(f"当前来源为 {journal}。")
    else:
        parts.append(f"当前来源为 {source}。")
    parts.append("这部分仍是结构化草稿，需在阅读摘要后补成正式中文短评。")
    return "".join(parts)


def _header_summary(primary: list[dict], low_signal: list[dict]) -> str:
    if not primary:
        return "今天没有形成足够强的主列表，建议扩大时间窗口或调整关键词。"
    kw_freq: dict[str, int] = {}
    for paper in primary:
        for kw in display_keywords(paper):
            kw_freq[kw] = kw_freq.get(kw, 0) + 1
    top_kws = sorted(kw_freq, key=lambda item: (-kw_freq[item], item))[:4]
    parts = [f"本轮主列表共 {len(primary)} 篇。"]
    if top_kws:
        parts.append(f"高频主题集中在 {SEP.join(top_kws)}。")
    must_count = len(classify_sections(primary)[LABEL_MUST])
    parts.append(f"当前自动分桶得到 {must_count} 篇必读。")
    if len(primary) < 3:
        parts.append("今天主题信号偏弱，建议扩大时间窗口后再看。")
    if low_signal:
        parts.append(f"另有 {len(low_signal)} 篇低信号候选被降到附录或隐藏。")
    parts.append("以下内容仍是结构化草稿，需在阅读题目、摘要和上下文后补写最终锐评。")
    return "".join(parts)


def build_markdown(papers: list[dict], today: str) -> str:
    primary = [paper for paper in papers if is_primary(paper)]
    low_signal = [paper for paper in papers if not is_primary(paper)]
    sections = classify_sections(primary)

    # ── Pre-compute note names once (avoids one rglob scan per paper per call) ─
    def _note_for(paper: dict) -> str | None:
        pid = normalize_text(paper.get("id", "") or paper.get("doi", ""))
        return note_name_for_identifier(pid) if pid else None

    note_cache: dict[int, str | None] = {id(p): _note_for(p) for p in primary}

    # ── Pre-compute bucket labels (O(N) instead of O(N²) classify_sections calls) ─
    bucket_by_id: dict[int, str] = {}
    for label in (LABEL_MUST, LABEL_WORTH, LABEL_SKIP):
        for p in sections[label]:
            bucket_by_id[id(p)] = label

    lines: list[str] = [
        "---",
        f"date: {today}",
        "tags: [daily-papers, pubmed, biorxiv, auto-generated]",
        "---",
        "",
        f"# {LABEL_REVIEW}",
        "",
        _header_summary(primary, low_signal),
        "",
    ]

    lines.extend(_build_split_table(sections, note_cache))
    lines.extend(["", f"## {LABEL_RANK}", ""])

    for idx, paper in enumerate(primary, start=1):
        pid = normalize_text(paper.get("id", "") or paper.get("doi", "")) or f"p{idx}"
        title = clean_title(paper.get("title", ""))
        kw_text = ", ".join(display_keywords(paper)) or "-"
        reason = normalize_text(paper.get("recommendation_reason", "")) or _default_reason(paper)
        comment = (
            normalize_text(paper.get("abstract_comment", ""))
            or normalize_text(paper.get("method_summary", ""))
            or _default_abstract_comment(paper)
        )
        note = note_cache.get(id(paper))

        lines.extend([
            f"### {idx}. {title}",
            "",
            _field(LABEL_LEVEL, f"`{bucket_by_id.get(id(paper), LABEL_SKIP)}`", bold=True),
            _field(LABEL_SCORE, f"`{paper.get('score', 0)}`"),
            _field(LABEL_SOURCE, f"`{paper.get('source', 'unknown')}`"),
            _field(LABEL_DATE, f"`{paper.get('date', '')}`"),
            _field(LABEL_JOURNAL, f"`{paper.get('journal', '')}`", bold=True),
            f"- ID: `{pid}`",
            _field(LABEL_LINKS, build_source_links(paper)),
            _field(LABEL_KEYWORDS, f"`{kw_text}`"),
            _field(LABEL_REASON, reason, bold=True),
            _field(LABEL_ABSTRACT, comment, bold=True),
        ])
        if note:
            lines.append(_field(LABEL_NOTES, f"[[{note}]]", bold=True))
        lines.append("")

    if SHOW_APPENDIX and low_signal:
        lines.extend([f"## {LABEL_APPENDIX}", "", LABEL_APPENDIX_NOTE, ""])
        for idx, paper in enumerate(low_signal, start=1):
            title = clean_title(paper.get("title", ""))
            kw_text = ", ".join(display_keywords(paper)) or "-"
            lines.extend([
                f"### A{idx}. {title}",
                "",
                _field(LABEL_SCORE, f"`{paper.get('score', 0)}`"),
                _field(LABEL_SOURCE, f"`{paper.get('source', 'unknown')}`"),
                _field(LABEL_KEYWORDS, f"`{kw_text}`"),
                _field(LABEL_ABSTRACT, abstract_excerpt(paper.get('abstract', '')), bold=True),
                "",
            ])

    return "\n".join(lines).rstrip() + "\n"



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output")
    args = parser.parse_args()

    input_path = Path(args.input_json)
    papers = json.loads(input_path.read_text(encoding="utf-8-sig"))
    output_path = Path(args.output) if args.output else temp_file_path(f"{args.date}-{DRAFT_SUFFIX}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_markdown(papers, args.date), encoding="utf-8-sig")
    print(json.dumps({"draft_path": str(output_path), "paper_count": len(papers)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
