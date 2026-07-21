import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from reader_generation import SECTION_KEYS, needs_chinese_rewrite


class GenerationQualityTests(unittest.TestCase):
    def test_generic_fallback_is_rejected(self):
        sections = {key: "这是一段足够长的中文论文分析内容，用于满足基础长度要求。" for key in SECTION_KEYS}
        sections["paper_topic"] = "这项研究基于摘要和可用正文材料，围绕题目所指的问题展开。"
        sections["one_sentence_summary"] = "这篇文章基于可用摘要/正文材料提出一个具体生物学问题。"
        sections["research_question"] = "这篇文章用现有数据和方法试图回答的核心生物学问题是什么？"
        sections["data_materials"] = "- 具体样本和数据见正文。"
        self.assertTrue(needs_chinese_rewrite(sections))

    def test_specific_sections_pass(self):
        sections = {key: "作者比较了39个蝙蝠基因组，并结合101个物种的生活史数据分析寿命差异。" for key in SECTION_KEYS}
        sections["research_question"] = "生活史性状与基因组变化能否共同解释蝙蝠寿命差异？"
        sections["data_materials"] = "- 101个物种的生活史数据\n- 39个高质量基因组"
        self.assertFalse(needs_chinese_rewrite(sections))


if __name__ == "__main__":
    unittest.main()
