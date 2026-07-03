"""Local PDF text and Figure 1 extraction helpers."""

from __future__ import annotations

import io
import re
from pathlib import Path

# ── optional heavy deps ───────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
    _FITZ_OK = True
except ImportError:
    _FITZ_OK = False

try:
    import pypdf as _pypdf
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False


def extract_fig1_from_pdf_bytes(pdf_bytes: bytes, out_path: Path | str) -> bool:
    """
    Extract Figure 1 from PDF bytes and save as PNG to out_path.

    Strategy:
    1. Use fitz get_text("dict") to locate caption "Fig. 1" / "Figure 1" text blocks
       and image blocks by bounding-box.
    2. Crop the combined region (figure + caption) and render at 3×.
    3. Fallback: first large embedded bitmap ≥500×400 px.
    Returns True if an image was saved.
    """
    if not _FITZ_OK or not pdf_bytes:
        return False
    out_path = Path(out_path)
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return False
    CAP_PAT      = re.compile(r"(?i)^fig(?:ure)?\.?\s*1[\.\s\)\,\:]")
    LEGENDS_PAT  = re.compile(r"(?i)^fig(?:ure)?\s*(legends?|captions?|titles?)\s*$")

    for pn, pg in enumerate(doc):
        pg_rect   = pg.rect
        blk_dict  = pg.get_text("dict")["blocks"]
        caption_rect: "fitz.Rect | None" = None
        image_rects:  list = []
        page_is_legends = False

        for blk in blk_dict:
            if blk["type"] == 0:   # text block
                full = " ".join(
                    sp["text"]
                    for ln in blk.get("lines", [])
                    for sp in ln.get("spans", [])
                ).strip()
                # Detect "Figure legends" / "Figure captions" heading pages
                if LEGENDS_PAT.match(full):
                    page_is_legends = True
                if CAP_PAT.match(full) and caption_rect is None:
                    caption_rect = fitz.Rect(blk["bbox"])
            elif blk["type"] == 1:  # image block
                r = fitz.Rect(blk["bbox"])
                if r.width > 80 and r.height > 80:
                    image_rects.append(r)

        if caption_rect is None:
            continue

        # Images above caption (y1 ≤ caption top + 30pt overlap tolerance)
        fig_rects = [r for r in image_rects
                     if r.y1 <= caption_rect.y0 + 30 and r.y0 < caption_rect.y0]

        if fig_rects:
            combined = fig_rects[0]
            for r in fig_rects[1:]:
                combined = combined | r
            combined = combined | caption_rect
            clip = fitz.Rect(
                max(0,          combined.x0 - 8),
                max(0,          combined.y0 - 8),
                min(pg_rect.x1, combined.x1 + 8),
                min(pg_rect.y1, combined.y1 + 50),
            )
            pix = pg.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
            pix.save(str(out_path))
            doc.close()
            return True

        # Vector figure (no image blocks above caption): crop region above caption.
        # Skip if this page is a dedicated "Figure legends" section (text-only,
        # no actual figure above the caption) or if there's not enough space above
        # the caption to hold a real figure.
        fig_height = min(caption_rect.y0, 500)
        if page_is_legends or fig_height < 200:
            continue   # no real figure on this page — keep searching

        clip = fitz.Rect(
            max(0,          caption_rect.x0 - 20),
            max(0,          caption_rect.y0 - fig_height),
            min(pg_rect.x1, caption_rect.x1 + 20),
            min(pg_rect.y1, caption_rect.y1 + 60),
        )
        pix = pg.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
        pix.save(str(out_path))
        doc.close()
        return True

    # Fallback: first large embedded bitmap (original resolution)
    for pn, pg in enumerate(doc):
        for img_info in pg.get_images(full=True):
            try:
                base = doc.extract_image(img_info[0])
                raw  = base["image"]
                w, h = base["width"], base["height"]
                if w >= 500 and h >= 400 and len(raw) >= 50_000:
                    out_path.write_bytes(raw)
                    doc.close()
                    return True
            except Exception:
                pass

    doc.close()
    return False

def extract_fig1_from_pdf_path(pdf_path: Path | str, out_path: Path | str) -> bool:
    """Convenience wrapper that reads a PDF file then calls extract_fig1_from_pdf_bytes."""
    try:
        data = Path(pdf_path).read_bytes()
        return extract_fig1_from_pdf_bytes(data, out_path)
    except Exception:
        return False

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if _FITZ_OK:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            text = "\n".join(pg.get_text() for pg in doc)
            doc.close()
            if text.strip():
                return text
        except Exception:
            pass
    if _PYPDF_OK:
        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            return "\n".join(pg.extract_text() or "" for pg in reader.pages)
        except Exception:
            pass
    return ""
