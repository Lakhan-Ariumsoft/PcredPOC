"""
Lightweight blur/legibility check for uploaded scans, run synchronously at
upload time — before any OCR or extraction spend happens on a document that
can't reliably be read anyway.

Uses variance-of-Laplacian: a standard, fast blur-detection heuristic. A
sharp image has strong high-frequency edge content (high variance after a
Laplacian filter); a blurry one doesn't. This is a heuristic, not a
guarantee — it has no labeled sharp/blurry CMA document set to calibrate
against, so BLUR_VARIANCE_THRESHOLD is deliberately conservative (biased
toward NOT rejecting a real document) and may need real-world tuning.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

# Below this Laplacian-variance score, a page is flagged as too blurry/low-
# quality to reliably OCR. Threshold is resolution-dependent — tied to the
# render zoom level used in render_pages_grayscale below; don't change one
# without the other.
BLUR_VARIANCE_THRESHOLD = 15.0
RENDER_ZOOM = 1.5
MAX_PAGES_TO_CHECK = 3


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the Laplacian response of a 2D grayscale array."""
    if gray.size == 0:
        return 0.0
    padded = np.pad(gray.astype(np.float64), 1, mode="edge")
    lap = (
        padded[0:-2, 1:-1] + padded[2:, 1:-1] +
        padded[1:-1, 0:-2] + padded[1:-1, 2:] -
        4 * padded[1:-1, 1:-1]
    )
    return float(lap.var())


def is_page_too_blurry(gray: np.ndarray) -> tuple[bool, float]:
    score = laplacian_variance(gray)
    return score < BLUR_VARIANCE_THRESHOLD, score


def render_pages_grayscale_from_bytes(
    pdf_bytes: bytes, start_page: Optional[int] = None, max_pages: int = MAX_PAGES_TO_CHECK,
) -> list[np.ndarray]:
    """
    Render up to max_pages pages to grayscale numpy arrays, starting from
    start_page (1-indexed) if given — so the check samples the pages the
    uploader actually flagged as containing the financial statements,
    rather than always the document's first pages (which might be a sharp
    cover/index page even when the real content later is a bad scan).
    """
    import fitz

    arrays = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total = len(doc)
        start_idx = max(0, (start_page or 1) - 1)
        if start_idx >= total:
            start_idx = 0
        end_idx = min(total, start_idx + max_pages)
        for i in range(start_idx, end_idx):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM),
                                   colorspace=fitz.csGRAY, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            arrays.append(arr)
    return arrays


def check_document_legibility(
    pdf_bytes: bytes, start_page: Optional[int] = None, max_pages: int = MAX_PAGES_TO_CHECK,
) -> dict:
    """
    A document is flagged blurry only if EVERY sampled page falls below the
    threshold — a mix of sharp and blurry pages (e.g. one bad scan among
    several good ones) shouldn't block the whole upload, since extraction
    can often still work off the readable pages.
    """
    pages = render_pages_grayscale_from_bytes(pdf_bytes, start_page, max_pages)
    if not pages:
        return {"blurry": False, "checked_pages": 0, "scores": []}
    results = [is_page_too_blurry(p) for p in pages]
    scores = [round(s, 2) for _, s in results]
    all_blurry = all(b for b, _ in results)
    return {"blurry": all_blurry, "checked_pages": len(pages), "scores": scores}
