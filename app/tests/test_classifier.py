import pytest

from app.schemas.common import DocumentType
from app.services.classifier import DocumentClassifier


@pytest.mark.asyncio
async def test_classifier_uses_invoice_heuristics() -> None:
    classifier = DocumentClassifier()
    result = await classifier.classify("Tax Invoice\nGSTIN 27ABCDE1234F1Z5\nCGST\nSGST\nInvoice No INV-1")
    assert result.document_type == DocumentType.invoice
    assert result.source == "heuristic"
