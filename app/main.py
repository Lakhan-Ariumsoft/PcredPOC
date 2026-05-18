"""
FastAPI — CMA Document Management + Multi-Year Extraction API.

Endpoints
---------
System
  GET  /health
  GET  /fields

Document management
  POST   /documents/upload                       Upload PDF for a company
  GET    /documents                              List all companies + docs
  GET    /documents/{company_slug}               Docs for one company
  DELETE /documents/{company_slug}/{doc_id}      Delete a doc

Extraction
  POST /extract/upload
      One-shot: upload → extract (no storage).

  GET  /extract/stored/{company_slug}
      Extract & MERGE all documents of a company into one multi-year response.
      Automatically detects which FY each document covers and merges results.

  GET  /extract/stored/{company_slug}/{doc_id}
      Extract CMA fields from a single stored document.
"""

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.constants import CMA_FIELDS, SECTION_LABELS
from app.extractor import detect_financial_year, extract_cma_data
from app.storage   import (
    DuplicateDocumentError,
    delete_document,
    get_document_path,
    list_all_documents,
    list_company_documents,
    store_document,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CMA Extraction API",
    description=(
        "Upload financial PDFs per company and extract multi-year CMA data. "
        "Supports 3-4 financial years per company."
    ),
    version="3.0.0",
)

import os as _os
TEMP_DIR = Path(_os.environ.get("TEMP_DIR", "temp"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ── Guards ────────────────────────────────────────────────────────────────────

def _require_pdf(file: UploadFile) -> None:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")


def _read_bytes(file: UploadFile) -> bytes:
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return content


# ── Multi-year merge logic ────────────────────────────────────────────────────

def _build_year_entry(yv: dict, doc_id: str, filename: str, is_primary: bool) -> dict:
    """Attach source-document info to a year-value dict."""
    return {
        "value":           yv["value"],
        "confidence":      yv["confidence"],
        "source_doc_id":   doc_id,
        "source_filename": filename,
        "source_page":     yv["source_page"],
        "matched_text":    yv["matched_text"],
        "is_primary":      is_primary,   # True = this doc's CY; False = this doc's PY
    }


def _merge_into_company_result(
    company_result: dict,
    single_doc: dict,
    doc_id: str,
    filename: str,
) -> None:
    """
    Merge a single-document extraction result into the accumulating
    company_result dict.

    Priority rule for a given (field, year):
      - Primary wins over secondary  (CY data beats PY data from another doc)
      - Among equals, higher confidence wins
    """
    current_fy  = single_doc["meta"]["current_fy"]
    previous_fy = single_doc["meta"]["previous_fy"]

    fields_acc = company_result["fields"]
    tp_acc     = company_result["trade_payables_detail"]

    # ── Merge regular fields ──────────────────────────────────────────────────
    for field_key, field_data in single_doc["fields"].items():
        if field_key not in fields_acc:
            fields_acc[field_key] = {
                "label":        field_data["label"],
                "section":      field_data["section"],
                "section_label": field_data["section_label"],
                "years":        {},
            }

        years_acc = fields_acc[field_key]["years"]

        def _try_set(fy: str, yv: dict, is_primary: bool):
            if yv["value"] is None:
                return
            entry = _build_year_entry(yv, doc_id, filename, is_primary)
            existing = years_acc.get(fy)
            if existing is None:
                years_acc[fy] = entry
            else:
                # Primary always beats secondary
                if is_primary and not existing["is_primary"]:
                    years_acc[fy] = entry
                # Same priority → higher confidence wins
                elif is_primary == existing["is_primary"]:
                    if (entry["confidence"] or 0) > (existing["confidence"] or 0):
                        years_acc[fy] = entry

        _try_set(current_fy,  field_data["current_year"],  is_primary=True)
        _try_set(previous_fy, field_data["previous_year"], is_primary=False)

    # ── Merge trade payables detail ───────────────────────────────────────────
    tp_src = single_doc.get("trade_payables_detail") or {}
    for sub_key in ("msme", "others", "total"):
        if sub_key not in tp_acc:
            tp_acc[sub_key] = {}
        for fy, yv in (tp_src.get(sub_key) or {}).items():
            if yv.get("value") is None:
                continue
            is_primary = (fy == current_fy)
            entry = _build_year_entry(yv, doc_id, filename, is_primary)
            existing = tp_acc[sub_key].get(fy)
            if existing is None:
                tp_acc[sub_key][fy] = entry
            elif is_primary and not existing["is_primary"]:
                tp_acc[sub_key][fy] = entry


def _sort_financial_years(years: list[str]) -> list[str]:
    """Sort FY strings chronologically: '2021-22' < '2022-23'."""
    def _key(fy):
        try:
            return int(fy.split("-")[0])
        except (ValueError, IndexError):
            return 0
    return sorted(set(years), key=_key)


# ── System endpoints ──────────────────────────────────────────────────────────

@app.get("/")
async def home():
    return {"message": "API running successfully"}

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/fields", tags=["System"])
def list_fields():
    """All expected CMA fields grouped by section."""
    grouped: dict = {}
    for key, meta in CMA_FIELDS.items():
        sec = meta["section"]
        grouped.setdefault(sec, {"label": SECTION_LABELS[sec], "fields": []})
        grouped[sec]["fields"].append({"key": key, "label": meta["label"]})
    return grouped


# ── Document management ───────────────────────────────────────────────────────

@app.post("/documents/upload", status_code=201, tags=["Documents"])
async def upload_document(
    company_name: str = Form(
        ..., min_length=2, max_length=200,
        description="Full company name, e.g. 'Cargosol Logistics Limited'",
    ),
    file: UploadFile = File(...),
):
    """
    Upload a PDF for a company.

    - Multiple companies are supported (stored in separate folders).
    - Multiple PDFs per company are supported (one per financial year recommended).
    - Duplicate files (same SHA-256) within a company are rejected with HTTP 409.

    Returns `doc_id` for use with `/extract/stored/{company_slug}/{doc_id}`.
    """
    _require_pdf(file)
    content = _read_bytes(file)
    try:
        meta = store_document(company_name, file.filename, content)
    except DuplicateDocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Storage failed")
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(f"Stored: {meta['company_slug']}/{meta['doc_id']} — {meta['filename']}")
    return JSONResponse(status_code=201, content={
        "message":      "Document uploaded successfully.",
        "doc_id":       meta["doc_id"],
        "company_slug": meta["company_slug"],
        "company_name": meta["company_name"],
        "filename":     meta["filename"],
        "size_bytes":   meta["size_bytes"],
        "uploaded_at":  meta["uploaded_at"],
    })


@app.get("/documents", tags=["Documents"])
def get_all_documents():
    """List every company and all their uploaded documents."""
    return list_all_documents()


@app.get("/documents/{company_slug}", tags=["Documents"])
def get_company_documents(company_slug: str):
    """List documents for one company."""
    result = list_company_documents(company_slug)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Company '{company_slug}' not found.")
    return result


@app.delete("/documents/{company_slug}/{doc_id}", tags=["Documents"])
def remove_document(company_slug: str, doc_id: str):
    """Permanently delete a stored document."""
    if not delete_document(company_slug, doc_id):
        raise HTTPException(
            status_code=404,
            detail=f"Document '{doc_id}' not found for company '{company_slug}'.",
        )
    return {"message": f"Document '{doc_id}' deleted."}


# ── Extraction endpoints ──────────────────────────────────────────────────────

@app.post("/extract/upload", tags=["Extraction"])
async def extract_from_upload(file: UploadFile = File(...)):
    """One-shot: upload PDF → get CMA JSON (file not stored)."""
    _require_pdf(file)
    content = _read_bytes(file)
    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}_{file.filename}"
    try:
        tmp_path.write_bytes(content)
        result = extract_cma_data(tmp_path, source_name=file.filename)
    except Exception as exc:
        logger.exception("Extraction failed")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return JSONResponse(content=result)


@app.get(
    "/extract/stored/{company_slug}",
    tags=["Extraction"],
    summary="Extract & merge CMA data from ALL documents of a company",
)
def extract_company_all_years(company_slug: str):
    """
    Trigger extraction for **every uploaded document** of a company and merge
    results into a single multi-year CMA table.

    Behaviour
    ---------
    - Automatically detects which financial year each document covers
      (e.g. 'March 31, 2023' → FY 2022-23).
    - For each CMA field, stores values keyed by financial year.
    - When the same year appears in two documents (once as CY, once as PY),
      the document where that year is the **current year** (primary) wins.
    - Among equal-priority sources, higher confidence wins.

    Response shape
    --------------
    {
      company_slug, company_name,
      financial_years: ["2021-22", "2022-23", ...],   ← sorted chronologically
      documents_processed: [{ doc_id, filename, primary_year,
                               secondary_year, status, warnings }],
      fields: {
        field_key: {
          label, section, section_label,
          years: {
            "2022-23": { value, confidence, source_doc_id,
                         source_filename, source_page, is_primary }
          }
        }
      },
      trade_payables_detail: {
        msme/others/total: {
          "2022-23": { value, confidence, source_doc_id, ... }
        }
      },
      warnings: [str]
    }
    """
    company_info = list_company_documents(company_slug)
    if company_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Company '{company_slug}' not found. "
                   f"Upload documents first via POST /documents/upload.",
        )

    documents = company_info["documents"]
    if not documents:
        raise HTTPException(
            status_code=404,
            detail=f"No documents found for company '{company_slug}'.",
        )

    # ── Accumulator ───────────────────────────────────────────────────────────
    company_result = {
        "company_slug":          company_slug,
        "company_name":          company_info["display_name"],
        "financial_years":       [],
        "documents_processed":   [],
        "fields":                {},
        "trade_payables_detail": {"msme": {}, "others": {}, "total": {}},
        "warnings":              [],
    }

    all_fys: list[str] = []

    for doc in documents:
        doc_id   = doc["doc_id"]
        filename = doc["filename"]
        path     = get_document_path(company_slug, doc_id)

        if path is None:
            company_result["warnings"].append(
                f"File not found on disk for doc_id={doc_id} ({filename}). Skipped."
            )
            company_result["documents_processed"].append({
                "doc_id":         doc_id,
                "filename":       filename,
                "primary_year":   None,
                "secondary_year": None,
                "status":         "error",
                "warnings":       ["File missing from disk."],
            })
            continue

        logger.info(f"Processing {company_slug}/{doc_id} — {filename}")
        try:
            single = extract_cma_data(path, source_name=filename, doc_id=doc_id)
        except Exception as exc:
            logger.exception(f"Extraction failed for {filename}")
            company_result["warnings"].append(f"Extraction failed for {filename}: {exc}")
            company_result["documents_processed"].append({
                "doc_id":         doc_id,
                "filename":       filename,
                "primary_year":   None,
                "secondary_year": None,
                "status":         "error",
                "warnings":       [str(exc)],
            })
            continue

        primary_fy   = single["meta"]["current_fy"]
        secondary_fy = single["meta"]["previous_fy"]

        _merge_into_company_result(company_result, single, doc_id, filename)

        all_fys.extend([primary_fy, secondary_fy])
        company_result["documents_processed"].append({
            "doc_id":             doc_id,
            "filename":           filename,
            "primary_year":       primary_fy,
            "secondary_year":     secondary_fy,
            "balance_sheet_page": single["meta"]["balance_sheet_page"],
            "status":             "success",
            "warnings":           single.get("warnings", []),
        })

        if single.get("warnings"):
            company_result["warnings"].extend(
                [f"[{filename}] {w}" for w in single["warnings"]]
            )

    company_result["financial_years"] = _sort_financial_years(
        [fy for fy in all_fys if fy != "unknown"]
    )

    return JSONResponse(content=company_result)


@app.get(
    "/extract/stored/{company_slug}/{doc_id}",
    tags=["Extraction"],
    summary="Extract CMA fields from one stored document",
)
def extract_single_stored(company_slug: str, doc_id: str):
    """
    Extract CMA fields from a single previously-uploaded document.

    Detects the financial year automatically and returns current-year
    and previous-year values alongside full source evidence.
    """
    path = get_document_path(company_slug, doc_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"Document '{doc_id}' not found for company '{company_slug}'.",
        )
    try:
        result = extract_cma_data(path, source_name=path.name, doc_id=doc_id)
    except Exception as exc:
        logger.exception("Extraction failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content=result)
