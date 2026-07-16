import io

import fitz
from PIL import Image, ImageFilter

from app.utils.image_quality import check_document_legibility, laplacian_variance, BLUR_VARIANCE_THRESHOLD


def _sharp_textful_pdf_bytes(num_pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(num_pages):
        page = doc.new_page()
        for i in range(40):
            page.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56    2,345.67    Note {i}", fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def _blank_pdf_bytes(num_pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _heavily_blurred_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56    2,345.67", fontsize=9)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    blurred = img.filter(ImageFilter.GaussianBlur(radius=8))
    buf = io.BytesIO()
    blurred.save(buf, format="PNG")
    doc.close()

    doc2 = fitz.open()
    p2 = doc2.new_page(width=pix.width, height=pix.height)
    p2.insert_image(p2.rect, stream=buf.getvalue())
    data = doc2.tobytes()
    doc2.close()
    return data


def test_laplacian_variance_of_empty_array_is_zero():
    import numpy as np
    assert laplacian_variance(np.array([[]])) == 0.0


def test_sharp_textful_document_is_not_flagged_blurry():
    result = check_document_legibility(_sharp_textful_pdf_bytes())
    assert result["blurry"] is False
    assert result["checked_pages"] == 1
    assert result["scores"][0] > BLUR_VARIANCE_THRESHOLD


def test_blank_document_is_flagged_blurry():
    result = check_document_legibility(_blank_pdf_bytes())
    assert result["blurry"] is True
    assert result["scores"][0] == 0.0


def test_heavily_blurred_document_is_flagged_blurry():
    result = check_document_legibility(_heavily_blurred_pdf_bytes())
    assert result["blurry"] is True


def test_mixed_sharp_and_blank_pages_not_flagged_blurry():
    # One sharp page among blank ones shouldn't block the whole upload —
    # extraction can still work off the readable page.
    doc = fitz.open()
    doc.new_page()  # blank
    page = doc.new_page()
    for i in range(40):
        page.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56", fontsize=9)
    data = doc.tobytes()
    doc.close()

    result = check_document_legibility(data, max_pages=2)
    assert result["checked_pages"] == 2
    assert result["blurry"] is False


def test_start_page_samples_from_declared_range_not_document_start():
    # Page 1 blank, page 2 sharp — sampling starting at page 2 (1-indexed)
    # should only see the sharp page and not be dragged down by page 1.
    doc = fitz.open()
    doc.new_page()  # page 1: blank
    page2 = doc.new_page()
    for i in range(40):
        page2.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56", fontsize=9)
    data = doc.tobytes()
    doc.close()

    result = check_document_legibility(data, start_page=2, max_pages=1)
    assert result["checked_pages"] == 1
    assert result["blurry"] is False


def test_check_document_legibility_handles_invalid_pdf_bytes_by_raising():
    # Truly invalid PDF bytes propagate as an exception — the caller (the
    # upload endpoint) is responsible for catching this and degrading
    # gracefully rather than blocking the upload on a check failure.
    import pytest
    with pytest.raises(Exception):
        check_document_legibility(b"not a pdf at all")
