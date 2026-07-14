from app.schemas.common import DocumentType
from app.services.validation_service import ValidationService


def test_date_validation() -> None:
    validator = ValidationService()
    # Test valid dates
    assert validator._is_valid_date("2026-06-08")
    assert validator._is_valid_date("08/06/2026")
    assert validator._is_valid_date("08-06-2026")
    assert validator._is_valid_date("Jun 08, 2026")
    assert validator._is_valid_date("08 Jun 2026")

    # Test invalid dates
    assert not validator._is_valid_date("not-a-date")
    assert not validator._is_valid_date("2026-13-45")
    assert not validator._is_valid_date("")


def test_float_validation() -> None:
    validator = ValidationService()
    # Test valid floats
    assert validator._is_valid_float("100.50")
    assert validator._is_valid_float("₹1,000.00")
    assert validator._is_valid_float(123)
    assert validator._is_valid_float(None)
    assert validator._is_valid_float("")

    # Test invalid floats
    assert not validator._is_valid_float("one hundred")


def test_currency_validation() -> None:
    validator = ValidationService()
    # Test valid currencies
    assert validator._is_valid_currency("INR")
    assert validator._is_valid_currency("USD")
    assert validator._is_valid_currency("₹")
    assert validator._is_valid_currency("$")

    # Test invalid currencies
    assert not validator._is_valid_currency("XYZ12")
    assert not validator._is_valid_currency("123")


def test_purchase_order_validation() -> None:
    validator = ValidationService()
    # PO valid amounts
    data_ok = {
        "document_type": "purchase_order",
        "po_number": "PO-100",
        "po_date": "2026-06-08",
        "vendor_name": "Supplier Corp",
        "subtotal": 500.0,
        "tax": 90.0,
        "grand_total": 590.0,
    }
    validated_ok = validator.validate(data_ok)
    assert not [issue for issue in validated_ok.issues if issue.status == "amount_mismatch"]

    # PO mismatched amounts
    data_bad = {
        "document_type": "purchase_order",
        "po_number": "PO-100",
        "po_date": "2026-06-08",
        "vendor_name": "Supplier Corp",
        "subtotal": 500.0,
        "tax": 90.0,
        "grand_total": 600.0,
    }
    validated_bad = validator.validate(data_bad)
    assert any(issue.status == "amount_mismatch" for issue in validated_bad.issues)


def test_bank_statement_balance_progression() -> None:
    validator = ValidationService()

    # Chronological/Ascending progression (balance[i] = balance[i-1] + credit - debit)
    data_asc_ok = {
        "document_type": "bank_statement",
        "bank_name": "MyBank",
        "account_number": "12345",
        "transactions": [
            {"date": "2026-06-01", "description": "Opening", "debit": 0.0, "credit": 0.0, "balance": 100.0},
            {"date": "2026-06-02", "description": "Deposit", "debit": 0.0, "credit": 50.0, "balance": 150.0},
            {"date": "2026-06-03", "description": "Withdrawal", "debit": 20.0, "credit": 0.0, "balance": 130.0},
        ],
    }
    validated_asc_ok = validator.validate(data_asc_ok)
    assert not [issue for issue in validated_asc_ok.issues if issue.status == "balance_mismatch"]

    # Mismatched ascending progression
    data_asc_bad = {
        "document_type": "bank_statement",
        "bank_name": "MyBank",
        "account_number": "12345",
        "transactions": [
            {"date": "2026-06-01", "description": "Opening", "debit": 0.0, "credit": 0.0, "balance": 100.0},
            {"date": "2026-06-02", "description": "Deposit", "debit": 0.0, "credit": 50.0, "balance": 140.0},  # Should be 150
            {"date": "2026-06-03", "description": "Withdrawal", "debit": 20.0, "credit": 0.0, "balance": 120.0},
        ],
    }
    validated_asc_bad = validator.validate(data_asc_bad)
    assert any(issue.status == "balance_mismatch" for issue in validated_asc_bad.issues)

    # Reverse chronological/Descending progression (balance[i] = balance[i-1] - credit + debit)
    data_desc_ok = {
        "document_type": "bank_statement",
        "bank_name": "MyBank",
        "account_number": "12345",
        "transactions": [
            {"date": "2026-06-03", "description": "Withdrawal", "debit": 20.0, "credit": 0.0, "balance": 130.0},
            {"date": "2026-06-02", "description": "Deposit", "debit": 0.0, "credit": 50.0, "balance": 150.0},
            {"date": "2026-06-01", "description": "Opening", "debit": 0.0, "credit": 0.0, "balance": 100.0},
        ],
    }
    validated_desc_ok = validator.validate(data_desc_ok)
    assert not [issue for issue in validated_desc_ok.issues if issue.status == "balance_mismatch"]


def test_bank_statement_duplicate_tx() -> None:
    validator = ValidationService()
    data = {
        "document_type": "bank_statement",
        "bank_name": "MyBank",
        "account_number": "12345",
        "transactions": [
            {"date": "2026-06-01", "description": "Coffee", "debit": 5.0, "credit": 0.0, "balance": 95.0},
            {"date": "2026-06-01", "description": "Coffee", "debit": 5.0, "credit": 0.0, "balance": 95.0},
        ],
    }
    validated = validator.validate(data)
    assert any(issue.status == "duplicate" for issue in validated.issues)
