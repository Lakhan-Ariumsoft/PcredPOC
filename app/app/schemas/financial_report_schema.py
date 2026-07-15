from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FinancialReportSchema(BaseModel):
    document_type: str = "financial_report"
    report_name: str | None = None
    period: str | None = None
    currency: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    tables: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")

    @field_validator("period", mode="before")
    @classmethod
    def coerce_period_to_string(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return ", ".join(str(val) for val in v.values() if val)
        elif isinstance(v, list):
            return ", ".join(str(val) for val in v if val)
        return v

