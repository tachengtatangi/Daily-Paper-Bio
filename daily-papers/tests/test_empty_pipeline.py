import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_review
from pipeline_guard import PipelineGuardError, require_enriched_ready, require_top30_ready


class EmptyPipelineTests(unittest.TestCase):
    def _write(self, path: Path, value) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_successful_empty_fetch_is_valid_and_reviewable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / "status.json"
            top = root / "top.json"
            enriched = root / "enriched.json"
            self._write(status, {
                "status": "success",
                "window_end": "2026-07-13",
                "days": 3,
                "top_count": 0,
                "source_fetch_error_count": 0,
            })
            self._write(top, [])
            self._write(enriched, [])

            self.assertEqual(require_top30_ready(
                top30_path=top,
                status_path=status,
                expected_date="2026-07-13",
                expected_days=3,
            ), [])
            self.assertEqual(require_enriched_ready(
                enriched_path=enriched,
                top30_path=top,
                status_path=status,
                expected_date="2026-07-13",
                expected_days=3,
            ), [])
            report = build_review.build_markdown([], "2026-07-13")
            self.assertIn("本期无合格论文", report)
            self.assertNotIn("TODO_AGENT", report)

    def test_source_failure_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / "status.json"
            top = root / "top.json"
            self._write(status, {
                "status": "success",
                "window_end": "2026-07-13",
                "days": 3,
                "top_count": 0,
                "source_fetch_error_count": 1,
            })
            self._write(top, [])
            with self.assertRaises(PipelineGuardError):
                require_top30_ready(top30_path=top, status_path=status)


if __name__ == "__main__":
    unittest.main()
