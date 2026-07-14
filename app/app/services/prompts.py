from __future__ import annotations

from app.schemas.common import DocumentType


JSON_MODE_INSTRUCTIONS = """You are a financial data extraction engine.

Return ONLY valid JSON.

Do not explain.
Do not add markdown.
Do not add comments.
Do not add text before or after JSON.

Missing values must be null.

Preserve exact numbers.

Preserve currency symbols.

Return according to schema."""


SCHEMAS: dict[DocumentType, str] = {
    DocumentType.invoice: """{
  "document_type": "invoice",
  "invoice_number": null,
  "invoice_date": null,
  "vendor_name": null,
  "vendor_gstin": null,
  "customer_name": null,
  "customer_gstin": null,
  "subtotal": null,
  "cgst": null,
  "sgst": null,
  "igst": null,
  "total_tax": null,
  "grand_total": null,
  "currency": null,
  "line_items": [{"description": null, "quantity": null, "unit_price": null, "amount": null}]
}""",
    DocumentType.receipt: """{
  "document_type": "receipt",
  "merchant_name": null,
  "receipt_number": null,
  "date": null,
  "currency": null,
  "subtotal": null,
  "tax": null,
  "total": null
}""",
    DocumentType.bank_statement: """{
  "document_type": "bank_statement",
  "bank_name": null,
  "account_number": null,
  "statement_period": null,
  "transactions": [{"date": null, "description": null, "debit": null, "credit": null, "balance": null}]
}""",
    DocumentType.purchase_order: """{
  "document_type": "purchase_order",
  "po_number": null,
  "po_date": null,
  "vendor_name": null,
  "buyer_name": null,
  "currency": null,
  "subtotal": null,
  "tax": null,
  "grand_total": null,
  "line_items": [{"description": null, "quantity": null, "unit_price": null, "amount": null}]
}""",
    DocumentType.financial_report: """{
  "document_type": "financial_report",
  "report_name": null,
  "period": null,
  "currency": null,
  "metrics": {},
  "tables": []
}""",
    DocumentType.unknown: "{}",
}
