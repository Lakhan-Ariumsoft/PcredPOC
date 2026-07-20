import uuid
from typing import Optional

from fastapi.testclient import TestClient

from app.main import app
from app.services.storage import delete_document

client = TestClient(app)


def _upload(company_name: str, extra: Optional[dict] = None,
            filename: str = "doc.pdf", content: bytes = b"%PDF-1.4 fake"):
    data = {"company_name": company_name}
    if extra:
        data.update(extra)
    files = {"file": (filename, content, "application/pdf")}
    return client.post("/cma/documents/upload", data=data, files=files)


def _unique_company() -> str:
    return f"Upload Test Co {uuid.uuid4().hex[:8]}"


def test_upload_with_entity_metadata_persists_and_returns_it():
    company = _unique_company()
    resp = _upload(company, {
        "entity_type": "llp",
        "start_page": "2",
        "end_page": "8",
        "notes": "Balance Sheet on page 4",
    })
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["entity_type"] == "llp"
        assert body["start_page"] == 2
        assert body["end_page"] == 8
        assert body["notes"] == "Balance Sheet on page 4"
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def test_upload_without_metadata_defaults_to_null():
    company = _unique_company()
    resp = _upload(company)
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["entity_type"] is None
        assert body["start_page"] is None
        assert body["end_page"] is None
        assert body["notes"] is None
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def test_upload_rejects_start_page_after_end_page():
    resp = _upload(_unique_company(), {"start_page": "9", "end_page": "2"})
    assert resp.status_code == 400


def test_upload_rejects_invalid_entity_type():
    resp = _upload(_unique_company(), {"entity_type": "sole_trader_llc_invalid"})
    assert resp.status_code == 422


def test_upload_rejects_non_pdf_file():
    resp = _upload(_unique_company(), filename="doc.txt", content=b"not a pdf")
    assert resp.status_code == 400


def _real_pdf_bytes(num_pages: int = 3) -> bytes:
    """
    A real, openable, non-blank PDF — text content is required, not just a
    valid PDF structure, since blank pages are correctly caught by the
    legibility check as "nothing readable here" (see test_image_quality.py).
    """
    import fitz
    doc = fitz.open()
    for _ in range(num_pages):
        page = doc.new_page()
        for i in range(30):
            page.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56    2,345.67", fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def test_upload_returns_cost_estimate_for_valid_pdf():
    company = _unique_company()
    resp = _upload(company, content=_real_pdf_bytes(3))
    assert resp.status_code == 201
    body = resp.json()
    try:
        est = body["extraction_estimate"]
        assert est is not None
        assert est["total_pages"] == 3
        assert est["documents"] == 1
        assert "estimated_cost_usd" in est
        assert "estimated_tokens" in est
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def test_upload_returns_estimate_scoped_to_page_range():
    company = _unique_company()
    resp = _upload(company, {"start_page": "1", "end_page": "2"}, content=_real_pdf_bytes(5))
    assert resp.status_code == 201
    body = resp.json()
    try:
        assert body["extraction_estimate"]["total_pages"] == 2
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def test_upload_gracefully_handles_estimate_failure_for_invalid_pdf():
    company = _unique_company()
    resp = _upload(company, content=b"%PDF-1.4 not a real pdf structure")
    assert resp.status_code == 201
    body = resp.json()
    try:
        # Fake content isn't a real PDF fitz can open — estimate degrades to
        # None rather than the upload itself failing.
        assert body["extraction_estimate"] is None
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def _blank_pdf_bytes(num_pages: int = 1) -> bytes:
    import fitz
    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_upload_rejects_blank_unreadable_pdf():
    company = _unique_company()
    resp = _upload(company, content=_blank_pdf_bytes(2))
    assert resp.status_code == 400
    assert "blurry" in resp.json()["detail"].lower()

    # Confirm nothing was actually stored for a rejected upload.
    listing = client.get("/cma/documents")
    slugs = [c["slug"] for c in listing.json()["companies"]]
    from app.services.storage import _slugify
    assert _slugify(company) not in slugs


def test_upload_accepts_textful_pdf_past_legibility_check():
    company = _unique_company()
    resp = _upload(company, content=_real_pdf_bytes(1))
    assert resp.status_code == 201
    body = resp.json()
    delete_document(body["company_slug"], body["doc_id"])


def test_list_company_documents_reflects_uploaded_metadata():
    company = _unique_company()
    resp = _upload(company, {"entity_type": "partnership", "start_page": "1", "end_page": "3"})
    assert resp.status_code == 201
    body = resp.json()
    try:
        listing = client.get(f"/cma/documents/{body['company_slug']}")
        assert listing.status_code == 200
        doc = next(d for d in listing.json()["documents"] if d["doc_id"] == body["doc_id"])
        assert doc["entity_type"] == "partnership"
        assert doc["start_page"] == 1
        assert doc["end_page"] == 3
    finally:
        delete_document(body["company_slug"], body["doc_id"])


def test_company_slug_lookups_are_case_insensitive_across_endpoints():
    """
    Uploading under one casing of the company name must be findable via
    GET /cma/documents/{company_slug} and DELETE .../{company_slug}/{doc_id}
    no matter how the caller capitalizes the slug in the URL later.
    """
    company = f"Charbhuja {_unique_company()}"
    resp = _upload(company, content=_real_pdf_bytes(1))
    assert resp.status_code == 201
    body = resp.json()
    slug = body["company_slug"]  # already lowercase from _slugify

    try:
        for variant in (slug.upper(), slug.title()):
            got = client.get(f"/cma/documents/{variant}")
            assert got.status_code == 200, f"lookup failed for case variant {variant!r}"
            assert got.json()["slug"] == slug
    finally:
        deleted = client.delete(f"/cma/documents/{slug.upper()}/{body['doc_id']}")
        assert deleted.status_code == 200
