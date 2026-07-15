import abc
from pathlib import Path

from app.services.lm_studio_client import LMStudioClient


class OcrAdapter(abc.ABC):
    """Abstract base class for OCR/document conversion adapters."""

    @abc.abstractmethod
    async def convert(self, file_path: Path, mime_type: str) -> str:
        """Convert a document image or PDF into raw text/markdown/HTML representation.

        Args:
            file_path: Absolute path to the source file.
            mime_type: MIME type of the file.

        Returns:
            The structured markdown or text conversion.
        """
        pass


class LMStudioDoclingAdapter(OcrAdapter):
    """Adapter for Granite Docling 258M hosted on LM Studio."""

    def __init__(self, client: LMStudioClient) -> None:
        self.client = client

    async def convert(self, file_path: Path, mime_type: str) -> str:
        prompt = (
            "Convert this financial document into clean markdown. Preserve headings, tables, "
            "key-value pairs, line items, totals, dates, account numbers, and currency symbols. "
            "Do not extract or infer financial fields; only represent the document content."
        )
        return await self.client.vision_chat(prompt=prompt, file_path=file_path, mime_type=mime_type)
