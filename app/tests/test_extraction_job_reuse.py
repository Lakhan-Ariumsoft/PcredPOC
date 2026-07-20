import uuid
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.api.cma import _jobs, _jobs_lock
from app.services.storage import delete_document

client = TestClient(app)


def _unique_company() -> str:
    return f"Job Reuse Test Co {uuid.uuid4().hex[:8]}"


def _real_pdf_bytes(num_pages: int = 1) -> bytes:
    import fitz
    doc = fitz.open()
    for _ in range(num_pages):
        page = doc.new_page()
        for i in range(30):
            page.insert_text((30, 20 + i * 15), f"Line item {i}    1,234.56    2,345.67", fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def _upload(company_name: str) -> dict:
    files = {"file": ("financials.pdf", _real_pdf_bytes(), "application/pdf")}
    resp = client.post("/cma/documents/upload", data={"company_name": company_name}, files=files)
    assert resp.status_code == 201
    return resp.json()


def _inject_done_job(company_slug: str, doc_ids: list) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id, "company_slug": company_slug, "company_name": company_slug,
            "status": "done", "progress": 1, "total": 1, "doc_ids": sorted(doc_ids),
            "message": "Extraction complete", "raw": False,
            "result": {"cma_data": {}, "financial_years": []}, "error": None,
            "started_at": datetime.utcnow().isoformat(), "finished_at": datetime.utcnow().isoformat(),
        }
    return job_id


def _inject_running_job(company_slug: str) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id, "company_slug": company_slug, "company_name": company_slug,
            "status": "running", "progress": 1, "total": 2, "doc_ids": [],
            "message": "Processing 2/2", "raw": False,
            "result": None, "error": None,
            "started_at": datetime.utcnow().isoformat(), "finished_at": None,
        }
    return job_id


def _remove_job(job_id: str):
    with _jobs_lock:
        _jobs.pop(job_id, None)


def test_trigger_reuses_completed_job_for_same_document_set():
    company = _unique_company()
    body = _upload(company)
    slug = body["company_slug"]
    existing_job_id = _inject_done_job(slug, [body["doc_id"]])
    try:
        resp = client.post(f"/cma/extract/trigger/{slug}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == existing_job_id, "should reuse the already-completed job, not start a new one"
        assert data["status"] == "done"
        assert data["percent"] == 100
    finally:
        _remove_job(existing_job_id)
        delete_document(slug, body["doc_id"])


def test_trigger_force_true_starts_fresh_even_if_done_job_exists():
    company = _unique_company()
    body = _upload(company)
    slug = body["company_slug"]
    existing_job_id = _inject_done_job(slug, [body["doc_id"]])
    new_job_id = None
    try:
        resp = client.post(f"/cma/extract/trigger/{slug}?force=true")
        assert resp.status_code == 200
        data = resp.json()
        new_job_id = data["job_id"]
        assert new_job_id != existing_job_id
        assert data["status"] == "pending"
        assert data["percent"] == 0
    finally:
        _remove_job(existing_job_id)
        if new_job_id:
            _remove_job(new_job_id)
        delete_document(slug, body["doc_id"])


def test_trigger_starts_fresh_when_document_set_changed():
    company = _unique_company()
    body = _upload(company)
    slug = body["company_slug"]
    # "done" job recorded against a doc_id that is NOT in the current set —
    # simulates a new document having been uploaded since that job ran.
    stale_job_id = _inject_done_job(slug, ["some-other-doc-id-not-uploaded"])
    new_job_id = None
    try:
        resp = client.post(f"/cma/extract/trigger/{slug}")
        assert resp.status_code == 200
        data = resp.json()
        new_job_id = data["job_id"]
        assert new_job_id != stale_job_id
        assert data["status"] == "pending"
    finally:
        _remove_job(stale_job_id)
        if new_job_id:
            _remove_job(new_job_id)
        delete_document(slug, body["doc_id"])


def test_trigger_still_dedupes_running_job_with_percent():
    company = _unique_company()
    body = _upload(company)
    slug = body["company_slug"]
    running_job_id = _inject_running_job(slug)
    try:
        resp = client.post(f"/cma/extract/trigger/{slug}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == running_job_id
        assert data["status"] == "running"
        assert data["percent"] == 50  # progress=1, total=2
    finally:
        _remove_job(running_job_id)
        delete_document(slug, body["doc_id"])


def test_excel_endpoint_reuses_completed_job_and_points_to_download():
    company = _unique_company()
    body = _upload(company)
    slug = body["company_slug"]
    existing_job_id = _inject_done_job(slug, [body["doc_id"]])
    try:
        resp = client.get(f"/cma/extract/excel/{slug}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == existing_job_id
        assert data["status"] == "done"
        assert f"/cma/extract/excel/job/{existing_job_id}" in data["excel_download_hint"]
    finally:
        _remove_job(existing_job_id)
        delete_document(slug, body["doc_id"])
