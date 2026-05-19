"""
CMA Extraction API  v6.0 — Async background extraction

New flow:
1. POST /extract/trigger/{company_slug}  → starts background job, returns job_id immediately
2. GET  /extract/status/{job_id}         → poll for progress (pending/running/done/error)
3. GET  /extract/result/{job_id}         → get final merged CMA data when done

Old sync endpoint still works if result is already cached (instant response).
"""
import logging, os, uuid, threading
from pathlib import Path
from typing import Optional
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.cma_fields   import CMA_SECTIONS
from app.extractor_ai import extract_cma_fields, clear_ai_cache
from app.merger       import merge_documents
from app.pdf_reader   import extract_all_text, get_financial_year
from app.storage      import (
    DuplicateDocumentError, delete_document, get_document_path,
    list_all_documents, list_company_documents, store_document,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CMA Extraction API",
    description="Upload financial PDFs per company. AI extracts all CMA fields year-wise.",
    version="6.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path(os.environ.get("TEMP_DIR", "temp"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# ── In-memory job store ───────────────────────────────────────────────────────
# { job_id: { status, company_slug, progress, total, message, result, error, started_at } }
_jobs: dict = {}
_jobs_lock = threading.Lock()

def _job_get(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _jobs.get(job_id)

def _job_set(job_id: str, data: dict):
    with _jobs_lock:
        _jobs[job_id] = data

def _job_update(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_pdf(file: UploadFile):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

def _read_bytes(file: UploadFile) -> bytes:
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    return data

def _run_pipeline(pdf_path: Path, filename: str, doc_id: str = "") -> dict:
    pages = extract_all_text(pdf_path)
    primary_fy, sec_fy = get_financial_year(pdf_path, pages)
    logger.info(f"{filename}: FY={primary_fy} | {len(pages)} pages")
    extraction = extract_cma_fields(pages=pages, source_file=filename, doc_id=doc_id)
    return {"primary_fy": primary_fy, "secondary_fy": sec_fy, "extraction": extraction}

# ── Background extraction worker ──────────────────────────────────────────────

def _extraction_worker(job_id: str, company_slug: str, company_name: str, documents: list):
    """Runs in a thread. Processes each doc, updates job status, stores result."""
    total = len(documents)
    _job_update(job_id, status="running", total=total, progress=0)

    doc_results = []
    for i, doc in enumerate(documents):
        doc_id   = doc["doc_id"]
        filename = doc["filename"]
        _job_update(job_id,
            progress=i,
            message=f"Processing {i+1}/{total}: {filename}"
        )

        path = get_document_path(company_slug, doc_id)
        if path is None:
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": "error_file_missing",
                "primary_fy": None, "secondary_fy": None, "extraction": None,
            })
            continue

        try:
            res = _run_pipeline(path, filename, doc_id=doc_id)
            doc_results.append({
                "doc_id":       doc_id,
                "filename":     filename,
                "status":       "success",
                "primary_fy":   res["primary_fy"],
                "secondary_fy": res["secondary_fy"],
                "extraction":   res["extraction"],
            })
            logger.info(f"Job {job_id}: done {filename} ({i+1}/{total})")
        except Exception as exc:
            logger.exception(f"Job {job_id}: failed {filename}")
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": f"error: {exc}",
                "primary_fy": None, "secondary_fy": None, "extraction": None,
            })

    # Merge and store result
    try:
        merged = merge_documents(
            company_slug=company_slug,
            company_name=company_name,
            doc_results=doc_results,
        )
        _job_update(job_id,
            status="done",
            progress=total,
            message="Extraction complete",
            result=merged,
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"Job {job_id}: COMPLETE for {company_slug}")
    except Exception as exc:
        logger.exception(f"Job {job_id}: merge failed")
        _job_update(job_id, status="error", error=str(exc))

# ── System ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "6.0.0"}

@app.get("/fields", tags=["System"])
def list_fields():
    return {k: {"label": v["label"], "fields": v["fields"]} for k, v in CMA_SECTIONS.items()}

# ── Documents ─────────────────────────────────────────────────────────────────

@app.post("/documents/upload", status_code=201, tags=["Documents"])
async def upload_document(
    company_name: str = Form(..., min_length=2, max_length=200),
    file: UploadFile = File(...),
):
    _require_pdf(file)
    data = _read_bytes(file)
    try:
        meta = store_document(company_name, file.filename, data)
    except DuplicateDocumentError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.exception("Storage failed")
        raise HTTPException(500, str(e))
    return JSONResponse(status_code=201, content={
        "message":      "Uploaded successfully.",
        "doc_id":       meta["doc_id"],
        "company_slug": meta["company_slug"],
        "company_name": meta["company_name"],
        "filename":     meta["filename"],
        "size_bytes":   meta["size_bytes"],
        "uploaded_at":  meta["uploaded_at"],
    })

@app.get("/documents", tags=["Documents"])
def get_all_documents():
    return list_all_documents()

@app.get("/documents/{company_slug}", tags=["Documents"])
def get_company_documents(company_slug: str):
    r = list_company_documents(company_slug)
    if r is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    return r

@app.delete("/documents/{company_slug}/{doc_id}", tags=["Documents"])
def remove_document(company_slug: str, doc_id: str):
    if not delete_document(company_slug, doc_id):
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    clear_ai_cache(doc_id)
    return {"message": f"Document '{doc_id}' deleted."}

# ── Cache ─────────────────────────────────────────────────────────────────────

@app.delete("/extract/cache/{doc_id}", tags=["Extraction"])
def clear_cache(doc_id: str):
    return {"cleared": clear_ai_cache(doc_id), "doc_id": doc_id}

# ── ASYNC extraction (recommended for multiple/large docs) ────────────────────

@app.post("/extract/trigger/{company_slug}", tags=["Extraction"],
          summary="Trigger background extraction → returns job_id immediately")
def trigger_extraction(company_slug: str):
    """
    Starts extraction in background. Returns job_id instantly.
    Poll GET /extract/status/{job_id} for progress.
    Get result from GET /extract/result/{job_id} when done.

    If a job is already running for this company, returns existing job_id.
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

    # Check if there's already a running/done job for this company
    with _jobs_lock:
        for jid, job in _jobs.items():
            if job.get("company_slug") == company_slug and job.get("status") in ("pending", "running"):
                return {"job_id": jid, "status": job["status"], "message": "Job already running"}

    job_id = uuid.uuid4().hex
    _job_set(job_id, {
        "job_id":       job_id,
        "company_slug": company_slug,
        "company_name": info["display_name"],
        "status":       "pending",
        "progress":     0,
        "total":        len(docs),
        "message":      "Starting extraction...",
        "result":       None,
        "error":        None,
        "started_at":   datetime.utcnow().isoformat(),
        "finished_at":  None,
    })

    # Launch background thread
    t = threading.Thread(
        target=_extraction_worker,
        args=(job_id, company_slug, info["display_name"], docs),
        daemon=True,
    )
    t.start()

    logger.info(f"Started extraction job {job_id} for {company_slug} ({len(docs)} docs)")
    return {
        "job_id":   job_id,
        "status":   "pending",
        "total":    len(docs),
        "message":  f"Extraction started for {len(docs)} document(s). Poll /extract/status/{job_id}",
    }


@app.get("/extract/status/{job_id}", tags=["Extraction"],
         summary="Poll extraction job progress")
def get_job_status(job_id: str):
    """
    Poll this every 5 seconds from frontend.
    Returns: { job_id, status, progress, total, message, percent }
    status: pending | running | done | error
    """
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")

    total   = job.get("total", 1) or 1
    prog    = job.get("progress", 0)
    percent = round(prog / total * 100)

    return {
        "job_id":       job_id,
        "company_slug": job.get("company_slug"),
        "status":       job.get("status"),
        "progress":     prog,
        "total":        total,
        "percent":      percent,
        "message":      job.get("message"),
        "started_at":   job.get("started_at"),
        "finished_at":  job.get("finished_at"),
        "error":        job.get("error"),
    }


@app.get("/extract/result/{job_id}", tags=["Extraction"],
         summary="Get extraction result when job is done")
def get_job_result(job_id: str):
    """
    Returns the full merged CMA data once status = 'done'.
    Returns 202 if still processing, 500 if error.
    """
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")

    status = job.get("status")
    if status == "error":
        raise HTTPException(500, f"Extraction failed: {job.get('error')}")
    if status in ("pending", "running"):
        return JSONResponse(status_code=202, content={
            "message": "Still processing. Check /extract/status/" + job_id,
            "status":  status,
            "percent": round(job.get("progress", 0) / (job.get("total", 1) or 1) * 100),
        })

    return JSONResponse(content=job["result"])


@app.get("/extract/jobs", tags=["Extraction"],
         summary="List all extraction jobs")
def list_jobs():
    with _jobs_lock:
        return [
            {
                "job_id":       jid,
                "company_slug": j.get("company_slug"),
                "status":       j.get("status"),
                "progress":     j.get("progress"),
                "total":        j.get("total"),
                "started_at":   j.get("started_at"),
                "finished_at":  j.get("finished_at"),
            }
            for jid, j in _jobs.items()
        ]


# ── SYNC extraction (works instantly if cached, otherwise may timeout) ─────────

@app.get("/extract/stored/{company_slug}", tags=["Extraction"],
         summary="Sync extract (use /extract/trigger instead for large datasets)")
def extract_company_all_years(company_slug: str):
    """
    Synchronous extraction. Fine if all docs are already cached (fast).
    For fresh extraction of many docs, use POST /extract/trigger/{company_slug} instead.
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

    doc_results = []
    for doc in docs:
        doc_id, filename = doc["doc_id"], doc["filename"]
        path = get_document_path(company_slug, doc_id)
        if path is None:
            doc_results.append({"doc_id": doc_id, "filename": filename,
                "status": "error_file_missing", "primary_fy": None,
                "secondary_fy": None, "extraction": None})
            continue
        try:
            res = _run_pipeline(path, filename, doc_id=doc_id)
            doc_results.append({"doc_id": doc_id, "filename": filename,
                "status": "success", "primary_fy": res["primary_fy"],
                "secondary_fy": res["secondary_fy"], "extraction": res["extraction"]})
        except Exception as exc:
            logger.exception(f"Failed: {filename}")
            doc_results.append({"doc_id": doc_id, "filename": filename,
                "status": f"error: {exc}", "primary_fy": None,
                "secondary_fy": None, "extraction": None})

    return JSONResponse(content=merge_documents(
        company_slug=company_slug,
        company_name=info["display_name"],
        doc_results=doc_results,
    ))


@app.get("/extract/stored/{company_slug}/{doc_id}", tags=["Extraction"])
def extract_single_doc(company_slug: str, doc_id: str):
    path = get_document_path(company_slug, doc_id)
    if path is None:
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    try:
        res = _run_pipeline(path, path.name, doc_id=doc_id)
        return JSONResponse(content={
            "primary_fy": res["primary_fy"],
            "secondary_fy": res["secondary_fy"],
            **res["extraction"],
        })
    except Exception as exc:
        logger.exception("Extraction failed")
        raise HTTPException(500, str(exc))