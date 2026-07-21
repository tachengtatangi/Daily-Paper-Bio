import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "_shared"))

import pdf_fetcher
import run_reader


def make_pdf_bytes(page_count):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class PublisherAssetTests(unittest.TestCase):
    def test_svg_placeholder_is_not_accepted_as_figure(self):
        svg = b'<svg width="1200" height="800">' + b"x" * 6000 + b"</svg>"
        self.assertFalse(pdf_fetcher._acceptable_image_bytes(svg, "image/svg+xml"))
        jpeg = b"\xff\xd8\xff" + b"x" * 6000
        self.assertTrue(pdf_fetcher._acceptable_image_bytes(jpeg, "image/jpeg"))

    def test_oup_article_id_suffix_matches_target_doi(self):
        self.assertTrue(run_reader.pdf_doi_matches_target(
            "10.1093/sysbio/syag051/8734840",
            "10.1093/sysbio/syag051",
        ))
        self.assertFalse(run_reader.pdf_doi_matches_target(
            "10.1093/sysbio/unrelated/8734840",
            "10.1093/sysbio/syag051",
        ))

    def test_elsevier_pdf_requires_real_pdf_bytes(self):
        completed = type("Result", (), {
            "returncode": 0,
            "stdout": make_pdf_bytes(2),
        })()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "paper.pdf"
            with patch.object(pdf_fetcher.subprocess, "run", return_value=completed):
                saved = pdf_fetcher._elsevier_fetch_pdf_via_curl(
                    "10.1016/j.test.2026.1", "key", target
                )
            self.assertEqual(saved, str(target))
            self.assertTrue(target.read_bytes().startswith(b"%PDF"))

    def test_elsevier_cover_page_is_rejected(self):
        completed = type("Result", (), {
            "returncode": 0,
            "stdout": make_pdf_bytes(1),
        })()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "paper.pdf"
            with patch.object(pdf_fetcher.subprocess, "run", return_value=completed):
                saved = pdf_fetcher._elsevier_fetch_pdf_via_curl(
                    "10.1016/j.test.2026.1", "key", target
                )
            self.assertEqual(saved, "")
            self.assertFalse(target.exists())

    def test_elsevier_fig1_uses_compact_pii_cdn(self):
        xml = "<pii>S0965-1748(26)00160-8</pii>"
        payload = b"\xff\xd8\xff" + b"x" * 2500
        completed = type("Result", (), {
            "returncode": 0,
            "stdout": payload,
        })()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "figure.png"
            with patch.object(pdf_fetcher.subprocess, "run", return_value=completed) as mocked:
                saved, source = pdf_fetcher._elsevier_fetch_fig1_via_curl(xml, target)
            self.assertTrue(saved.endswith(".jpg"))
            self.assertEqual(
                source,
                "https://ars.els-cdn.com/content/image/"
                "1-s2.0-S0965174826001608-gr1.jpg",
            )
            self.assertIn(source, mocked.call_args.args[0])
