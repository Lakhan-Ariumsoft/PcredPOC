from app.services.validation_service import ValidationService


def test_invoice_amount_validation_ok() -> None:
    data = {
        "document_type": "invoice",
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-01",
        "vendor_name": "Vendor",
        "subtotal": 100.0,
        "cgst": 9.0,
        "sgst": 9.0,
        "igst": 0.0,
        "total_tax": 18.0,
        "grand_total": 118.0,
        "line_items": [],
    }
    validated = ValidationService().validate(data)
    assert not [issue for issue in validated.issues if issue.status == "amount_mismatch"]


def test_invoice_amount_validation_mismatch() -> None:
    data = {
        "document_type": "invoice",
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-01",
        "vendor_name": "Vendor",
        "subtotal": 100.0,
        "total_tax": 18.0,
        "grand_total": 110.0,
    }
    validated = ValidationService().validate(data)
    assert any(issue.status == "amount_mismatch" for issue in validated.issues)


def test_duplicate_line_items() -> None:
    data = {
        "document_type": "invoice",
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-01",
        "vendor_name": "Vendor",
        "grand_total": 10.0,
        "line_items": [
            {"description": "Item", "quantity": 1, "unit_price": 10, "amount": 10},
            {"description": "Item", "quantity": 1, "unit_price": 10, "amount": 10},
        ],
    }
    validated = ValidationService().validate(data)
    assert any(issue.status == "duplicate" for issue in validated.issues)
