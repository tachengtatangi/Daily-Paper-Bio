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


if __name__ == "__main__":
    unittest.main()
