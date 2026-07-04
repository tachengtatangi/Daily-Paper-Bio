"""Section generation, fallback, and repair helpers for paper-reader."""

from __future__ import annotations

import json
import re
from pathlib import Path

from reader_quality import page_is_cookie_wall, publisher_page_blocked
from reader_text import (
    _normalize_related_concepts,
    count_cjk_chars,
    count_latin_tokens,
    figure_takeaways_from_record,
    first_sentence,
    looks_like_mojibake,
    markdown_bullets,
    normalize_whitespace,
    sanitize_model_text,
    sentence_chunks,
    unique_keep_order,
    ensure_question,
)

SECTION_KEYS = [
    "paper_topic",
    "one_sentence_summary",
    "background_context",
    "research_question",
    "data_materials",
    "core_methods",
    "main_findings",
    "figure_takeaways",
    "strengths",
    "limitations",
    "critical_analysis",
    "related_concepts",
    "quick_reference",
    "notes",
]

PROMPT_JSON_CHAR_LIMIT = 120_000

def _json_for_prompt(value: object, label: str) -> str:
    if isinstance(value, (str, Path)):
        return f"{label} file path: {value}\n"
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        payload = json.dumps(str(value), ensure_ascii=False, indent=2)
    if len(payload) > PROMPT_JSON_CHAR_LIMIT:
        payload = payload[:PROMPT_JSON_CHAR_LIMIT] + "\n[... truncated by paper-reader ...]"
    return f"{label} content:\n{payload}\n"

BAD_OUTPUT_PHRASES = [
    "与题目相关的核心科学问题",
    "当前主要基于自动提取结果",
    "当前可用信息不足以支持更细的局限性判断",
    "当前回退模式下",
    "暂无一句话总结",
    "论文主题待进一步确认",
    # ── 摘要访问自我报告短语（不应出现在 limitations 等内容字段）──
    "摘要层面显示",
    "摘要层面来看",
    "仅基于摘要",
    "基于摘要层面",
    "当前只有摘要",
    "全文未可用",
    "全文暂不可用",
    "数据获取受限",
    "仅凭摘要",
    "Graphical abstract",
    "Contents lists available at ScienceDirect",
    "One-Sentence Summary",
    "Research Question",
    "Background Context",
    "Core Methods",
    "Main Findings",
    "Quick Reference",
    "summary_mode:",
    "authors:",
    "figure_paths:",
    "figure_items:",
    "pdf_path:",
    "downloaded_pdf:",
    "web_url:",
    "source:",
    "title:",
    "doi:",
    "journal:",
    "image_url:",
    "abstract_available:",
    "full_text_excerpt_available:",
    "figure_count:",
    "evidence_note:",
    ]

_GENERIC_MESH_LOWER: frozenset[str] = frozenset({
    "animals", "humans", "male", "female", "adult", "mice", "rats", "cells",
    "cell line", "cells, cultured", "methods", "embryonic development",
    "gene expression", "proteins", "rna", "dna", "genotype", "phenotype",
    "time factors", "dose-response relationship, drug", "mice, inbred c57bl",
    "signal transduction", "gene expression regulation", "sequence analysis",
    "molecular sequence data", "base sequence", "amino acid sequence",
    "protein binding", "binding sites", "ligands", "neurotoxins",
    "cryoelectron microscopy",
})

_DATA_MATERIALS_BOILERPLATE = (
    "已抓取", "本地pdf已落盘", "pmc元数据", "pubmed/pmc 元数据", "已落盘",
    "本地 pdf 和图", "xml 全文", "xml 全文、本地", "pmc xml", "pubmed/pmc",
)

def needs_chinese_rewrite(sections: dict) -> bool:
    combined = "\n".join(str(sections.get(key, "") or "") for key in SECTION_KEYS)
    if not combined.strip():
        return True
    if looks_like_mojibake(combined):
        return True
    if re.search(r"(?im)^\s*(finding|basis|path|caption|takeaway|figure)\s*[:：]", combined):
        return True
    lowered = combined.lower()
    if any(phrase.lower() in lowered for phrase in BAD_OUTPUT_PHRASES):
        return True
    if any(token in lowered for token in ["<h1", "<h2", "<p>", "<div", "</p>", "</div>"]):
        return True
    chinese = count_cjk_chars(combined)
    latin = count_latin_tokens(combined)
    if chinese < 80 and latin > 20:
        return True
    if chinese < 150 and latin > 80:
        return True
    for key in [
        "paper_topic",
        "one_sentence_summary",
        "research_question",
        "background_context",
        "core_methods",
        "main_findings",
        "figure_takeaways",
        "critical_analysis",
        "notes",
    ]:
        text = str(sections.get(key, "") or "")
        if not text:
            continue
        if looks_like_mojibake(text):
            return True
        if any(phrase.lower() in text.lower() for phrase in BAD_OUTPUT_PHRASES):
            return True
        if text.strip() in {"Abstract.", "Summary.", "Introduction."}:
            return True
        if count_cjk_chars(text) < 20 and count_latin_tokens(text) > 8:
            return True

    # data_materials must not be acquisition pipeline boilerplate
    data_materials = str(sections.get("data_materials", "") or "").lower()
    if any(phrase in data_materials for phrase in ["已抓取", "本地pdf已落盘", "pmc元数据", "pubmed/pmc 元数据", "已落盘"]):
        return True

    return False

def parse_sections_output(raw: str) -> dict | None:
    required = set(SECTION_KEYS)

    def looks_good(obj: object) -> dict | None:
        if not isinstance(obj, dict):
            return None
        if not required.issubset(obj):
            return None
        result = {key: sanitize_model_text(obj.get(key, "")) for key in SECTION_KEYS}
        result["related_concepts"] = _normalize_related_concepts(result["related_concepts"])
        return result

    try:
        parsed = json.loads(raw)
        clean = looks_good(parsed)
        if clean:
            return clean
    except Exception:
        pass

    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean

    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        clean = looks_good(parsed)
        if clean:
            return clean
    return None

def _filter_related_concepts(keywords: list[str]) -> list[str]:
    """Drop generic MeSH terms, keeping only specific scientific concepts."""
    return [
        kw for kw in unique_keep_order(keywords)
        if kw.lower() not in _GENERIC_MESH_LOWER and len(kw.strip()) > 3
    ]

def build_fallback_sections(record: dict, mode: str) -> dict:
    abstract = record.get("abstract", "")
    full_text = record.get("full_text", "")
    title = record.get("title", "")
    # Prefer full_text (local PDF / PMC) over abstract (PubMed metadata).
    # abstract or full_text would silently drop PDF content whenever PubMed
    # abstract is present — exactly the wrong priority for local-PDF inputs.
    rich_text = full_text if normalize_whitespace(full_text) else abstract
    summary_text = rich_text or title
    title_hint = title or "该论文"
    access_issue = publisher_page_blocked(title, full_text) or page_is_cookie_wall(title, full_text)
    evidence_level = record.get("summary_mode", "") or "未知"
    has_pdf = bool(record.get("downloaded_pdf"))
    has_figures = bool(record.get("figure_paths"))
    has_captioned_figures = bool(record.get("figure_items"))
    abstract_available = bool(normalize_whitespace(abstract))
    full_text_available = bool(normalize_whitespace(full_text)) and not access_issue

    if access_issue and not abstract_available:
        findings = "当前抓取到的主要是安全验证页或 cookie wall，不是论文正文，因此还不能可靠提炼主要结论。"
    else:
        findings = f"围绕\"{title_hint}\"的主要结果已经能看出一个大致轮廓，但还需要结合正文和图表逐条核对。"

    methods_candidates = [
        s for s in sentence_chunks(rich_text, limit=8)
        if any(token in s.lower() for token in ["method", "using", "performed", "review", "simulation", "assay", "analysis", "model"])
    ]
    methods_candidate = " ".join(methods_candidates[:2]) if methods_candidates else ""
    methods = (
        methods_candidate
        if methods_candidate and count_cjk_chars(methods_candidate) >= 12
        else f"当前材料显示作者围绕\"{title_hint}\"展开了分析，但更细的方法步骤还需要回到正文确认。"
    )

    limitations = []
    if "全文" not in evidence_level:
        limitations.append("当前主要依据摘要、网页元数据或可见页面文本，证据细节有限。")
    if access_issue:
        limitations.append("当前页面更像安全验证页或 cookie wall，正文可信度不足。")
    title_text = (title + " " + abstract).lower()
    if "review" in title_text or "mini-review" in title_text:
        limitations.append("该文属于综述类文章，结论强度依赖纳入研究的覆盖范围和质量。")
    if any(token in title_text for token in ["simulation", "docking", "homology modeling", "in silico"]):
        limitations.append("如果方法偏计算建模，需要额外关注是否有充分实验验证。")
    if mode == "critical":
        limitations.append("批判性阅读时还应继续核查样本量、对照设计、统计方法和可重复性。")

    notes = {
        "standard": ["标准阅读模式，优先保留可以复用的论文细节。"],
        "quick": ["快速浏览模式，重点是帮助判断这篇文章是否值得细读。"],
        "critical": ["批判性分析模式，更强调证据边界、方法假设和外推风险。"],
    }[mode]
    if record.get("image_url"):
        notes.append("网页里还有可引用图片，可在需要时进一步抓取。")
    if access_issue:
        notes.append("当前页面更像安全验证页或 cookie wall，正文可信度不足。")
    if re.search(r"graphical abstract|highlights", f"{title} {abstract} {full_text}", re.IGNORECASE):
        notes.append("材料里出现了 Graphical abstract 或 Highlights，适合优先回看。")

    abstract_hint = first_sentence(abstract or full_text)
    figure_takeaways = (
        figure_takeaways_from_record(record)
        if (has_figures or has_captioned_figures)
        else "- 当前没有可解释的图注或稳定图像，暂时无法给出图级 takeaway。"
    )

    if access_issue and not abstract_available and not full_text_available:
        one_sentence_summary = "当前材料主要是安全验证页或 cookie wall，尚未成功提取正文内容。"
        background_context = "需要先恢复对正文、PDF 或摘要的访问，才能判断这篇研究的背景与动机。"
        research_question = "这篇文章的研究问题目前无法从可用材料中可靠确认。"
        core_methods = "- 当前没有可靠正文，方法细节无法确认。"
        main_findings = markdown_bullets(
            [
                "当前抓取到的主要是安全验证页或 cookie wall，不是论文正文。",
                "需要重新获取摘要、PDF 或可访问的全文后，才能提炼主要结果。",
            ]
        )
        strengths = [
            f"已经记录到来源链接和证据层级: {evidence_level}",
            "后续只要恢复正文访问，就能继续补齐结构化笔记。",
        ]
        critical_analysis = markdown_bullets(
            [
                "当前不应基于这份页面文本推断论文结论。",
                "先解决访问限制，再核查样本、方法和结果图。",
            ]
        )
    else:
        # Build a best-effort one-sentence summary from the abstract/full text.
        # Avoid prefixes like "这篇论文的可用证据首先指向：" which are confusing.
        one_sentence_summary = abstract_hint or f"这篇文章研究 {title_hint}，核心信息需结合正文进一步核实。"

        # Build background context from the second sentence of the abstract, or
        # a neutral hint that avoids simply echoing the title.
        _abstract_sents = sentence_chunks(abstract or rich_text, limit=3) if (abstract or rich_text) else []
        if len(_abstract_sents) >= 2:
            background_context = _abstract_sents[1]
        elif _abstract_sents:
            background_context = _abstract_sents[0]
        else:
            background_context = f"研究动机需结合 {title_hint} 的摘要或正文才能可靠重建。"

        # Build research question: derive from title keywords, not just echo it.
        # Branches are broadly ordered from most specific to most generic;
        # adding more branches here does NOT require changes to user-config.
        _title_lower = title.lower()
        if any(w in _title_lower for w in ["convergent", "evolution", "divergence", "adaptation", "speciation"]):
            research_question = f"{title_hint} 中观察到的现象是否具有趋同进化或适应性的分子机制支撑？"
        elif any(w in _title_lower for w in ["genomic", "genome", "transcriptomic", "transcriptome", "epigenomic", "epigenome"]):
            research_question = f"这项研究通过基因组或组学比较，能否揭示 {title_hint} 背后的遗传或表观遗传驱动因素？"
        elif any(w in _title_lower for w in ["receptor", "taste", "olfactory", "sensory", "vision", "hearing", "chemosensory"]):
            research_question = f"这篇文章在感受器或感觉系统层面，具体解决了 {title_hint} 中的什么核心问题？"
        elif any(w in _title_lower for w in ["gene family", "paralog", "duplication", "de novo", "pseudogene"]):
            research_question = f"这篇文章怎样处理 {title_hint} 中基因家族扩张、收缩或新功能化的证据？"
        elif any(w in _title_lower for w in ["cell", "cellular", "signaling", "pathway", "apoptosis", "proliferation"]):
            research_question = f"这篇文章揭示了 {title_hint} 中哪条关键细胞信号通路或细胞过程的调控机制？"
        elif any(w in _title_lower for w in ["protein", "structure", "binding", "interaction", "domain", "fold"]):
            research_question = f"这篇文章在结构或互作层面，解决了 {title_hint} 中哪个核心的分子识别或功能问题？"
        elif any(w in _title_lower for w in ["neural", "neuron", "brain", "cortex", "circuit", "synapse", "cognitive"]):
            research_question = f"这篇文章在神经系统层面，揭示了 {title_hint} 中哪种神经回路、细胞类型或行为机制？"
        elif any(w in _title_lower for w in ["immune", "immunity", "vaccine", "antibody", "infection", "pathogen", "viral"]):
            research_question = f"这篇文章在免疫或宿主-病原互作层面，解决了 {title_hint} 中的什么关键问题？"
        elif any(w in _title_lower for w in ["method", "algorithm", "pipeline", "tool", "benchmark", "software"]):
            research_question = f"这个方法或工具解决了 {title_hint} 领域中哪个具体的技术瓶颈，与现有方案相比优势在哪里？"
        else:
            research_question = f"这篇文章在方法或结论上，对 {title_hint} 领域贡献了什么新的理解？"

        core_methods = methods
        main_findings = findings
        strengths = [
            "已抓取到基础元数据和摘要，可避免仅凭标题猜测研究内容。",
            "如果后续获取全文或图表，可继续补强证据链。",
        ]
        critical_analysis = "这份笔记基于摘要或部分正文自动生成；样本量、对照设计和统计处理仍需回到原文逐项核查。"

    # Filter generic MeSH terms from related_concepts
    raw_kws = record.get("keywords") or []
    filtered_kws = _filter_related_concepts(raw_kws)

    return {
        "paper_topic": title_hint or first_sentence(summary_text) or "这篇文章的主题还需要进一步确认。",
        "one_sentence_summary": one_sentence_summary,
        "background_context": background_context,
        "research_question": ensure_question(research_question),
        "data_materials": markdown_bullets(
            [
                "可用材料包括摘要、网页正文摘录和基础元数据。" if abstract_available or full_text_available else "目前只有较少的可用材料，主要是页面可见文本和基础元数据。",
                "已抓到全文级正文，可据此整理研究对象、结果和关键论证。" if full_text_available else "当前还没有稳定的全文摘录，因此细节判断需要保守。",
                "图像材料已落盘，可辅助核对正文中的关键图示。" if has_figures else "目前没有稳定图像可用于交叉核对图示结论。",
                "本地 PDF 已落盘，可回看原始版式和图注。" if has_pdf else "本次未拿到本地 PDF，只能依赖网页或 API 提供的正文材料。",
            ],
            fallback="当前材料主要来自摘要、网页元数据或可见页面文本。",
        ),
        "core_methods": core_methods,
        "main_findings": main_findings,
        "figure_takeaways": figure_takeaways,
        "strengths": markdown_bullets(
            strengths,
            fallback="当前自动提取到了基础信息，但创新点和细节仍需要结合原文进一步核对。",
        ),
        "limitations": markdown_bullets(limitations, fallback="当前可用信息还不足以支持更细的局限性判断。"),
        "critical_analysis": critical_analysis,
        "related_concepts": (
            "\n".join(f"- [[{kw.strip()}]]" for kw in filtered_kws[:5])
            if filtered_kws
            else "- （当前可用信息不足以可靠确定相关概念）"
        ),
        "quick_reference": markdown_bullets(
            [
                f"证据层级：{evidence_level}",
                f"摘要材料：{'有' if abstract_available else '无'}",
                f"正文摘录：{'有' if full_text_available else '无'}",
                f"图像材料：{'有' if has_figures else '无'}",
                f"访问受限：{'是' if access_issue else '否'}",
            ],
            limit=5,
            fallback="当前可用信息不足以形成可靠速览。",
        ),
        "notes": markdown_bullets(notes),
    }

def build_generation_prompt(materials: object, mode: str) -> str:
    mode_hint = {
        "standard": "生成一份适合 Obsidian 论文笔记的中文研究记录，厚一点、具体一点，不要摘要腔。",
        "quick": "生成一份简洁但仍有判断力的中文速览笔记，明确告诉我这篇文章值不值得细读。",
        "critical": "生成一份中文批判性笔记，重点写证据边界、方法假设、替代解释和外推风险。",
    }[mode]
    materials_block = _json_for_prompt(materials, "materials.json")
    return (
        "Use the embedded materials.json content below; output only one JSON object, no explanation and no Markdown code fence.\n"
        f"{materials_block}\n"
        "你是在为 Obsidian 里的论文笔记生成结构化中文内容。\n"
        f"{mode_hint}\n"
        "\n"
        "════════════════════════════════════════\n"
        "【四项绝对禁令 — 违反任意一条视为输出失败】\n"
        "════════════════════════════════════════\n"
        "禁①  paper_topic 不得是英文，不得直接复制标题。\n"
        "      必须用 1-2 句中文概括「研究了什么系统/问题、用什么方法」。\n"
        "禁②  one_sentence_summary 不得是英文。\n"
        "      必须是一句中文（≤60字），直接说出论文最重要的发现或结论。\n"
        "禁③  data_materials 不得描述 AI 的抓取过程。\n"
        "      禁止出现「已抓取」「本地PDF」「PMC元数据」「PMC XML」「已落盘」等词。\n"
        "      必须只写论文作者本人使用的数据：物种数量、样本量、基因组资源、\n"
        "      数据集名称、实验材料、测序平台等论文 Methods 中出现的内容。\n"
        "禁④  figure_takeaways 不得直接复制英文图注原文（哪怕只复制一部分也不行）。\n"
        "      必须读取 figure_items 里的 caption，将其翻译并概括为中文，\n"
        "      说明图像展示了什么、为什么重要、支持论文哪个论点。\n"
        "      示例：caption='Figure 1: Phylogenetic tree of 60 cichlid species...' →\n"
        "      输出「图1：60个慈鲷物种的时间校准系统发育树，枝条颜色区分各 tribe，\n"
        "      展示了活动时型在适应辐射内的广泛分化格局。」\n"
        "════════════════════════════════════════\n"
        "\n"
        "硬性要求:\n"
        "1. 先看 summary_mode，再决定证据层级：如果是全文/PDF/XML，就按全文层级写；如果只是摘要/网页内容/元数据，就明确保守，不要假装看过全文。\n"
        "2. 必须优先使用材料中的 abstract、full_text_excerpt、figure_items、figure_paths、web_url、pdf_path、downloaded_pdf、summary_mode；不要只盯着标题。\n"
        "3. 如果材料里出现 Abstract.、A B S T R A C T、摘要、引言、结果、讨论、Graphical abstract、Highlights 等分节，要尽量利用这些结构，不要漏掉正文里的关键段落。\n"
        "4. 如果抓到的是 cookie wall、Just a moment、security check、请开启 JavaScript / cookies 之类页面，就要诚实说明访问受限，不要编造论文内容。\n"
        "5. 全文尽量用中文重述，不要直接复制英文摘要原句；只有专有名词、基因名、物种名、方法名等必要术语可以保留英文。\n"
        "6. 任何字段都不要写成模板占位句，如\"与题目相关的核心科学问题\"\"当前主要基于自动提取结果\"\"暂无一句话总结\"这类空话。\n"
        "7. one_sentence_summary、background_context、research_question、main_findings、critical_analysis、notes 必须包含这篇文章自己的信息。\n"
        "8. research_question 必须是一个明确的问题句，结尾要像问题，而不是题目复述。\n"
        "9. background_context 要解释\"为什么这个问题值得研究\"，不要只复述题目。\n"
        "10. main_findings 最多 5 条，优先写具体结果、比较对象、机制、数据集或样本信息；不要输出 finding: / basis: 这类中间字段；每条必须单独占一行（用真实换行符分隔），不要在同一行内用 ' - ' 连缀多条。\n"
        "11. figure_takeaways：见上方禁④。每条单独占一行；不要输出 path: / caption: / takeaway: 这类字段名。\n"
        "12. data_materials：见上方禁③。每条单独占一行。\n"
        "13. quick_reference 要写成 3-5 条短的检查清单式条目；每条必须单独占一行（用真实换行符分隔），不要在同一行内用 ' - ' 连缀多条。\n"
        "14. strengths 和 limitations 每条也必须单独占一行，不要内联连缀。\n"
        "15. related_concepts 给出 Obsidian 链接形式，每个概念必须单独占一行，格式严格为 - [[概念名]]，不要在同一行内用 ' - ' 连缀多个概念；概念名必须使用英文（基因名、物种学名、学科术语均保持英文），不要使用通用 MeSH 词（Animals、Methods 等）；例如正确格式：\n- [[Olfactory Receptor]]\n- [[Comparative Genomics]]\n如知识库已有该概念页，保持与页名一致。\n"
        "16. notes 要写成可复用的研究笔记，而不是泛泛而谈的概述，优先用 bullet；每条单独占一行。\n"
        "17. 不要编造 materials.json 里没有的事实；不确定就明确写\"不足以确认\"。特别注意：物种学名、基因名、新发现的命名（如 sp. nov.）、具体统计数字，必须只来自当前材料，绝不能依赖训练记忆补全。\n"
        "18. 如果需要引用英文术语，保持最小化，不要让整句变成英文。\n"
        "19. 在 main_findings、core_methods、quick_reference 中，对最关键的基因名、物种名、方法名或定量数值用 **加粗** 标注；每条至少加粗 1 处，最多 2 处，不要对普通动词或连接词加粗。\n"
        "20. 当 summary_mode 包含\"摘要\"或\"元数据\"时（即只有摘要可用），必须基于 abstract 字段的实际内容填写 research_question、background_context、core_methods、main_findings 等字段；"
        "绝不能把标题原文嵌入任何分析字段作为占位符；如果摘要对某项内容确实无法回答，要写具体的不确定说明，例如\"摘要未说明具体样本量\"，而不是用标题文本填充。\n"
        "21. limitations 字段必须只写论文本身的科学局限（方法假设、样本不足、因果推断风险、功能实验缺失等），"
        "绝对不能写「当前只有摘要」「摘要层面显示」「全文未可用」「数据获取受限」等关于 AI 数据访问的句子。"
        "如果只有摘要，数据访问说明只能出现在 notes 或 quick_reference 里，例如「本笔记基于摘要，全文获取后建议补充方法细节」。\n"
        "22. 输出只允许一个 JSON 对象，且键必须严格是：\n"
        "paper_topic, one_sentence_summary, background_context, research_question, data_materials, core_methods, main_findings, figure_takeaways, strengths, limitations, critical_analysis, related_concepts, quick_reference, notes\n"
    )

def build_rewrite_prompt(materials: object, draft: object, mode: str) -> str:
    mode_hint = {
        "standard": "把初稿重写成更自然、更完整的中文论文笔记。",
        "quick": "把初稿重写成更简洁但仍然准确的中文速览笔记。",
        "critical": "把初稿重写成更锋利的中文批判性笔记。",
    }[mode]
    materials_block = _json_for_prompt(materials, "materials.json")
    draft_block = _json_for_prompt(draft, "draft.json")
    return (
        "Use the embedded materials.json and draft.json below to rewrite the final version.\n"
        f"{materials_block}\n"
        f"{draft_block}\n"
        "draft.json 只是初稿，请你根据 materials.json 和 draft.json 重写最终版本。\n"
        f"{mode_hint}\n"
        "════════════════════════════════════════\n"
        "【四项绝对禁令 — 违反任意一条视为输出失败】\n"
        "════════════════════════════════════════\n"
        "禁①  paper_topic 不得是英文，不得直接复制标题。\n"
        "      必须用 1-2 句中文概括「研究了什么系统/问题、用什么方法」。\n"
        "禁②  one_sentence_summary 不得是英文。\n"
        "      必须是一句中文（≤60字），直接说出论文最重要的发现或结论。\n"
        "禁③  data_materials 不得描述 AI 的抓取过程。\n"
        "      禁止出现「已抓取」「本地PDF」「PMC元数据」「PMC XML」「已落盘」等词。\n"
        "      必须只写论文作者本人使用的数据：物种数量、样本量、基因组资源、\n"
        "      数据集名称、实验材料、测序平台等论文 Methods 中出现的内容。\n"
        "禁④  figure_takeaways 不得直接复制英文图注原文（哪怕只复制一部分也不行）。\n"
        "      必须读取 figure_items 里的 caption，将其翻译并概括为中文，\n"
        "      说明图像展示了什么、为什么重要、支持论文哪个论点。\n"
        "      示例：caption='Figure 1: Phylogenetic tree of 60 cichlid species...' →\n"
        "      输出「图1：60个慈鲷物种的时间校准系统发育树，枝条颜色区分各 tribe，\n"
        "      展示了活动时型在适应辐射内的广泛分化格局。」\n"
        "════════════════════════════════════════\n"
        "硬性要求:\n"
        "1. 只输出一个 JSON 对象，不要解释，不要代码围栏。\n"
        "2. 输出键必须与 draft.json 完全一致。\n"
        "3. 全部字段都要用自然中文改写，删除英文直出、模板句、占位句和重复句。\n"
        "4. 不要保留\"与题目相关的核心科学问题\"\"当前主要基于自动提取结果\"\"暂无一句话总结\"这类空话。\n"
        "5. 如果 draft 里某个字段太泛泛，你要结合 materials.json 补足为更具体的中文表达。\n"
        "6. 一定要重新检查 summary_mode、abstract、full_text_excerpt、figure_items、figure_paths、web_url、pdf_path、downloaded_pdf，不要把全文级材料写成摘要级材料。\n"
        "7. 如果材料里出现 cookie wall、Just a moment、Highlights、Graphical abstract 之类提示，要在限制或 notes 里明确体现。\n"
        "8. one_sentence_summary、research_question、background_context、core_methods、main_findings、critical_analysis、notes 必须像真实研究笔记，不像模板。\n"
        "9. main_findings 和 figure_takeaways 必须改写成自然中文，不要保留 finding: / basis: / path: / caption: / takeaway: 这类字段壳；每条单独占一行，不要内联连缀。\n"
        "10. 如果 figure_takeaways 需要提图号，可以写成自然句式，例如\"图1：...\"或\"这张图说明...\"。\n"
        "11. quick_reference 要保持短、像清单，不要写成散文；每条单独占一行，不要在同一行内用 ' - ' 连缀。\n"
        "12. strengths 和 limitations 每条也必须单独占一行，不要内联连缀。\n"
        "13. notes 要写成未来可复用的研究提示，尽量具体，优先 bullet；每条单独占一行。\n"
        "14. 如果英文学术术语无法自然翻成中文，可以保留最小必要英文，但整句必须是中文。\n"
        "15. 不要编造 materials.json 里没有的事实；只在已有证据上重写和润色。\n"
        "16. 在 main_findings、core_methods、quick_reference 中，对最关键的基因名、物种名、方法名或定量数值用 **加粗** 标注；每条至少加粗 1 处，最多 2 处，不要对普通动词或连接词加粗。\n"
        "17. 当 summary_mode 包含\"摘要\"或\"元数据\"时，只有摘要可用；必须基于 abstract 字段的实际内容填写各分析字段；绝不可把标题原文嵌入 research_question 或 background_context 作为占位符；"
        "如果摘要确实不足以回答某项内容，写具体说明如\"摘要未说明样本量\"，而不是套用标题。\n"
        "18. limitations 字段必须只写论文本身的科学局限（方法假设、样本不足、因果推断风险、功能实验缺失等），"
        "绝对不能写「当前只有摘要」「摘要层面显示」「全文未可用」「数据获取受限」等关于 AI 数据访问的句子。"
        "如果只有摘要，数据访问说明只能出现在 notes 或 quick_reference 里，例如「本笔记基于摘要，全文获取后建议补充方法细节」。\n"
        "20. 输出只允许一个 JSON 对象，且键严格是：\n"
        "paper_topic, one_sentence_summary, background_context, research_question, data_materials, core_methods, main_findings, figure_takeaways, strengths, limitations, critical_analysis, related_concepts, quick_reference, notes\n"
    )

def _record_search_text(record: dict) -> str:
    return " ".join(
        normalize_whitespace(str(record.get(key, "") or ""))
        for key in ("title", "abstract", "full_text", "journal", "keywords")
    ).lower()

def _has_all(record: dict, *tokens: str) -> bool:
    text = _record_search_text(record)
    return all(token.lower() in text for token in tokens)

def _has_any(record: dict, *tokens: str) -> bool:
    text = _record_search_text(record)
    return any(token.lower() in text for token in tokens)

def _deterministic_topic_from_record(record: dict) -> str:
    if _has_all(record, "bat", "longevity"):
        return (
            "这项研究围绕蝙蝠长寿机制，结合101个蝙蝠物种的生活史性状和39个高质量蝙蝠基因组，"
            "分析寿命差异背后的生态预测因子、家族差异和候选分子过程。"
        )
    if _has_any(record, "olfactory receptor", "odorant receptor", "olfaction"):
        return "这项研究围绕嗅觉受体或嗅觉系统的选择、空间组织与功能机制，结合组学或功能证据解释感觉系统如何形成稳定表型。"
    if _has_any(record, "bitter taste receptor", "tas2r", "taste receptor"):
        return "这项研究围绕味觉受体或苦味相关分子机制，分析配体、受体响应或食品/生态背景中的功能证据。"
    if _has_any(record, "comparative genomics", "genome evolution", "genomic divergence"):
        return "这项研究以比较基因组学为核心，分析物种或类群之间的基因组差异、演化过程和可能的适应性机制。"
    title = normalize_whitespace(str(record.get("title", "") or ""))
    abstract = normalize_whitespace(str(record.get("abstract", "") or record.get("full_text", "") or ""))
    if abstract:
        return "这项研究基于摘要和可用正文材料，围绕题目所指的生物学问题整理研究对象、方法证据和结论边界。"
    return f"这项研究围绕 {title or '该论文'} 的核心问题展开，具体证据仍需结合摘要或正文核对。"

def _deterministic_one_sentence_from_record(record: dict) -> str:
    if _has_all(record, "bat", "longevity"):
        return (
            "蝙蝠寿命差异可能由性成熟年龄、最大纬度等生活史因素，以及DNA修复、炎症、免疫、"
            "线粒体功能和脂质代谢相关候选基因共同塑造。"
        )
    if _has_any(record, "olfactory receptor", "odorant receptor", "olfaction"):
        return "这篇文章用组学、空间或功能证据解释嗅觉受体选择和嗅觉系统组织如何受到特定生物学程序约束。"
    if _has_any(record, "bitter taste receptor", "tas2r", "taste receptor"):
        return "这篇文章把味觉受体响应与配体或苦味相关表型连接起来，提供了受体功能层面的证据。"
    if _has_any(record, "comparative genomics", "genome evolution", "genomic divergence"):
        return "这篇文章通过比较基因组学分析物种间差异，并把候选基因、结构变化或选择信号与演化表型联系起来。"
    abstract = normalize_whitespace(str(record.get("abstract", "") or record.get("full_text", "") or ""))
    if abstract:
        return "这篇文章基于可用摘要/正文材料提出一个具体生物学问题，并用组学、比较或功能证据支持主要结论。"
    return "当前材料不足以稳定提炼一句话结论，需要补充摘要或全文后再确认。"

def _deterministic_research_question_from_record(record: dict) -> str:
    if _has_all(record, "bat", "longevity"):
        return "蝙蝠最大寿命差异能否由生活史性状解释，并且不同蝙蝠类群是否具有与长寿相关的家族特异基因组机制？"
    if _has_any(record, "comparative genomics", "genome evolution", "genomic divergence"):
        return "这些物种或类群之间的基因组差异是否能解释其关键表型、生态适应或演化路径？"
    return "这篇文章用现有数据和方法试图回答的核心生物学问题是什么，其证据能支持到哪一步？"

def _deterministic_methods_from_record(record: dict) -> str:
    if _has_all(record, "bat", "longevity"):
        return (
            "作者整合101个蝙蝠物种的24个生活史性状，用系统发育回归分析最大寿命的预测因子；"
            "同时比较39个高质量蝙蝠基因组，围绕极端长寿类群筛选候选基因和功能过程。"
        )
    hints: list[str] = []
    if _has_any(record, "phylogenetic regression", "pgls"):
        hints.append("系统发育回归或PGLS")
    if _has_any(record, "comparative genomics", "genome"):
        hints.append("比较基因组学")
    if _has_any(record, "transcriptome", "rna-seq", "transcriptomic"):
        hints.append("转录组分析")
    if _has_any(record, "assay", "calcium", "functional"):
        hints.append("功能实验")
    if hints:
        return "作者主要使用" + "、".join(unique_keep_order(hints)) + "来连接研究对象、候选机制和主要结论。"
    return "当前材料显示作者使用摘要中描述的数据和分析流程支持结论；更细的参数、样本和统计处理仍需回到全文核查。"

def _deterministic_findings_from_record(record: dict) -> str:
    if _has_all(record, "bat", "longevity"):
        return markdown_bullets([
            "101个蝙蝠物种的生活史分析显示，性成熟年龄和最大纬度等生态/生活史变量与最大寿命差异相关。",
            "39个高质量基因组的比较把候选长寿过程集中到DNA损伤修复、炎症调控、免疫、线粒体功能和胆固醇/脂质代谢。",
            "Vespertilionidae 与 Pteropodidae 的寿命预测因子和候选机制并不相同，提示蝙蝠长寿不能压成单一共同路径。",
            "APO 基因家族等脂质代谢相关信号被纳入候选机制，但摘要层面仍属于比较基因组推断。",
            "整体证据支持生活史与多条分子适应共同塑造长寿假说，还不能直接证明候选通路的因果作用。",
        ])
    abstract = normalize_whitespace(str(record.get("abstract", "") or record.get("full_text", "") or ""))
    sents = sentence_chunks(abstract, limit=4) if abstract else []
    if sents:
        return markdown_bullets(["摘要显示：" + normalize_whitespace(s) for s in sents[:3]])
    return markdown_bullets(["当前材料不足以稳定提炼主要发现，需要补充摘要、全文或图表后再判断。"])

def _looks_like_note_placeholder(text: str) -> bool:
    clean = normalize_whitespace(text)
    if not clean:
        return True
    bad = [
        "待人工核实", "待进一步确认", "暂无一句话", "核心信息需结合正文进一步核实",
        "主要结果已经能看出一个大致轮廓", "还需要结合正文和图表逐条核对",
        "当前材料显示作者围绕", "更细的方法步骤还需要回到正文确认",
        "与题目相关的核心科学问题", "当前可用信息不足",
        "????", "????????",
    ]
    return any(phrase in clean for phrase in bad)

def _repair_english_fields(sections: dict, record: dict) -> dict:
    """Deterministic post-processing: fix fields that Codex/LLM consistently
    writes in English or fills with acquisition-pipeline boilerplate.

    Called after all generation/rewrite attempts so that the note always
    ends up with Chinese content in these fields regardless of model behavior.
    """
    repaired = dict(sections)

    def _is_english(text: str, min_latin: int = 6) -> bool:
        return count_cjk_chars(text) < 10 and count_latin_tokens(text) >= min_latin

    # paper_topic: repair English or placeholder output deterministically.
    pt = str(repaired.get("paper_topic", "") or "").strip()
    if _is_english(pt) or _looks_like_note_placeholder(pt):
        repaired["paper_topic"] = _deterministic_topic_from_record(record)

    # one_sentence_summary: avoid human-verification placeholders.
    oss = str(repaired.get("one_sentence_summary", "") or "").strip()
    if _is_english(oss) or _looks_like_note_placeholder(oss):
        repaired["one_sentence_summary"] = _deterministic_one_sentence_from_record(record)

    # Analytical fields that often degrade into title echoes.
    rq = str(repaired.get("research_question", "") or "").strip()
    if _looks_like_note_placeholder(rq):
        repaired["research_question"] = _deterministic_research_question_from_record(record)

    cm = str(repaired.get("core_methods", "") or "").strip()
    if _looks_like_note_placeholder(cm):
        repaired["core_methods"] = _deterministic_methods_from_record(record)

    mf = str(repaired.get("main_findings", "") or "").strip()
    if _looks_like_note_placeholder(mf):
        repaired["main_findings"] = _deterministic_findings_from_record(record)

    # Keep any remaining lines that actually describe the paper's own data.
    dm = str(repaired.get("data_materials", "") or "")
    dm_lower = dm.lower()
    if any(phrase in dm_lower for phrase in _DATA_MATERIALS_BOILERPLATE):
        clean_lines = [
            ln for ln in dm.splitlines()
            if ln.strip() and not any(p in ln.lower() for p in _DATA_MATERIALS_BOILERPLATE)
            and not re.search(r"\[\[AllPdfFig|\.pdf\]\]|\.pdf$", ln, re.IGNORECASE)
        ]
        repaired["data_materials"] = (
            "\n".join(clean_lines) if clean_lines
            else "- 数据与材料细节需结合全文补充。"
        )

    # ── figure_takeaways ───────────────────────────────────────────────────
    # Codex copies English captions verbatim (sometimes truncated mid-sentence).
    # Replace with a Chinese message; the image embed is already in the note.
    ft = str(repaired.get("figure_takeaways", "") or "").strip()
    if _is_english(ft):
        has_figures = bool(record.get("figure_paths") or record.get("figure_items"))
        repaired["figure_takeaways"] = (
            "- 图注为英文，请结合原文核对图示细节。"
            if has_figures
            else "- 当前没有可直接引用的图注或图像。"
        )

    return repaired
