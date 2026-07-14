import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core_config import get_settings
from app.exceptions import DocumentProcessingError, UnsupportedFormatError
from app.services.job_store import job_store
from app.services.storage import DuplicateDocumentError, store_document
from app.utils.files import SUPPORTED_EXTENSIONS, save_upload

router = APIRouter(tags=["upload"])


@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    company_name: Optional[str] = Form(None),
) -> dict:
    """
    Upload one or more documents. If company_name is provided the files are
    stored under uploads/{company_slug}/ and the job carries company_slug /
    doc_id so extraction results can be fetched by company later.
    """
    settings = get_settings()
    upload_dir = settings.output_dir / "uploads"
    jobs = []
    try:
        for upload_file in files:
            if company_name:
                content = await upload_file.read()
                if not content:
                    from app.exceptions import EmptyDocumentError
                    raise EmptyDocumentError("Uploaded file is empty")
                try:
                    meta = store_document(company_name, upload_file.filename or "", content)
                except DuplicateDocumentError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                source_path = Path(meta["path"])
                jobs.append(
                    job_store.create(
                        source_path,
                        company_slug=meta["company_slug"],
                        doc_id=meta["doc_id"],
                    )
                )
            else:
                saved = await save_upload(upload_file, upload_dir)
                if saved.suffix.lower() == ".zip":
                    jobs.extend(_create_jobs_from_zip(saved, upload_dir))
                else:
                    jobs.append(job_store.create(saved))
    except (DocumentProcessingError, HTTPException):
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"jobs": [job.model_dump(mode="json") for job in jobs]}


def _create_jobs_from_zip(zip_path: Path, upload_dir: Path) -> list:
    jobs = []
    extract_dir = upload_dir / zip_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            suffix = Path(member.filename).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS or suffix == ".zip":
                continue
            target = extract_dir / Path(member.filename).name
            target.write_bytes(archive.read(member))
            if target.stat().st_size == 0:
                continue
            jobs.append(job_store.create(target))
    if not jobs:
        raise UnsupportedFormatError("ZIP did not contain supported documents")
    return jobs
