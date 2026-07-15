from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    invoice = "invoice"
    receipt = "receipt"
    bank_statement = "bank_statement"
    purchase_order = "purchase_order"
    financial_report = "financial_report"
    cma = "cma"
    unknown = "unknown"


class EntityType(str, Enum):
    """Legal structure of the entity whose financials are being extracted.

    Different entity types use materially different balance-sheet vocabulary
    (e.g. LLPs have "Partners' Capital" instead of "Share Capital"), which
    drives which keyword/synonym overlay the extraction pipeline uses.
    """
    private_limited = "private_limited"
    public_limited = "public_limited"
    llp = "llp"
    partnership = "partnership"
    proprietorship = "proprietorship"
    huf = "huf"
    other = "other"


class FieldConfidence(BaseModel):
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ValidationIssue(BaseModel):
    field: str
    status: str
    message: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ValidatedDocument(BaseModel):
    document_type: DocumentType
    data: dict[str, Any]
    field_confidence: dict[str, FieldConfidence]
    issues: list[ValidationIssue] = []
