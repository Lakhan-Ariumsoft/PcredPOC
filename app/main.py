"""
CMA Extraction API v7.0

Cache strategy:
- Upload a doc → OCR cached immediately on first extraction
- On extract trigger → check AI cache first, skip if hit
- New file uploaded → old AI cache for that doc auto-invalidated
- DELETE /cache/all → wipe everything (OCR + AI)
"""
import logging, os, uuid, threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.cma_fields   import CMA_SECTIONS
from app.extractor_ai import extract_cma_fields, clear_ai_cache, clear_all_ai_caches
from app.merger       import merge_documents
from app.pdf_reader   import (
    extract_all_text, get_financial_year,
    clear_all_ocr_caches, clear_ocr_cache_for,
)
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
    description="Upload financial PDFs. AI extracts 199 CMA fields across financial years.",
    version="7.0.0",
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

# ── Pipeline ──────────────────────────────────────────────────────────────────

def _run_pipeline(pdf_path: Path, filename: str, doc_id: str = "") -> dict:
    """
    Full pipeline: PDF → text (cached) → FY detection → AI extraction (cached).
    Second call on same doc_id is instant — everything served from cache.
    """
    pages = extract_all_text(pdf_path)
    current_fy, previous_fy = get_financial_year(pdf_path, pages)
    logger.info(f"{filename}: FY={current_fy} prev={previous_fy} pages={len(pages)}")

    extraction = extract_cma_fields(
        pages       = pages,
        source_file = filename,
        doc_id      = doc_id,
        current_fy  = current_fy,
        previous_fy = previous_fy,
    )
    return {
        "current_fy":   current_fy,
        "previous_fy":  previous_fy,
        "extraction":   extraction,
    }

# ── Background extraction worker ──────────────────────────────────────────────

def _extraction_worker(job_id: str, company_slug: str, company_name: str, documents: list):
    total = len(documents)
    _job_update(job_id, status="running", total=total, progress=0)

    doc_results = []
    for i, doc in enumerate(documents):
        doc_id   = doc["doc_id"]
        filename = doc["filename"]
        _job_update(job_id, progress=i,
                    message=f"Processing {i+1}/{total}: {filename}")

        path = get_document_path(company_slug, doc_id)
        if path is None:
            doc_results.append({"doc_id": doc_id, "filename": filename,
                "status": "error_file_missing", "current_fy": None,
                "previous_fy": None, "extraction": None})
            continue

        try:
            res = _run_pipeline(path, filename, doc_id=doc_id)
            doc_results.append({
                "doc_id":     doc_id,
                "filename":   filename,
                "status":     "success",
                "current_fy":  res["current_fy"],
                "previous_fy": res["previous_fy"],
                "extraction":  res["extraction"],
            })
            logger.info(f"Job {job_id}: done {filename} ({i+1}/{total})")
        except Exception as exc:
            logger.exception(f"Job {job_id}: failed {filename}")
            doc_results.append({"doc_id": doc_id, "filename": filename,
                "status": f"error: {exc}", "current_fy": None,
                "previous_fy": None, "extraction": None})

    try:
        merged = merge_documents(company_slug, company_name, doc_results)
        _job_update(job_id, status="done", progress=total,
                    message="Extraction complete",
                    result=merged,
                    finished_at=datetime.utcnow().isoformat())
        logger.info(f"Job {job_id}: COMPLETE for {company_slug}")
    except Exception as exc:
        logger.exception(f"Job {job_id}: merge failed")
        _job_update(job_id, status="error", error=str(exc))

# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_pdf(file: UploadFile):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted.")

def _read_bytes(file: UploadFile) -> bytes:
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    return data

# ── System ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "7.0.0"}

@app.get("/fields", tags=["System"])
def list_fields():
    return {k: {"label": v["label"], "fields": v["fields"]} for k, v in CMA_SECTIONS.items()}

# ── Documents ─────────────────────────────────────────────────────────────────

@app.post("/documents/upload", status_code=201, tags=["Documents"])
async def upload_document(
    company_name: str = Form(..., min_length=2, max_length=200),
    file: UploadFile = File(...),
):
    """
    Upload a PDF. On duplicate content (same SHA-256), returns 409.
    Clears AI cache for doc_id if re-uploading same filename to force re-extraction.
    """
    _require_pdf(file)
    data = _read_bytes(file)
    try:
        meta = store_document(company_name, file.filename, data)
    except DuplicateDocumentError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.exception("Storage failed")
        raise HTTPException(500, str(e))

    # Clear any stale AI cache for this doc so new upload triggers fresh extraction
    clear_ai_cache(meta["doc_id"])
    logger.info(f"Uploaded: {meta['company_slug']}/{meta['doc_id']} — {meta['filename']}")

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
    """Delete document and its AI cache."""
    if not delete_document(company_slug, doc_id):
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    clear_ai_cache(doc_id)
    return {"message": f"Document '{doc_id}' deleted."}

# ── Cache management ──────────────────────────────────────────────────────────

@app.delete("/cache/all", tags=["Cache"],
            summary="Clear ALL caches (OCR + AI) — forces full re-extraction")
def clear_all_caches():
    """
    Wipes every cached result. Use when:
    - Extraction logic has been updated
    - You want completely fresh results
    - Something looks wrong in outputs
    """
    ai_count  = clear_all_ai_caches()
    ocr_count = clear_all_ocr_caches()
    return {
        "message":       "All caches cleared.",
        "ai_caches_cleared":  ai_count,
        "ocr_caches_cleared": ocr_count,
    }

@app.delete("/cache/ai", tags=["Cache"],
            summary="Clear only AI extraction caches (keeps OCR cache)")
def clear_all_ai():
    count = clear_all_ai_caches()
    return {"message": "AI caches cleared.", "cleared": count}

@app.delete("/cache/ai/{doc_id}", tags=["Cache"],
            summary="Clear AI cache for one document")
def clear_one_ai_cache(doc_id: str):
    cleared = clear_ai_cache(doc_id)
    return {"cleared": cleared, "doc_id": doc_id}

# ── Async extraction (recommended) ───────────────────────────────────────────

@app.post("/extract/trigger/{company_slug}", tags=["Extraction"],
          summary="Trigger background extraction → returns job_id instantly")
def trigger_extraction(company_slug: str):
    """
    Starts extraction in a background thread. Returns job_id immediately.
    Cached docs return instantly; only new/changed docs hit OpenAI.

    Poll: GET /extract/status/{job_id}
    Get result: GET /extract/result/{job_id}
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

    # Return existing running job if one exists
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
        "message":      "Starting...",
        "result":       None,
        "error":        None,
        "started_at":   datetime.utcnow().isoformat(),
        "finished_at":  None,
    })

    t = threading.Thread(
        target=_extraction_worker,
        args=(job_id, company_slug, info["display_name"], docs),
        daemon=True,
    )
    t.start()

    logger.info(f"Started job {job_id} for {company_slug} ({len(docs)} docs)")
    return {
        "job_id": job_id, "status": "pending", "total": len(docs),
        "message": f"Started for {len(docs)} doc(s). Poll /extract/status/{job_id}",
    }


@app.get("/extract/status/{job_id}", tags=["Extraction"])
def get_job_status(job_id: str):
    """Poll every 5 seconds. status: pending | running | done | error"""
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    total   = job.get("total", 1) or 1
    prog    = job.get("progress", 0)
    return {
        "job_id":       job_id,
        "company_slug": job.get("company_slug"),
        "status":       job.get("status"),
        "progress":     prog,
        "total":        total,
        "percent":      round(prog / total * 100),
        "message":      job.get("message"),
        "started_at":   job.get("started_at"),
        "finished_at":  job.get("finished_at"),
        "error":        job.get("error"),
    }


@app.get("/extract/result/{job_id}", tags=["Extraction"])
def get_job_result(job_id: str):
    """Get full CMA result. Returns 202 if still running."""
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    if job.get("status") == "error":
        raise HTTPException(500, f"Extraction failed: {job.get('error')}")
    if job.get("status") in ("pending", "running"):
        total = job.get("total", 1) or 1
        return JSONResponse(status_code=202, content={
            "message": f"Still processing. Check /extract/status/{job_id}",
            "status":  job.get("status"),
            "percent": round(job.get("progress", 0) / total * 100),
        })
    return JSONResponse(content=job["result"])


@app.get("/extract/jobs", tags=["Extraction"])
def list_jobs():
    with _jobs_lock:
        return [
            {"job_id": jid, "company_slug": j.get("company_slug"),
             "status": j.get("status"), "progress": j.get("progress"),
             "total": j.get("total"), "started_at": j.get("started_at"),
             "finished_at": j.get("finished_at")}
            for jid, j in _jobs.items()
        ]

# ── Sync extraction (for cached results — instant) ────────────────────────────

@app.get("/extract/stored/{company_slug}", tags=["Extraction"],
         summary="Sync extract (instant if cached, may timeout on first run)")
def extract_company_sync(company_slug: str):
    """
    Synchronous. Use after jobs complete or when all docs are already cached.
    For first-time extraction of many docs, use POST /extract/trigger instead.
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents found for '{company_slug}'.")

    doc_results = []
    for doc in docs:
        path = get_document_path(company_slug, doc["doc_id"])
        if path is None:
            doc_results.append({"doc_id": doc["doc_id"], "filename": doc["filename"],
                "status": "error_file_missing", "current_fy": None,
                "previous_fy": None, "extraction": None})
            continue
        try:
            res = _run_pipeline(path, doc["filename"], doc_id=doc["doc_id"])
            doc_results.append({"doc_id": doc["doc_id"], "filename": doc["filename"],
                "status": "success", "current_fy": res["current_fy"],
                "previous_fy": res["previous_fy"], "extraction": res["extraction"]})
        except Exception as exc:
            logger.exception(f"Failed: {doc['filename']}")
            doc_results.append({"doc_id": doc["doc_id"], "filename": doc["filename"],
                "status": f"error: {exc}", "current_fy": None,
                "previous_fy": None, "extraction": None})

    return JSONResponse(content=merge_documents(
        company_slug=company_slug,
        company_name=info["display_name"],
        doc_results=doc_results,
    ))


@app.get("/extract/stored/{company_slug}/{doc_id}", tags=["Extraction"])
def extract_single_doc(company_slug: str, doc_id: str):
    """Extract one stored document. Returns from cache if available."""
    path = get_document_path(company_slug, doc_id)
    if path is None:
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    try:
        res = _run_pipeline(path, path.name, doc_id=doc_id)
        return JSONResponse(content={
            "current_fy":   res["current_fy"],
            "previous_fy":  res["previous_fy"],
            **res["extraction"],
        })
    except Exception as exc:
        logger.exception("Single doc extraction failed")
        raise HTTPException(500, str(exc))