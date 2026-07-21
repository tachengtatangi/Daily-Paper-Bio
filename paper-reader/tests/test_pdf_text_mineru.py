import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "_shared"))

import reader_pdf_text


class MinerUTextTests(unittest.TestCase):
    def test_default_timeout_allows_full_paper_processing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(reader_pdf_text.mineru_timeout_seconds(), 900)

    def test_existing_markdown_is_reused_without_running_mineru(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pdf_dir = root / "AllPdfFig"
            pdf_dir.mkdir()
            pdf_path = pdf_dir / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            identifier = "10.1093/sysbio/syag051"
            output_root = pdf_dir / "mineru" / reader_pdf_text.safe_filename(identifier)
            markdown_path = output_root / "paper" / "auto" / "paper.md"
            markdown_path.parent.mkdir(parents=True)
            markdown_path.write_text(
                "# Specific paper\n\nAbstract\n\n" + "Empirical Bayes simulation result. " * 100,
                encoding="utf-8",
            )

            with patch.object(
                reader_pdf_text.subprocess,
                "run",
                side_effect=AssertionError("MinerU must not run when cached Markdown is valid"),
            ):
                result = reader_pdf_text.extract_pdf_text_with_mineru(
                    pdf_path,
                    identifier,
                    find_tool_func=lambda _name: None,
                    pdf_save_dir=pdf_dir,
                )

            self.assertIn("Empirical Bayes simulation result", result["text"])
            self.assertEqual(result["markdown_path"], str(markdown_path))
            self.assertEqual(result["output_dir"], str(output_root))
            self.assertEqual(result["summary_mode"], "基于 MinerU PDF→Markdown 全文提取")


if __name__ == "__main__":
    unittest.main()
