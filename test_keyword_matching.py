#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parent
DAILY = ROOT / "daily-papers"
SHARED = ROOT / "_shared"
for path in (SHARED, DAILY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

stub_config = {
    "keywords": [
        "de novo gene",
        "olfactory receptor",
        "convergent acquisition",
        "genomic convergence",
    ],
    "negative_keywords": [],
    "rejected_journals": [],
    "domain_boost_keywords": ["comparative transcriptomics"],
    "keyword_variants": {
        "convergent acquisition": ["convergently acquired"],
        "genomic convergence": ["host genomic convergence"],
        "comparative transcriptomics": ["comparative transcriptome"],
    },
    "min_score": 1,
    "top_n": 30,
    "search_retmax": 0,
    "search_retmax_total_cap": 0,
    "min_quartile": 4,
    "biorxiv_enabled": False,
    "pubmed_enabled": False,
    "biorxiv_retmax": 0,
    "biorxiv_retmax_total_cap": 0,
    "biorxiv_timeout": 30,
    "biorxiv_categories": [],
    "efetch_workers": 1,
}

user_config_stub = types.ModuleType("user_config")
user_config_stub.daily_papers_config = lambda: dict(stub_config)
user_config_stub.daily_papers_dir = lambda: Path(tempfile.gettempdir())
user_config_stub.ncbi_api_key = lambda: ""
user_config_stub.temp_file_path = lambda name: Path(tempfile.gettempdir()) / name
sys.modules["user_config"] = user_config_stub

cas_stub = types.ModuleType("cas_quartiles")
cas_stub.lookup_journal = lambda journal: {}
sys.modules["cas_quartiles"] = cas_stub

profile_stub = types.ModuleType("library_profile")
profile_stub.load_or_build_library_profile = lambda config, refresh=False: {
    "domain_boost_keywords": [],
    "preferred_journals": [],
}
sys.modules["library_profile"] = profile_stub

import fetch_and_score as fs


class KeywordMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fs.apply_library_profile(refresh=False)

    def assert_matches(self, keyword: str, text: str) -> None:
        tokens = fs.text_to_norm_tokens(text)
        self.assertTrue(fs.contains_keyword_variant(tokens, keyword), f"{keyword!r} did not match {text!r}")

    def test_plural_and_hyphen_variants(self) -> None:
        self.assert_matches("de novo gene", "Several de-novo genes evolved in this lineage.")
        self.assert_matches("olfactory receptor", "The olfactory receptors expanded rapidly.")

    def test_configured_semantic_variants(self) -> None:
        self.assert_matches("convergent acquisition", "Convergently acquired enzymes explain the phenotype.")
        self.assert_matches("genomic convergence", "Host genomic convergence underlies subterranean adaptation.")
        self.assert_matches("comparative transcriptomics", "A comparative transcriptome analysis was performed.")

    def test_pubmed_supplement_uses_configured_variants(self) -> None:
        variants = fs.pubmed_keyword_variants("convergent acquisition")
        self.assertIn("convergently acquired", [item.lower() for item in variants])

    def test_pubmed_supplement_queries_are_chunked(self) -> None:
        original = fs.KEYWORDS
        try:
            fs.KEYWORDS = [f"synthetic keyword {idx}" for idx in range(80)]
            queries = fs.build_pubmed_supplemental_queries(max_chars=500)
            self.assertGreater(len(queries), 1)
            self.assertTrue(all(len(query) <= 520 for query in queries))
        finally:
            fs.KEYWORDS = original


if __name__ == "__main__":
    unittest.main()
