from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.exceptions import UnsupportedFormatError

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".zip"}


def ensure_supported(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"Unsupported file format: {suffix}")
    return suffix


async def save_upload(upload_file: UploadFile, upload_dir: Path) -> Path:
    ensure_supported(upload_file.filename or "")
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload_file.filename or "").suffix.lower()
    safe_path = upload_dir / f"{uuid4().hex}{suffix}"
    content = await upload_file.read()
    if not content:
        from app.exceptions import EmptyDocumentError

        raise EmptyDocumentError("Uploaded file is empty")
    safe_path.write_bytes(content)
    return safe_path
