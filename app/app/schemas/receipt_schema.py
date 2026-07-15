from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ReceiptSchema(BaseModel):
    document_type: str = "receipt"
    merchant_name: str | None = None
    receipt_number: str | None = None
    date: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None

    model_config = ConfigDict(extra="allow")
