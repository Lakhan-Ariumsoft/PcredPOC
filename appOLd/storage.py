"""
Document storage layer.

Folder layout
-------------
uploads/
├── registry.json                   ← global index (all companies + docs)
├── {company_slug}/
│   ├── {doc_id}_{original_name}.pdf
│   └── ...

Registry schema (registry.json)
--------------------------------
{
  "companies": {
    "{slug}": {
      "slug":         "cargosol-logistics-limited",
      "display_name": "Cargosol Logistics Limited",
      "created_at":   "2024-01-01T10:00:00",
      "documents": [
        {
          "doc_id":      "uuid4-hex",
          "filename":    "CLL_FY_2022-23.pdf",
          "stored_name": "uuid4hex_CLL_FY_2022-23.pdf",
          "size_bytes":  102400,
          "sha256":      "abc123...",
          "uploaded_at": "2024-01-01T10:00:00"
        }
      ]
    }
  }
}

Design choices
--------------
- No database: a single JSON file + per-write file-lock keeps things
  simple, fast, and portable.
- Deduplication: SHA-256 per company — same file won't be stored twice
  for the same company.  Same file for a different company is allowed.
- Concurrency: filelock prevents simultaneous registry corruption.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock

# ── Paths ──────────────────────────────────────────────────────────────────────
# Allow override via environment variable so Docker volumes work transparently.
# Dockerfile sets UPLOADS_ROOT=/data/uploads; locally it stays "uploads/".
import os as _os
UPLOADS_ROOT  = Path(_os.environ.get("UPLOADS_ROOT", "uploads"))
REGISTRY_FILE = UPLOADS_ROOT / "registry.json"
LOCK_FILE     = UPLOADS_ROOT / "registry.lock"

UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    """'Cargosol Logistics Limited' → 'cargosol-logistics-limited'"""
    name = name.strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)          # remove special chars
    name = re.sub(r"[\s_]+", "-", name)            # spaces/underscores → dash
    name = re.sub(r"-+", "-", name).strip("-")     # collapse multiple dashes
    return name or "unknown-company"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Registry read / write ──────────────────────────────────────────────────────

def _read_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"companies": {}}


def _write_registry(data: dict) -> None:
    REGISTRY_FILE.write_text(json.dumps(data, indent=2))


# ── Public API ─────────────────────────────────────────────────────────────────

class DuplicateDocumentError(ValueError):
    """Raised when the same file (by hash) is already stored for this company."""


def store_document(
    company_name: str,
    filename: str,
    content: bytes,
) -> dict:
    """
    Persist *content* to disk and register it under *company_name*.

    Returns the document metadata dict on success.
    Raises DuplicateDocumentError if the identical file already exists
    for this company.
    """
    slug     = _slugify(company_name)
    sha256   = _sha256(content)
    doc_id   = uuid.uuid4().hex

    company_dir = UPLOADS_ROOT / slug
    company_dir.mkdir(exist_ok=True)

    safe_filename = re.sub(r"[^\w.\-]", "_", filename)
    stored_name   = f"{doc_id}_{safe_filename}"
    stored_path   = company_dir / stored_name

    with FileLock(str(LOCK_FILE)):
        registry = _read_registry()
        companies = registry.setdefault("companies", {})

        # Initialise company entry if first upload
        if slug not in companies:
            companies[slug] = {
                "slug":         slug,
                "display_name": company_name.strip(),
                "created_at":   _now(),
                "documents":    [],
            }

        existing_docs = companies[slug]["documents"]

        # Deduplication check within this company
        for doc in existing_docs:
            if doc["sha256"] == sha256:
                raise DuplicateDocumentError(
                    f"File '{filename}' is identical to already-uploaded "
                    f"'{doc['filename']}' (uploaded {doc['uploaded_at']})."
                )

        # Write file to disk
        stored_path.write_bytes(content)

        meta = {
            "doc_id":      doc_id,
            "filename":    filename,
            "stored_name": stored_name,
            "size_bytes":  len(content),
            "sha256":      sha256,
            "uploaded_at": _now(),
        }
        existing_docs.append(meta)
        _write_registry(registry)

    return {
        "doc_id":       doc_id,
        "company_slug": slug,
        "company_name": companies[slug]["display_name"],
        "filename":     filename,
        "stored_name":  stored_name,
        "size_bytes":   len(content),
        "uploaded_at":  meta["uploaded_at"],
        "path":         str(stored_path),
    }


def get_document_path(company_slug: str, doc_id: str) -> Optional[Path]:
    """Return the absolute path for a stored document, or None if not found."""
    with FileLock(str(LOCK_FILE)):
        registry = _read_registry()
        company  = registry["companies"].get(company_slug)
        if not company:
            return None
        for doc in company["documents"]:
            if doc["doc_id"] == doc_id:
                path = UPLOADS_ROOT / company_slug / doc["stored_name"]
                return path if path.exists() else None
    return None


def list_all_documents() -> dict:
    """
    Return all companies and their documents.

    Response shape
    --------------
    {
      "total_companies": int,
      "total_documents": int,
      "companies": [
        {
          "slug": str,
          "display_name": str,
          "created_at": str,
          "document_count": int,
          "documents": [ { doc fields … } ]
        }
      ]
    }
    """
    with FileLock(str(LOCK_FILE)):
        registry = _read_registry()

    companies_raw = registry.get("companies", {})
    companies_out = []
    total_docs    = 0

    for slug, company in sorted(companies_raw.items()):
        docs = company.get("documents", [])
        total_docs += len(docs)
        companies_out.append({
            "slug":           slug,
            "display_name":   company["display_name"],
            "created_at":     company["created_at"],
            "document_count": len(docs),
            "documents": [
                {
                    "doc_id":      d["doc_id"],
                    "filename":    d["filename"],
                    "size_bytes":  d["size_bytes"],
                    "uploaded_at": d["uploaded_at"],
                }
                for d in docs
            ],
        })

    return {
        "total_companies": len(companies_out),
        "total_documents": total_docs,
        "companies":       companies_out,
    }


def list_company_documents(company_slug: str) -> Optional[dict]:
    """
    Return documents for a single company, or None if slug not found.
    """
    with FileLock(str(LOCK_FILE)):
        registry = _read_registry()
        company  = registry["companies"].get(company_slug)
        if not company:
            return None

    docs = company.get("documents", [])
    return {
        "slug":           company["slug"],
        "display_name":   company["display_name"],
        "created_at":     company["created_at"],
        "document_count": len(docs),
        "documents": [
            {
                "doc_id":      d["doc_id"],
                "filename":    d["filename"],
                "size_bytes":  d["size_bytes"],
                "uploaded_at": d["uploaded_at"],
            }
            for d in docs
        ],
    }


def delete_document(company_slug: str, doc_id: str) -> bool:
    """
    Remove a document from the registry and disk.
    Returns True if deleted, False if not found.
    """
    with FileLock(str(LOCK_FILE)):
        registry  = _read_registry()
        company   = registry["companies"].get(company_slug)
        if not company:
            return False

        docs = company["documents"]
        target = next((d for d in docs if d["doc_id"] == doc_id), None)
        if not target:
            return False

        # Remove from disk
        stored_path = UPLOADS_ROOT / company_slug / target["stored_name"]
        if stored_path.exists():
            stored_path.unlink()

        # Remove from registry
        company["documents"] = [d for d in docs if d["doc_id"] != doc_id]

        # Remove company entry if no documents left
        if not company["documents"]:
            del registry["companies"][company_slug]
            company_dir = UPLOADS_ROOT / company_slug
            if company_dir.exists() and not any(company_dir.iterdir()):
                company_dir.rmdir()

        _write_registry(registry)
    return True