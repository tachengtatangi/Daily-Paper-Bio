#!/usr/bin/env python3
from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SHARED = ROOT / "_shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from date_window import parse_date, parse_range, parse_window


class DateWindowTests(unittest.TestCase):
    def test_parse_non_zero_padded_date(self) -> None:
        self.assertEqual(parse_date("2025-3-21"), date(2025, 3, 21))

    def test_parse_window_is_inclusive(self) -> None:
        window = parse_window("2025-03-24", 4)
        self.assertEqual(window.start, date(2025, 3, 21))
        self.assertEqual(window.end, date(2025, 3, 24))
        self.assertEqual(window.days, 4)

    def test_parse_range_is_inclusive(self) -> None:
        window = parse_range("2025-3-21", "2025-3-24")
        self.assertEqual(window.start_date, "2025-03-21")
        self.assertEqual(window.end_date, "2025-03-24")
        self.assertEqual(window.days, 4)

    def test_parse_window_rejects_invalid_days(self) -> None:
        for bad_days in (0, -1):
            with self.subTest(days=bad_days):
                with self.assertRaises(ValueError):
                    parse_window("2025-03-24", bad_days)

    def test_parse_range_rejects_reversed_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_range("2025-03-24", "2025-03-21")

    def test_pubmed_primary_issue_date_controls_window(self) -> None:
        daily = ROOT / "daily-papers"
        if str(daily) not in sys.path:
            sys.path.insert(0, str(daily))
        import types

        user_config_stub = types.ModuleType("user_config")
        user_config_stub.daily_papers_config = lambda: {
            "keywords": [],
            "negative_keywords": [],
            "rejected_journals": [],
            "domain_boost_keywords": [],
            "keyword_variants": {},
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
        user_config_stub.daily_papers_dir = lambda: ROOT
        user_config_stub.ncbi_api_key = lambda: ""
        user_config_stub.temp_file_path = lambda name: ROOT / name
        sys.modules["user_config"] = user_config_stub

        cas_stub = types.ModuleType("cas_quartiles")
        cas_stub.lookup_journal = lambda journal: {}
        sys.modules["cas_quartiles"] = cas_stub

        profile_stub = types.ModuleType("library_profile")
        profile_stub.load_or_build_library_profile = lambda config, refresh=False: {}
        sys.modules["library_profile"] = profile_stub

        import fetch_and_score as fs

        paper = {
            "date": "2025-08-26",
            "date_for_window": "2025-08-26",
            "date_candidates": ["2025-08-07", "2025-08-26"],
        }
        self.assertTrue(fs.apply_pubmed_date_window(paper, date(2025, 8, 26), date(2025, 8, 29)))
        self.assertEqual(paper["date"], "2025-08-26")

        early_only = {
            "date": "2025-08-07",
            "date_for_window": "2025-08-07",
            "date_candidates": ["2025-08-07", "2025-08-26"],
        }
        self.assertFalse(fs.apply_pubmed_date_window(early_only, date(2025, 8, 26), date(2025, 8, 29)))


if __name__ == "__main__":
    unittest.main()
