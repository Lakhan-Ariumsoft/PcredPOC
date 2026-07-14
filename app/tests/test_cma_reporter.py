import pytest
import io
from openpyxl import load_workbook
from app.services.cma_reporter_service import generate_cma_excel

def test_generate_cma_excel():
    mock_data = {
        "company_name": "Acme Corp",
        "company_slug": "acme-corp",
        "financial_years": ["2022-23", "2023-24", "2024-25"],
        "cma_data": {
            "sales": {
                "label": "Sales",
                "fields": {
                    "Net Sales": {
                        "2022-23": {"value": 1000.0, "confidence": 0.9},
                        "2023-24": {"value": 1100.0, "confidence": 0.95},
                        "2024-25": {"value": 1200.0, "confidence": 0.95}
                    }
                }
            },
            "cost_of_sales": {
                "label": "Cost of Sales",
                "fields": {
                    "Depreciation": {
                        "2022-23": {"value": 50.0, "confidence": 0.9},
                        "2023-24": {"value": 55.0, "confidence": 0.9},
                        "2024-25": {"value": 60.0, "confidence": 0.9}
                    }
                }
            }
        }
    }
    
    excel_bytes = generate_cma_excel(mock_data)
    
    assert isinstance(excel_bytes, bytes)
    # Excel files are zip archives starting with the standard PK header
    assert excel_bytes.startswith(b"PK\x03\x04")
    
    # Load with openpyxl to inspect sheets
    wb = load_workbook(io.BytesIO(excel_bytes))
    assert "CMA" in wb.sheetnames
    
    ws = wb["CMA"]
    # Check that title cell contains Acme Corp
    title_val = ws.cell(row=1, column=1).value
    assert "Acme Corp" in title_val
