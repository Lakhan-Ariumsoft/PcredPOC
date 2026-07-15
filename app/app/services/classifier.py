from __future__ import annotations

from dataclasses import dataclass

from app.core_config import get_settings
from app.schemas.common import DocumentType
from app.services.client_factory import get_llm_adapter
from app.utils.retry import retry_async


@dataclass(frozen=True)
class ClassificationResult:
    document_type: DocumentType
    confidence: float
    source: str


class DocumentClassifier:
    KEYWORDS: dict[DocumentType, tuple[str, ...]] = {
        DocumentType.invoice: ("invoice", "tax invoice", "gstin", "cgst", "sgst", "igst", "invoice no"),
        DocumentType.receipt: ("receipt", "paid", "payment received", "merchant", "cash memo"),
        DocumentType.bank_statement: ("bank statement", "account number", "transaction", "debit", "credit", "balance"),
        DocumentType.purchase_order: ("purchase order", "po number", "buyer", "ship to", "bill to"),
        DocumentType.financial_report: ("profit and loss", "balance sheet", "financial report", "revenue", "assets"),
    }

    def __init__(self) -> None:
        self.adapter = get_llm_adapter()
        self.settings = get_settings()

    async def classify(self, markdown: str) -> ClassificationResult:
        lowered = markdown.lower()
        scores = {
            doc_type: sum(1 for keyword in keywords if keyword in lowered)
            for doc_type, keywords in self.KEYWORDS.items()
        }
        best_type, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score >= 2:
            return ClassificationResult(best_type, min(0.95, 0.55 + best_score * 0.1), "heuristic")
        return await self._classify_with_llm(markdown)

    async def _classify_with_llm(self, markdown: str) -> ClassificationResult:
        prompt = (
            "Classify this document as exactly one of: invoice, receipt, bank_statement, "
            "purchase_order, financial_report, unknown. Return only the label.\n\n"
            f"{markdown[:6000]}"
        )

        async def operation() -> str:
            return await self.adapter.chat([{"role": "user", "content": prompt}])

        raw = (await retry_async(operation, attempts=self.settings.max_retries)).strip().lower()
        try:
            doc_type = DocumentType(raw)
        except ValueError:
            doc_type = DocumentType.unknown
        return ClassificationResult(doc_type, 0.6 if doc_type != DocumentType.unknown else 0.2, "gemma")

