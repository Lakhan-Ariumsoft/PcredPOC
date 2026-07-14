import pandas as pd

from app.schemas.common import DocumentType, FieldConfidence, ValidatedDocument, ValidationIssue
from app.services.excel_service import ExcelService


def test_excel_generation(tmp_path) -> None:
    validated = ValidatedDocument(
        document_type=DocumentType.invoice,
        data={
            "document_type": "invoice",
            "invoice_number": "INV-1",
            "invoice_date": "2026-01-01",
            "vendor_name": "Vendor",
            "grand_total": 118.0,
            "total_tax": 18.0,
            "currency": "INR",
            "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100, "amount": 100}],
        },
        field_confidence={
            "invoice_number": FieldConfidence(value="INV-1", confidence=0.95),
            "invoice_date": FieldConfidence(value="2026-01-01", confidence=0.95),
            "vendor_name": FieldConfidence(value="Vendor", confidence=0.95),
            "grand_total": FieldConfidence(value=118.0, confidence=0.95),
        },
        issues=[
            ValidationIssue(field="invoice_date", status="ok", message="", confidence=0.95),
        ]
    )
    output = ExcelService().generate(validated, tmp_path / "invoice.xlsx")
    assert output.exists()

    # Read back and verify sheets exist
    excel = pd.ExcelFile(output)
    assert "Document Summary" in excel.sheet_names
    assert "Line Items" in excel.sheet_names
    assert "Validation Report" in excel.sheet_names


def test_excel_generation_bank_statement(tmp_path) -> None:
    validated = ValidatedDocument(
        document_type=DocumentType.bank_statement,
        data={
            "document_type": "bank_statement",
            "bank_name": "GlobalBank",
            "account_number": "987654",
            "statement_period": "June 2026",
            "transactions": [
                {"date": "2026-06-01", "description": "ATM Deposit", "debit": 0.0, "credit": 500.0, "balance": 1500.0},
                {"date": "2026-06-02", "description": "Grocery Store", "debit": 45.50, "credit": 0.0, "balance": 1454.50},
            ]
        },
        field_confidence={
            "bank_name": FieldConfidence(value="GlobalBank", confidence=0.95),
            "account_number": FieldConfidence(value="987654", confidence=0.95),
        },
        issues=[
            ValidationIssue(field="transactions", status="duplicate", message="Duplicate check passed", confidence=0.95),
        ]
    )
    output = ExcelService().generate(validated, tmp_path / "bank.xlsx")
    assert output.exists()

    excel = pd.ExcelFile(output)
    assert "Document Summary" in excel.sheet_names
    assert "Transactions" in excel.sheet_names
    assert "Validation Report" in excel.sheet_names

    summary_df = pd.read_excel(output, sheet_name="Document Summary")
    assert summary_df.iloc[0]["Field"] == "Bank Name"
    assert summary_df.iloc[0]["Value"] == "GlobalBank"

