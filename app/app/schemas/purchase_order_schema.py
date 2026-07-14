from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invoice_schema import InvoiceLineItem


class PurchaseOrderSchema(BaseModel):
    document_type: str = "purchase_order"
    po_number: str | None = None
    po_date: str | None = None
    vendor_name: str | None = None
    buyer_name: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    grand_total: float | None = None
    line_items: list[InvoiceLineItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
