from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BankTransaction(BaseModel):
    date: str | None = None
    description: str | None = None
    debit: float | None = None
    credit: float | None = None
    balance: float | None = None


class BankStatementSchema(BaseModel):
    document_type: str = "bank_statement"
    bank_name: str | None = None
    account_number: str | None = None
    statement_period: str | None = None
    transactions: list[BankTransaction] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
