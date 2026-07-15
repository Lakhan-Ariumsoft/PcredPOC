from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class InvoiceLineItem(BaseModel):
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class InvoiceSchema(BaseModel):
    document_type: str = "invoice"
    invoice_number: str | None = None
    invoice_date: str | None = None
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    customer_name: str | None = None
    customer_gstin: str | None = None
    subtotal: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    total_tax: float | None = None
    grand_total: float | None = None
    currency: str | None = None
    line_items: list[InvoiceLineItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
