from pydantic import ValidationError

from app.core_config import get_settings
from app.exceptions import JsonParseFailureError
from app.schemas.bank_schema import BankStatementSchema
from app.schemas.common import DocumentType
from app.schemas.financial_report_schema import FinancialReportSchema
from app.schemas.invoice_schema import InvoiceSchema
from app.schemas.purchase_order_schema import PurchaseOrderSchema
from app.schemas.receipt_schema import ReceiptSchema
from app.services.client_factory import get_llm_adapter
from app.services.prompts import JSON_MODE_INSTRUCTIONS, SCHEMAS
from app.utils.json_tools import parse_json_object
from app.utils.retry import retry_async


SCHEMA_MODELS = {
    DocumentType.invoice: InvoiceSchema,
    DocumentType.receipt: ReceiptSchema,
    DocumentType.bank_statement: BankStatementSchema,
    DocumentType.purchase_order: PurchaseOrderSchema,
    DocumentType.financial_report: FinancialReportSchema,
}


class FinancialExtractionService:
    def __init__(self) -> None:
        self.adapter = get_llm_adapter()
        self.settings = get_settings()

    async def extract(self, markdown_document: str, document_type: DocumentType) -> dict:
        schema = SCHEMAS.get(document_type, SCHEMAS[DocumentType.unknown])
        prompt = (
            f"{JSON_MODE_INSTRUCTIONS}\n\n"
            f"Document type: {document_type.value}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Document markdown:\n{markdown_document[:24000]}"
        )

        async def operation() -> str:
            return await self.adapter.chat([{"role": "user", "content": prompt}], temperature=0.0)

        raw = await retry_async(operation, attempts=self.settings.max_retries)
        parsed = parse_json_object(raw)
        parsed["document_type"] = document_type.value
        model = SCHEMA_MODELS.get(document_type)
        if not model:
            return parsed
        try:
            return model.model_validate(parsed).model_dump()
        except ValidationError as exc:
            raise JsonParseFailureError(f"Extracted JSON does not match {document_type.value} schema: {exc}") from exc

