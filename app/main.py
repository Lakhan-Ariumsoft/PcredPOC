"""
CMA Extraction API  v5.0

Endpoints
---------
GET  /health
GET  /fields

POST /documents/upload                      Upload PDF for a company
GET  /documents                             List all companies + docs
GET  /documents/{company_slug}              List docs for one company
DELETE /documents/{company_slug}/{doc_id}   Delete a doc

POST /extract/upload                        One-shot: upload → extract (not stored)
GET  /extract/stored/{company_slug}         Extract ALL docs → merged year-wise CMA table
GET  /extract/stored/{company_slug}/{doc_id} Extract single stored doc
DELETE /extract/cache/{doc_id}              Clear AI cache for a doc (force re-extraction)
"""
import logging, os, uuid
from pathlib import Path

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
    version="5.0.0",
)

# ── CORS — allow all origins (restrict in production to your FE domain) ───────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # e.g. ["https://yourapp.com"] in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path(os.environ.get("TEMP_DIR", "temp"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _require_pdf(file: UploadFile) -> None:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

def _read_bytes(file: UploadFile) -> bytes:
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    return data

def _run_pipeline(pdf_path: Path, filename: str, doc_id: str = "") -> dict:
    """Full pipeline: PDF → text → FY detection → OpenAI → structured result."""
    pages              = extract_all_text(pdf_path)
    primary_fy, sec_fy = get_financial_year(pdf_path, pages)
    logger.info(f"{filename}: FY={primary_fy} | {len(pages)} pages")
    extraction = extract_cma_fields(pages=pages, source_file=filename, doc_id=doc_id)
    return {"primary_fy": primary_fy, "secondary_fy": sec_fy, "extraction": extraction}


# ── system ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "5.0.0"}


@app.get("/fields", tags=["System"])
def list_fields():
    """All 199 CMA fields grouped by section."""
    return {k: {"label": v["label"], "fields": v["fields"]} for k, v in CMA_SECTIONS.items()}


# ── document management ───────────────────────────────────────────────────────

@app.post("/documents/upload", status_code=201, tags=["Documents"])
async def upload_document(
    company_name: str = Form(..., min_length=2, max_length=200,
                             description="e.g. 'Cargosol Logistics Limited'"),
    file: UploadFile = File(...),
):
    """
    Upload a PDF for a company.
    - Multiple companies and multiple PDFs per company supported.
    - Duplicate files (same content) rejected with 409.
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
    """List all companies and their uploaded documents."""
    return list_all_documents()


@app.get("/documents/{company_slug}", tags=["Documents"])
def get_company_documents(company_slug: str):
    """List documents for one company."""
    r = list_company_documents(company_slug)
    if r is None:
        raise HTTPException(404, f"Company '{company_slug}' not found.")
    return r


@app.delete("/documents/{company_slug}/{doc_id}", tags=["Documents"])
def remove_document(company_slug: str, doc_id: str):
    """Delete a stored document and its AI cache."""
    if not delete_document(company_slug, doc_id):
        raise HTTPException(404, f"Document '{doc_id}' not found.")
    clear_ai_cache(doc_id)
    return {"message": f"Document '{doc_id}' deleted."}


# ── extraction ────────────────────────────────────────────────────────────────

@app.delete("/extract/cache/{doc_id}", tags=["Extraction"])
def clear_cache(doc_id: str):
    """
    Clear AI extraction cache for a document.
    Next extraction call will re-run OpenAI instead of returning cached result.
    """
    cleared = clear_ai_cache(doc_id)
    return {"cleared": cleared, "doc_id": doc_id}


@app.post("/extract/upload", tags=["Extraction"])
async def extract_from_upload(file: UploadFile = File(...)):
    """One-shot: upload PDF → get full CMA JSON. File is NOT stored."""
    _require_pdf(file)
    data     = _read_bytes(file)
    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}_{file.filename}"
    try:
        tmp_path.write_bytes(data)
        res = _run_pipeline(tmp_path, file.filename)
        return JSONResponse(content={
            "primary_fy":   res["primary_fy"],
            "secondary_fy": res["secondary_fy"],
            **res["extraction"],
        })
    except Exception as e:
        logger.exception("One-shot extraction failed")
        raise HTTPException(500, str(e))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@app.get("/extract/stored/{company_slug}", tags=["Extraction"])
def extract_company_all_years(company_slug: str):
    """
    Main endpoint for the CMA Mapping UI.

    Processes ALL uploaded documents for a company:
    1. Extracts text from each PDF (OCR-cached after first run)
    2. Detects financial year per document
    3. Runs OpenAI extraction for all 199 CMA fields (AI-cached after first run)
    4. Merges into one year-wise table

    Response shape:
    {
      "company_slug": "...",
      "company_name": "...",
      "financial_years": ["2021-22", "2022-23", "2023-24"],
      "documents": [
        {
          "doc_id": "...", "filename": "...",
          "primary_fy": "2022-23", "secondary_fy": "2021-22",
          "status": "success", "fields_found": 142
        }
      ],
      "cma_data": {
        "sales": {
          "label": "Sales",
          "fields": {
            "Net Sales": {
              "2022-23": {
                "value": 17012.85,
                "confidence": 0.98,
                "evidence": "Revenue from Operations 17,012.85",
                "page": 15,
                "source_doc_id": "...",
                "source_filename": "CLL_FY_2022-23.pdf",
                "is_primary": true
              },
              "2021-22": { ... }
            }
          }
        }
      },
      "warnings": []
    }
    """
    info = list_company_documents(company_slug)
    if info is None:
        raise HTTPException(404, f"Company '{company_slug}' not found. Upload documents first.")

    docs = info.get("documents", [])
    if not docs:
        raise HTTPException(404, f"No documents uploaded for '{company_slug}'.")

    doc_results = []
    for doc in docs:
        doc_id   = doc["doc_id"]
        filename = doc["filename"]
        path     = get_document_path(company_slug, doc_id)

        if path is None:
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": "error_file_missing",
                "primary_fy": None, "secondary_fy": None, "extraction": None,
            })
            continue

        logger.info(f"Processing {company_slug}/{doc_id} — {filename}")
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
        except Exception as e:
            logger.exception(f"Failed: {filename}")
            doc_results.append({
                "doc_id": doc_id, "filename": filename,
                "status": f"error: {e}",
                "primary_fy": None, "secondary_fy": None, "extraction": None,
            })

    merged = merge_documents(
        company_slug = company_slug,
        company_name = info["display_name"],
        doc_results  = doc_results,
    )
    return JSONResponse(content=merged)


@app.get("/extract/stored/{company_slug}/{doc_id}", tags=["Extraction"])
def extract_single_doc(company_slug: str, doc_id: str):
    """Extract CMA fields from one stored document."""
    path = get_document_path(company_slug, doc_id)
    if path is None:
        raise HTTPException(404, f"Document '{doc_id}' not found for '{company_slug}'.")
    try:
        res = _run_pipeline(path, path.name, doc_id=doc_id)
        return JSONResponse(content={
            "primary_fy":   res["primary_fy"],
            "secondary_fy": res["secondary_fy"],
            **res["extraction"],
        })
    except Exception as e:
        logger.exception("Single doc extraction failed")
        raise HTTPException(500, str(e))