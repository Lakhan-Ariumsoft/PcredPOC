import pytest
from app.services.cma_validator import validate_extraction
from app.services.validation_service import ValidationService
from app.schemas.common import DocumentType

def test_cma_validator_direct():
    merged_data = {
        "company_slug": "test-company",
        "company_name": "Test Company",
        "financial_years": ["2022-23"],
        "cma_data": {
            "intangibles": {
                "label": "Intangibles",
                "fields": {
                    "Total Assets": {"2022-23": {"value": 1000.0, "confidence": 0.95}}
                }
            },
            "current_liabilities": {
                "label": "Current Liabilities",
                "fields": {
                    "Total Current Liabilities": {"2022-23": {"value": 300.0, "confidence": 0.95}}
                }
            },
            "term_liabilities": {
                "label": "Term Liabilities",
                "fields": {
                    "Total Term Liabilities": {"2022-23": {"value": 400.0, "confidence": 0.95}}
                }
            },
            "net_worth": {
                "label": "Net Worth",
                "fields": {
                    "Net Worth": {"2022-23": {"value": 300.0, "confidence": 0.95}}
                }
            },
            "sales": {
                "label": "Sales",
                "fields": {
                    "Net Sales": {"2022-23": {"value": -50.0, "confidence": 0.95}} # Negative sales should flag warning
                }
            }
        }
    }
    
    result = validate_extraction(merged_data)
    validation = result.get("validation", {})
    
    # Negative sales should trigger warning
    assert validation["total_warnings"] > 0
    warnings = validation["warnings"]
    assert any(w["check"] == "sign_check" for w in warnings)
    
    # Total Assets (1000) = CL(300) + TL(400) + NW(300) = 1000 (diff 0%), so accounting identity checks should pass
    assert any("accounting identity OK" in c for c in validation["checks_passed"])

def test_cma_validator_mismatch():
    merged_data = {
        "company_slug": "test-company",
        "company_name": "Test Company",
        "financial_years": ["2022-23"],
        "cma_data": {
            "intangibles": {
                "label": "Intangibles",
                "fields": {
                    "Total Assets": {"2022-23": {"value": 1000.0, "confidence": 0.95}}
                }
            },
            "current_liabilities": {
                "label": "Current Liabilities",
                "fields": {
                    "Total Current Liabilities": {"2022-23": {"value": 400.0, "confidence": 0.95}}
                }
            },
            "term_liabilities": {
                "label": "Term Liabilities",
                "fields": {
                    "Total Term Liabilities": {"2022-23": {"value": 400.0, "confidence": 0.95}}
                }
            },
            "net_worth": {
                "label": "Net Worth",
                "fields": {
                    "Net Worth": {"2022-23": {"value": 300.0, "confidence": 0.95}}
                }
            }
        }
    }
    
    result = validate_extraction(merged_data)
    validation = result.get("validation", {})
    
    # 1000 != 400 + 400 + 300 (1100) -> diff 10% which is > 2% tolerance
    assert validation["total_warnings"] > 0
    warnings = validation["warnings"]
    assert any(w["check"] == "accounting_identity" for w in warnings)

def test_validation_service_cma():
    validation_service = ValidationService()
    cma_payload = {
        "document_type": "cma",
        "company_slug": "test-company",
        "company_name": "Test Company",
        "financial_years": ["2022-23"],
        "cma_data": {
            "sales": {
                "fields": {
                    "Net Sales": {"2022-23": {"value": -10.0, "confidence": 0.5}} # Low confidence + negative sales
                }
            }
        }
    }
    
    validated = validation_service.validate(cma_payload)
    assert validated.document_type == DocumentType.cma
    assert len(validated.issues) > 0
    
    # Issues should include low confidence and negative sales warnings
    issue_fields = [issue.field for issue in validated.issues]
    assert "cma_data" in issue_fields
    assert any("low_confidence" in issue.status for issue in validated.issues)
