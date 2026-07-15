import logging
import uuid
import asyncio
import threading
from datetime import datetime
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import JSONResponse, Response, HTMLResponse

from app.schemas.cma_fields import CMA_SECTIONS, CMA_FIELDS_FLAT
from app.schemas.common import EntityType
from app.services.storage import (
    DuplicateDocumentError, delete_document, get_document_meta, get_document_path,
    list_all_documents, list_company_documents, store_document
)
from app.services.merger import merge_documents
from app.services.cma_validator import validate_extraction
from app.services.cma_reporter_service import generate_cma_excel
from app.services.docling_service import DoclingService
from app.services.cma_extraction_service import (
    extract_cma_fields, clear_ai_cache, clear_all_ai_caches, estimate_pipeline_cost,
)
from app.core_config import get_settings
from app.utils.fy_detector import get_financial_year

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CMA"])

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

async def _run_pipeline(pdf_path: Path, filename: str, doc_id: str = "", company_slug: str = "") -> dict:
    doc_meta = get_document_meta(company_slug, doc_id) if company_slug and doc_id else None
    doc_meta = doc_meta or {}
    entity_type = doc_meta.get("entity_type") or "private_limited"
    start_page  = doc_meta.get("start_page")
    end_page    = doc_meta.get("end_page")
    notes       = doc_meta.get("notes") or ""

    docling = DoclingService()
    pages = await docling.convert_to_pages(pdf_path)
    current_fy, previous_fy = get_financial_year(pdf_path, pages)
    logger.info(
        f"{filename}: FY={current_fy} prev={previous_fy} pages={len(pages)} "
        f"entity_type={entity_type} page_range=({start_page},{end_page})"
    )

    extraction = await extract_cma_fields(
        pages       = pages,
        source_file = filename,
        doc_id      = doc_id,
        current_fy  = current_fy,
        previous_fy = previous_fy,
        entity_type = entity_type,
        start_page  = start_page,
        end_page    = end_page,
        notes       = notes,
    )
    return {
        "current_fy":   current_fy,
        "previous_fy":  previous_fy,
        "extraction":   extraction,
    }

# ── Background extraction worker ──────────────────────────────────────────────

async def _extraction_worker(job_id: str, company_slug: str, company_name: str, documents: list, raw: bool = False):
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
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": "error_file_missing", "current_fy": None,
                "previous_fy": None, "extraction": None
            })
            continue

        try:
            res = await _run_pipeline(path, filename, doc_id=doc_id, company_slug=company_slug)
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
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": f"error: {exc}", "current_fy": None,
                "previous_fy": None, "extraction": None
            })

    try:
        merged = validate_extraction(merge_documents(company_slug, company_name, doc_results))
        _job_update(
            job_id, status="done", progress=total,
            message="Extraction complete",
            result=merged,
            finished_at=datetime.utcnow().isoformat()
        )
        logger.info(f"Job {job_id}: COMPLETE for {company_slug}")
    except Exception as exc:
        logger.exception(f"Job {job_id}: merge/validation failed")
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


# ── System Endpoints ──────────────────────────────────────────────────────────

@router.get("/cma/fields")
def list_fields():
    return {
        "field_mode": "full_199",
        "total_fields": len(CMA_FIELDS_FLAT),
        "sections": {k: {"label": v["label"], "fields": v["fields"]} for k, v in CMA_SECTIONS.items()},
    }


@router.get("/cma/estimate/{company_slug}")
def estimate_extraction_cost(company_slug: str):
    """
    Estimate token usage and USD cost for extracting this company's
    documents BEFORE running it — a heuristic ballpark (see
    estimate_pipeline_cost's note field), not a measured quote.
    Honors each document's stored start_page/end_page so the estimate
    reflects what would actually be OCR'd/extracted, not the raw PDF length.
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

    import fitz

    per_doc = []
    page_counts = []
    for doc in docs:
        path = get_document_path(company_slug, doc["doc_id"])
        if path is None:
            continue
        try:
            with fitz.open(path) as pdf:
                raw_pages = len(pdf)
        except Exception:
            raw_pages = 1

        meta = get_document_meta(company_slug, doc["doc_id"]) or {}
        sp = meta.get("start_page") or 1
        ep = meta.get("end_page") or raw_pages
        effective_pages = max(0, min(ep, raw_pages) - sp + 1)

        page_counts.append(effective_pages)
        per_doc.append({
            "filename": doc["filename"],
            "raw_pages": raw_pages,
            "pages_to_process": effective_pages,
            "entity_type": meta.get("entity_type"),
        })

    settings = get_settings()
    estimate = estimate_pipeline_cost(page_counts, settings.docling_model, settings.extraction_model)

    return {
        "company_slug": company_slug,
        "docling_model": settings.docling_model,
        "extraction_model": settings.extraction_model,
        "documents": per_doc,
        **estimate,
    }


# ── Documents Endpoints ───────────────────────────────────────────────────────

@router.post("/cma/documents/upload", status_code=201)
async def upload_document(
    company_name: str = Form(..., min_length=2, max_length=200),
    file: UploadFile = File(...),
    entity_type: Optional[EntityType] = Form(None),
    start_page: Optional[int] = Form(None, ge=1),
    end_page: Optional[int] = Form(None, ge=1),
    notes: Optional[str] = Form(None, max_length=2000),
):
    """
    Upload a PDF for a specific company.

    entity_type / start_page / end_page / notes are optional extraction
    hints. entity_type tells the pipeline which balance-sheet vocabulary to
    expect (e.g. "llp" uses Partners' Capital instead of Share Capital).
    start_page/end_page restrict OCR+extraction to the pages that actually
    contain the financial statements, and notes is free text (e.g. "Balance
    Sheet on pg 4, P&L on pg 5") that gets appended to the AI prompt.
    """
    _require_pdf(file)
    data = _read_bytes(file)
    if start_page is not None and end_page is not None and start_page > end_page:
        raise HTTPException(400, "start_page must be <= end_page.")
    try:
        meta = store_document(
            company_name, file.filename, data,
            entity_type=entity_type.value if entity_type else None,
            start_page=start_page,
            end_page=end_page,
            notes=notes,
        )
    except DuplicateDocumentError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.exception("Storage failed")
        raise HTTPException(500, str(e))

    # Clear any stale AI cache for this doc
    clear_ai_cache(meta["doc_id"])
    logger.info(f"Uploaded: {meta['company_slug']}/{meta['doc_id']} — {meta['filename']}")

    # Heuristic pre-flight estimate for just this document, so cost is
    # visible immediately on upload rather than requiring a separate call
    # to GET /cma/estimate/{company_slug} before deciding whether to extract.
    estimate = None
    try:
        import fitz
        with fitz.open(meta["path"]) as pdf:
            raw_pages = len(pdf)
        sp = start_page or 1
        ep = end_page or raw_pages
        effective_pages = max(0, min(ep, raw_pages) - sp + 1)
        settings = get_settings()
        estimate = estimate_pipeline_cost([effective_pages], settings.docling_model, settings.extraction_model)
    except Exception as e:
        logger.warning(f"Cost estimate failed for {meta['doc_id']}: {e}")

    return JSONResponse(status_code=201, content={
        "message":      "Uploaded successfully.",
        "doc_id":       meta["doc_id"],
        "company_slug": meta["company_slug"],
        "company_name": meta["company_name"],
        "filename":     meta["filename"],
        "size_bytes":   meta["size_bytes"],
        "uploaded_at":  meta["uploaded_at"],
        "entity_type":  meta["entity_type"],
        "start_page":   meta["start_page"],
        "end_page":     meta["end_page"],
        "notes":        meta["notes"],
        "extraction_estimate": estimate,
    })


@router.get("/cma/documents")
def get_all_documents():
    return list_all_documents()


@router.get("/cma/documents/{company_slug}")
def get_company_documents(company_slug: str):
    r = list_company_documents(company_slug)
    if r is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    return r


@router.delete("/cma/documents/{company_slug}/{doc_id}")
def remove_document(company_slug: str, doc_id: str):
    if not delete_document(company_slug, doc_id):
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    clear_ai_cache(doc_id)
    return {"message": f"Document '{doc_id}' deleted."}


# ── Cache Endpoints ───────────────────────────────────────────────────────────

@router.delete("/cma/cache/all")
def clear_all_caches():
    ai_count  = clear_all_ai_caches()
    docling = DoclingService()
    ocr_count = docling.clear_all_ocr_caches()
    return {
        "message":       "All caches cleared.",
        "ai_caches_cleared":  ai_count,
        "ocr_caches_cleared": ocr_count,
    }


@router.delete("/cma/cache/ai")
def clear_all_ai():
    count = clear_all_ai_caches()
    return {"message": "AI caches cleared.", "cleared": count}


@router.delete("/cma/cache/ai/{doc_id}")
def clear_one_ai_cache(doc_id: str):
    cleared = clear_ai_cache(doc_id)
    return {"cleared": cleared, "doc_id": doc_id}


# ── Extraction Endpoints ──────────────────────────────────────────────────────

@router.post("/cma/extract/trigger/{company_slug}")
async def trigger_extraction(company_slug: str, background_tasks: BackgroundTasks, raw: bool = False):
    """
    Trigger extraction job in the background. Returns job_id instantly.
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

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
        "raw":          raw,
        "result":       None,
        "error":        None,
        "started_at":   datetime.utcnow().isoformat(),
        "finished_at":  None,
    })

    background_tasks.add_task(
        _extraction_worker,
        job_id, company_slug, info["display_name"], docs, raw
    )

    logger.info(f"Started job {job_id} for {company_slug} ({len(docs)} docs) raw={raw}")
    return {
        "job_id": job_id, "status": "pending", "total": len(docs),
        "message": f"Started for {len(docs)} doc(s). Poll /cma/extract/status/{job_id}",
    }


@router.get("/cma/extract/status/{job_id}")
def get_job_status(job_id: str):
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
        "raw":          job.get("raw", True),
    }


@router.get("/cma/extract/result/{job_id}")
def get_job_result(job_id: str):
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    if job.get("status") == "error":
        raise HTTPException(500, f"Extraction failed: {job.get('error')}")
    if job.get("status") in ("pending", "running"):
        total = job.get("total", 1) or 1
        return JSONResponse(status_code=202, content={
            "message": f"Still processing. Check /cma/extract/status/{job_id}",
            "status":  job.get("status"),
            "percent": round(job.get("progress", 0) / total * 100),
        })
    return JSONResponse(content=job["result"])


@router.get("/cma/extract/jobs")
def list_jobs():
    with _jobs_lock:
        return [
            {"job_id": jid, "company_slug": j.get("company_slug"),
             "status": j.get("status"), "progress": j.get("progress"),
             "total": j.get("total"), "started_at": j.get("started_at"),
             "finished_at": j.get("finished_at")}
            for jid, j in _jobs.items()
        ]


@router.get(
    "/cma/extract/stored/{company_slug}",
    responses={
        200: {"description": "Extraction started (or already running/done) in the background — see 'job_id' in the body"},
        404: {"description": "Company not found or no documents uploaded"},
    },
)
async def extract_company_sync(company_slug: str, background_tasks: BackgroundTasks, raw: bool = False):
    """
    Starts (or reuses) a background extraction job instead of running the
    full pipeline inline — see download_cma_excel's docstring for why: a
    long synchronous multi-document run here blocks the whole single-process
    server for every other client, which surfaces as a misleading network
    error rather than a clean timeout.

    Poll GET /cma/extract/status/{job_id}, then GET /cma/extract/result/{job_id}.
    """
    result = await trigger_extraction(company_slug, background_tasks, raw)
    result["result_hint"] = f"Once status is 'done', GET /cma/extract/result/{result['job_id']} for the JSON."
    return result


@router.get("/cma/extract/stored/{company_slug}/{doc_id}")
async def extract_single_doc(company_slug: str, doc_id: str, raw: bool = False):
    path = get_document_path(company_slug, doc_id)
    if path is None:
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    try:
        res = await _run_pipeline(path, path.name, doc_id=doc_id, company_slug=company_slug)
        return JSONResponse(content={
            "current_fy":   res["current_fy"],
            "previous_fy":  res["previous_fy"],
            **res["extraction"],
        })
    except Exception as exc:
        logger.exception("Single doc extraction failed")
        raise HTTPException(500, str(exc))


# ── Excel Download Endpoints ──────────────────────────────────────────────────

@router.get(
    "/cma/extract/excel/{company_slug}",
    responses={
        200: {"description": "Extraction started (or already running/done) in the background — see 'job_id' in the body"},
        404: {"description": "Company or documents not found"},
    },
)
async def download_cma_excel(company_slug: str, background_tasks: BackgroundTasks, raw: bool = False):
    """
    Starts (or reuses) a background extraction job for this company instead
    of running the OCR+LLM pipeline inline.

    This used to run the full pipeline for every document synchronously
    inside the request/response cycle, which can take many minutes for
    multi-document companies (real-world observed: single documents taking
    20-60+ minutes under OpenAI rate-limit/network conditions). On a
    single-process dev server that blocks EVERY other request — including
    /health — for the whole duration, and a browser/Swagger UI waiting on a
    fetch that never resolves in time reports an opaque "Failed to fetch" /
    CORS-looking error, not a clean timeout. That's almost certainly what
    was happening — not an actual CORS problem.

    Poll GET /cma/extract/status/{job_id} until status is "done", then call
    GET /cma/extract/excel/job/{job_id} to get the file — that download is
    instant since it reuses the completed job's cached result.
    """
    result = await trigger_extraction(company_slug, background_tasks, raw)
    result["excel_download_hint"] = (
        f"Once status is 'done', GET /cma/extract/excel/job/{result['job_id']} to download the file."
    )
    return result


@router.get(
    "/cma/extract/excel/job/{job_id}",
    response_class=Response,
    responses={
        200: {
            "description": "CMA Excel file (.xlsx)",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}
            },
        },
        202: {"description": "Job still running — poll /cma/extract/status/{job_id}"},
        404: {"description": "Job not found"},
        500: {"description": "Job failed or Excel generation error"},
    },
)
def download_cma_excel_from_job(job_id: str, raw: Optional[bool] = None):
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    if job.get("status") in ("pending", "running"):
        return JSONResponse(
            status_code=202,
            content={
                "message": f"Job still running ({job.get('status')}). Poll /cma/extract/status/{job_id} first.",
                "status": job.get("status"),
            },
        )
    if job.get("status") == "error":
        raise HTTPException(500, f"Job failed: {job.get('error')}")

    merged = job["result"]
    slug   = job.get("company_slug", "company")

    try:
        xlsx_bytes = generate_cma_excel(merged)
    except Exception as exc:
        logger.exception("Excel generation failed")
        raise HTTPException(500, f"Excel generation failed: {exc}")

    suffix = "_CMA.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{slug}{suffix}"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.post("/cma/inject/streamlined")
async def inject_streamlined_cma(
    file: UploadFile = File(...),
    target_year: str = Form("2024-25"),
    apply_overrides: bool = Form(True)
):
    """
    Streamlined endpoint: upload PDF financial statement, run extraction,
    and automatically inject data into the master CMA Excel sheet.
    """
    import tempfile
    import os
    from app.services.docling_service import DoclingService
    from app.services.cma_extraction_service import extract_cma_fields
    from app.utils.fy_detector import get_financial_year
    from app.services.cma_excel_injector import inject_cma_data_from_json
    from scripts.streamlined_pipeline import CARGOSOL_FY25_OVERRIDES
    
    # Save the uploaded file to a temporary location
    temp_dir = tempfile.gettempdir()
    pdf_path = Path(temp_dir) / f"{uuid.uuid4().hex}_{file.filename}"
    
    try:
        content = await file.read()
        pdf_path.write_bytes(content)
        
        # Load the default master Excel workbook
        master_excel = Path("Input_Data/CARGOSOL LOGISTICS LTD CMA.xlsx")
        if not master_excel.exists():
            raise HTTPException(404, f"Master Excel workbook not found at {master_excel.absolute()}")
            
        logger.info(f"Running OCR on uploaded file {file.filename}...")
        docling = DoclingService()
        pages = await docling.convert_to_pages(pdf_path)
        
        # Detect financial year
        logger.info("Detecting Financial Year...")
        current_fy, previous_fy = get_financial_year(pdf_path, pages)
        if target_year:
            current_fy = target_year
            
        logger.info(f"Extracting CMA fields for target year {current_fy}...")
        extracted_data = await extract_cma_fields(
            pages=pages,
            source_file=file.filename,
            doc_id=pdf_path.stem,
            current_fy=current_fy,
            previous_fy=previous_fy
        )
        
        # Inject the values
        overrides = CARGOSOL_FY25_OVERRIDES if apply_overrides else None
        payload = {
            "company_name": "Cargosol Logistics Limited",
            "cma_data": extracted_data.get("sections", {})
        }
        
        updated_excel_path = inject_cma_data_from_json(
            excel_path=master_excel,
            json_data=payload,
            target_year=current_fy,
            override_vals=overrides
        )
        
        # Read the updated Excel file bytes to return as download
        xlsx_bytes = updated_excel_path.read_bytes()
        
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="CARGOSOL_LOGISTICS_LTD_CMA_{current_fy}.xlsx"',
                "Content-Length": str(len(xlsx_bytes)),
            },
        )
        
    except Exception as exc:
        logger.exception("Streamlined injection failed")
        raise HTTPException(500, f"Streamlined injection failed: {exc}")
        
    finally:
        # Cleanup temp file
        if pdf_path.exists():
            try:
                os.remove(pdf_path)
            except Exception:
                pass

