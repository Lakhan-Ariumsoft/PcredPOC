import asyncio
import mimetypes
from pathlib import Path

from app.core_config import get_settings
from app.exceptions import EmptyDocumentError, OcrFailureError, UnsupportedFormatError
from app.services.client_factory import get_ocr_adapter
from app.utils.files import ensure_supported
from app.utils.retry import retry_async

PAGE_OCR_CONCURRENCY = 6
PAGE_OCR_RETRY_ATTEMPTS = 3
PAGE_OCR_RETRY_DELAY_SECONDS = 2.0


class DoclingService:
    def __init__(self) -> None:
        self.adapter = get_ocr_adapter()
        self.settings = get_settings()

    async def convert_to_markdown(self, file_path: Path) -> str:
        suffix = ensure_supported(file_path.name)
        if suffix == ".zip":
            raise UnsupportedFormatError("ZIP files must be expanded before OCR")
        if not file_path.exists() or file_path.stat().st_size == 0:
            raise EmptyDocumentError("Document is empty")

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        async def operation() -> str:
            return await self.adapter.convert(file_path, mime_type)

        markdown = await retry_async(operation, attempts=self.settings.max_retries)
        if not markdown.strip():
            raise OcrFailureError("Docling returned empty output")
        return markdown

    async def convert_to_pages(self, file_path: Path) -> list[dict]:
        """OCR the document, splitting PDFs page-by-page.
        Returns [{"page": page_num, "text": markdown_content}]"""
        import fitz
        import tempfile
        import hashlib
        import json
        from app.utils.files import ensure_supported

        suffix = ensure_supported(file_path.name)
        if suffix == ".zip":
            raise UnsupportedFormatError("ZIP files must be expanded before OCR")
        if not file_path.exists() or file_path.stat().st_size == 0:
            raise EmptyDocumentError("Document is empty")

        # Caching logic
        cache_dir = self.settings.output_dir / "uploads" / ".ocr_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Calculate SHA256 of the document file
        h = hashlib.sha256()
        h.update(str(file_path.stat().st_size).encode())
        h.update(self.settings.docling_model.encode())
        h.update(f"ocr_start_page={self.settings.ocr_start_page}".encode())
        with open(file_path, "rb") as f:
            h.update(f.read(65536))
        cache_file = cache_dir / f"{h.hexdigest()[:32]}.json"
        
        if cache_file.exists():
            try:
                cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                return cached_data
            except Exception:
                pass

        if suffix != ".pdf":
            # Single-page image file
            text = await self.convert_to_markdown(file_path)
            pages = [{"page": 1, "text": text}]
        else:
            # PDF file — split page-by-page, OCR pages concurrently (bounded) so
            # vLLM's continuous batching can pipeline requests instead of one-at-a-time.
            with fitz.open(file_path) as doc:
                num_pages = len(doc)
            start_idx = self.settings.ocr_start_page - 1
            if start_idx >= num_pages:
                raise OcrFailureError(
                    f"OCR_START_PAGE={self.settings.ocr_start_page} is beyond document length ({num_pages} pages)"
                )

            semaphore = asyncio.Semaphore(PAGE_OCR_CONCURRENCY)

            async def ocr_page(idx: int) -> dict:
                page_num = idx + 1
                with fitz.open(file_path) as doc:
                    temp_pdf = fitz.open()
                    temp_pdf.insert_pdf(doc, from_page=idx, to_page=idx)

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    temp_path = Path(tmp.name)

                try:
                    temp_pdf.save(str(temp_path))
                    temp_pdf.close()

                    async def page_operation() -> str:
                        return await self.adapter.convert(temp_path, "application/pdf")

                    async with semaphore:
                        # Longer backoff than the default — rate-limit windows take
                        # many seconds to clear, not the ~1.5s the default schedule allows.
                        text = await retry_async(
                            page_operation,
                            attempts=PAGE_OCR_RETRY_ATTEMPTS,
                            delay_seconds=PAGE_OCR_RETRY_DELAY_SECONDS,
                        )
                    return {"page": page_num, "text": text}
                finally:
                    if temp_path.exists():
                        temp_path.unlink()

            # return_exceptions=True is required: without it, gather() propagates the
            # first failure immediately while leaving the other in-flight page tasks
            # running orphaned in the background — they keep retrying against the API
            # uncontrolled, which silently exhausts rate limits for everything after.
            results = await asyncio.gather(
                *[ocr_page(idx) for idx in range(start_idx, num_pages)],
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                raise errors[0]
            pages = sorted(results, key=lambda p: p["page"])

        try:
            cache_file.write_text(json.dumps(pages), encoding="utf-8")
        except Exception:
            pass

        return pages

    def clear_all_ocr_caches(self) -> int:
        cache_dir = self.settings.output_dir / "uploads" / ".ocr_cache"
        count = 0
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        return count
