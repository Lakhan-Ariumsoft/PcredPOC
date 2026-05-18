"""
Pydantic v2 schemas for the CMA extraction API.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Single extracted value
# ---------------------------------------------------------------------------

class ExtractedValue(BaseModel):
    value: Optional[float] = Field(None, description="Amount in Lakhs (INR)")
    confidence: int = Field(0, ge=0, le=100, description="Match confidence %")
    page: int = Field(0, description="Source PDF page number (1-indexed)")
    matched_text: str = Field("", description="Raw line from PDF that was matched")


# ---------------------------------------------------------------------------
# Per-field result: one entry per financial year
# ---------------------------------------------------------------------------

class CMARawField(BaseModel):
    label: str
    section: str
    years: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trade Payables sub-breakdown
# ---------------------------------------------------------------------------

class TradePayables(BaseModel):
    msme: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    others: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    total: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Section groupings
# ---------------------------------------------------------------------------

class ShareholdersFunds(BaseModel):
    share_capital: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    reserves_and_surplus: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    money_received_against_share_warrants: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    share_application_money_pending_allotment: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)


class NonCurrentLiabilities(BaseModel):
    long_term_borrowings: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    deferred_tax_liabilities_net: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    other_long_term_liabilities: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    long_term_provisions: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)


class CurrentLiabilities(BaseModel):
    short_term_borrowings: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    trade_payables: TradePayables = Field(default_factory=TradePayables)
    other_current_liabilities: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)
    short_term_provisions: dict[str, Optional[ExtractedValue]] = Field(default_factory=dict)


class EquityAndLiabilities(BaseModel):
    shareholders_funds: ShareholdersFunds = Field(default_factory=ShareholdersFunds)
    non_current_liabilities: NonCurrentLiabilities = Field(default_factory=NonCurrentLiabilities)
    current_liabilities: CurrentLiabilities = Field(default_factory=CurrentLiabilities)


# ---------------------------------------------------------------------------
# Extraction log entry
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    field_key: str
    confidence: int
    page: int
    raw_text: str


# ---------------------------------------------------------------------------
# Top-level extraction result
# ---------------------------------------------------------------------------

class CMAExtractionResult(BaseModel):
    company: Optional[str] = None
    source_file: str
    financial_years: list[str] = Field(default_factory=list)
    currency: str = "INR"
    unit: str = "Lakhs"
    balance_sheet_pages_found: list[int] = Field(default_factory=list)
    equity_and_liabilities: EquityAndLiabilities = Field(default_factory=EquityAndLiabilities)
    extraction_log: list[LogEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request/response wrappers
# ---------------------------------------------------------------------------

class ExtractionResponse(BaseModel):
    success: bool
    message: str
    data: Optional[CMAExtractionResult] = None