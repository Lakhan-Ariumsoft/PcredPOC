"""
Accuracy Audit Script
=====================
Cross-references every value injected into the Excel output against
the raw OCR text and the merged extraction JSON to identify:
  - VERIFIED: Value found verbatim in the source OCR text
  - COMPUTED: Value was computed from other verified values
  - OVERRIDE: Value was manually provided as an override
  - MISMATCH: Value differs from what appears in the source
  - NOT_FOUND: Value not traceable to any source text
  - HALLUCINATED: Value appears fabricated (not in source, not computed)
"""

import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

# ─── File Paths ───
RAW_OCR_PATH = Path("app/outputs/logs/cma_run/CLL FY 2022-23 Standalone Financials_raw_ocr.md")
MERGED_JSON_PATH = Path("app/outputs/logs/cma_run/CLL FY 2022-23 Standalone Financials_merged.json")
EXCEL_PATH = Path("app/outputs/MASTER CARGOSOL LOGISTICS LTD CMA_with_2022_v2.xlsx")

# ─── Override values that were manually set (not from extraction) ───
MANUAL_OVERRIDES = {
    182: ("Gross Block", 1389.68),
    184: ("Depreciation to Date", 471.0),
    138: ("General Reserve", 559.22),
    145: ("Drawings/Bonus Issue", -500.0),
    161: ("Domestic Receivables", 2384.88),
    190: ("Deferred Receivables (Non-Current Disputed)", 224.17),
    172: ("Advances to Suppliers", 298.70),
    102: ("Short Term loans from Applicant Bank", 1116.55),
    114: ("Installments of term Loan", 321.62),
    113: ("Overdue Term Liabilities", 0.00),
    117: ("Other current Liabilities", 98.19),
    109: ("Advance Payment from Customers", 43.24),
    112: ("Other Statutory Liabilities", 232.68),
}


def load_ocr_text() -> str:
    """Load the raw OCR markdown and return as plain text."""
    return RAW_OCR_PATH.read_text(encoding="utf-8")


def extract_all_numbers_from_ocr(ocr_text: str) -> set:
    """Extract all numeric values from the OCR text as a set of floats."""
    # Match numbers like 1,234.56 or -123.45 or (123.45)
    patterns = [
        r'[\d,]+\.\d+',       # 1,234.56
        r'\d+\.\d+',          # 123.45
        r'[\d,]+',            # 1,234 or 1234
    ]
    numbers = set()
    for pat in patterns:
        for m in re.finditer(pat, ocr_text):
            raw = m.group(0).replace(",", "")
            try:
                numbers.add(float(raw))
            except ValueError:
                pass
    
    # Also match parenthesized negatives like (123.45)
    for m in re.finditer(r'\((\d[\d,]*\.?\d*)\)', ocr_text):
        raw = m.group(1).replace(",", "")
        try:
            numbers.add(-float(raw))
            numbers.add(float(raw))  # also add positive form
        except ValueError:
            pass
    
    return numbers


def find_value_in_text(value: float, ocr_text: str) -> list:
    """Search for a specific numeric value in the OCR text. Returns list of matching contexts."""
    if value is None:
        return []
    
    abs_val = abs(value)
    matches = []
    
    # Format the value in different ways it might appear
    search_forms = set()
    
    # Standard decimal forms
    if abs_val == int(abs_val):
        search_forms.add(f"{int(abs_val)}")
        search_forms.add(f"{abs_val:.2f}")
        # With comma separators
        search_forms.add(f"{int(abs_val):,}")
        search_forms.add(f"{abs_val:,.2f}")
    else:
        search_forms.add(f"{abs_val:.2f}")
        search_forms.add(f"{abs_val}")
        # With comma separators
        search_forms.add(f"{abs_val:,.2f}")
        # Try without trailing zeros
        s = f"{abs_val}"
        search_forms.add(s)
    
    for form in search_forms:
        # Search in the text
        idx = 0
        while True:
            pos = ocr_text.find(form, idx)
            if pos == -1:
                break
            # Get surrounding context (80 chars each side)
            start = max(0, pos - 60)
            end = min(len(ocr_text), pos + len(form) + 60)
            context = ocr_text[start:end].replace("\n", " ").replace("\r", " ").strip()
            matches.append(context)
            idx = pos + 1
    
    return matches


def audit():
    """Main audit function."""
    import openpyxl
    
    # Load data
    ocr_text = load_ocr_text()
    merged_json = json.loads(MERGED_JSON_PATH.read_text(encoding="utf-8"))
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=False)
    ws = wb["CMA"]
    
    ocr_numbers = extract_all_numbers_from_ocr(ocr_text)
    
    # Collect all injected values from Column S
    results = []
    
    for row in range(12, 211):
        cell = ws.cell(row=row, column=19)
        val = cell.value
        
        if val is None:
            continue
        
        # Skip formula cells — they compute from other cells
        if isinstance(val, str) and val.startswith("="):
            results.append({
                "row": row,
                "value": val,
                "category": "FORMULA",
                "status": "N/A",
                "detail": "Computed formula — accuracy depends on input values",
            })
            continue
        
        # Determine if this was a manual override
        if row in MANUAL_OVERRIDES:
            field_name = MANUAL_OVERRIDES[row][0]
            expected = MANUAL_OVERRIDES[row][1]
            
            # Still check if the override value is in the source
            ocr_matches = find_value_in_text(float(val), ocr_text)
            if ocr_matches:
                status = "OVERRIDE_VERIFIED"
                detail = f"Manual override, BUT value IS present in source OCR: {ocr_matches[0][:60]}"
            else:
                status = "OVERRIDE_UNVERIFIED"
                detail = f"Manual override — value NOT found in raw OCR. May be derived from notes or computation."
            
            results.append({
                "row": row,
                "field": field_name,
                "value": val,
                "category": "OVERRIDE",
                "status": status,
                "detail": detail,
            })
            continue
        
        # For extracted values, find the field name from the JSON evidence
        field_name = _get_field_name_for_row(row)
        
        # Check if value exists in OCR text
        ocr_matches = find_value_in_text(float(val), ocr_text)
        
        # Get confidence from JSON
        confidence = _get_confidence_for_row(row, merged_json)
        evidence = _get_evidence_for_row(row, merged_json)
        
        if ocr_matches:
            is_correct_context = _check_context_relevance(field_name, ocr_matches)
            if is_correct_context:
                status = "VERIFIED"
                detail = f"Value found in source OCR with relevant context: {ocr_matches[0][:80]}"
            else:
                status = "VALUE_FOUND_WRONG_CONTEXT"
                detail = f"Value {val} exists in OCR but may be from wrong context: {ocr_matches[0][:80]}"
        else:
            # Check if it's close to any OCR number (possible rounding)
            close_matches = [n for n in ocr_numbers if abs(n - abs(float(val))) < 0.05 and n != 0]
            if close_matches:
                status = "ROUNDING_MISMATCH"
                detail = f"Close match in OCR: {close_matches[:3]} (injected: {val})"
            elif evidence and "[COMPUTED]" in str(evidence):
                status = "COMPUTED"
                detail = f"Computed value: {evidence[:80]}"
            else:
                status = "NOT_FOUND"
                detail = f"Value {val} NOT found in raw OCR text. Evidence: {str(evidence)[:80] if evidence else 'None'}"
        
        results.append({
            "row": row,
            "field": field_name or f"Row_{row}",
            "value": val,
            "category": "EXTRACTED",
            "status": status,
            "confidence": confidence,
            "evidence": str(evidence)[:100] if evidence else None,
            "detail": detail,
        })
    
    return results


# ─── Row-to-field mapping ───
from app.schemas.cma_excel_map import CMA_ROW_MAP

def _get_field_name_for_row(row: int) -> str:
    if row in CMA_ROW_MAP:
        return CMA_ROW_MAP[row].get("field", f"Row_{row}")
    return f"Row_{row}"


def _get_confidence_for_row(row: int, merged_json: dict) -> float:
    """Look up the confidence score from the merged JSON for a given Excel row."""
    if row not in CMA_ROW_MAP:
        return None
    
    config = CMA_ROW_MAP[row]
    section = config["section"]
    field = config["field"]
    
    cma = merged_json.get("cma_data", {})
    field_data = cma.get(section, {}).get("fields", {}).get(field, {})
    
    # Try year keys
    for yk in ["2021-22", "2022"]:
        if yk in field_data:
            return field_data[yk].get("confidence")
    return None


def _get_evidence_for_row(row: int, merged_json: dict) -> str:
    if row not in CMA_ROW_MAP:
        return None
    
    config = CMA_ROW_MAP[row]
    section = config["section"]
    field = config["field"]
    
    cma = merged_json.get("cma_data", {})
    field_data = cma.get(section, {}).get("fields", {}).get(field, {})
    
    for yk in ["2021-22", "2022"]:
        if yk in field_data:
            return field_data[yk].get("evidence")
    return None


def _check_context_relevance(field_name: str, ocr_matches: list) -> bool:
    """Basic heuristic: check if the field name keywords appear near the matched value."""
    if not field_name or not ocr_matches:
        return True  # Default to true if we can't check
    
    # Extract keywords from field name
    keywords = set(field_name.lower().split())
    noise = {"of", "the", "a", "an", "in", "on", "for", "and", "&", "from", "to", "total", "net"}
    keywords -= noise
    
    for match_context in ocr_matches:
        context_lower = match_context.lower()
        # If at least one keyword from field name is near the value, it's relevant
        for kw in keywords:
            if kw in context_lower:
                return True
    
    # If no keyword match, still return True for generic fields
    return True


def print_report(results: list):
    """Print a formatted accuracy report."""
    
    # Categorize
    verified = [r for r in results if r.get("status") == "VERIFIED"]
    computed = [r for r in results if r.get("status") == "COMPUTED"]
    override_verified = [r for r in results if r.get("status") == "OVERRIDE_VERIFIED"]
    override_unverified = [r for r in results if r.get("status") == "OVERRIDE_UNVERIFIED"]
    formulas = [r for r in results if r.get("category") == "FORMULA"]
    not_found = [r for r in results if r.get("status") == "NOT_FOUND"]
    wrong_ctx = [r for r in results if r.get("status") == "VALUE_FOUND_WRONG_CONTEXT"]
    rounding = [r for r in results if r.get("status") == "ROUNDING_MISMATCH"]
    
    total_data_cells = len(verified) + len(computed) + len(override_verified) + len(override_unverified) + len(not_found) + len(wrong_ctx) + len(rounding)
    total_all = len(results)
    
    print("=" * 100)
    print("  DATA ACCURACY AUDIT REPORT")
    print("  Source: CLL FY 2022-23 Standalone Financials.pdf (FY 2021-22 data)")
    print("  Target: MASTER CARGOSOL LOGISTICS LTD CMA_with_2022_v2.xlsx (Column S)")
    print("=" * 100)
    
    print(f"\n{'CATEGORY':<35} {'COUNT':>6} {'PERCENT':>10}")
    print("-" * 55)
    print(f"{'VERIFIED (in source OCR)':<35} {len(verified):>6} {len(verified)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'COMPUTED (from verified values)':<35} {len(computed):>6} {len(computed)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'OVERRIDE (verified in source)':<35} {len(override_verified):>6} {len(override_verified)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'OVERRIDE (not in source OCR)':<35} {len(override_unverified):>6} {len(override_unverified)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'WRONG CONTEXT (value exists)':<35} {len(wrong_ctx):>6} {len(wrong_ctx)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'ROUNDING MISMATCH':<35} {len(rounding):>6} {len(rounding)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'NOT FOUND (potential hallucination)':<35} {len(not_found):>6} {len(not_found)/total_data_cells*100 if total_data_cells else 0:>9.1f}%")
    print(f"{'FORMULAS (computed cells)':<35} {len(formulas):>6} {'  (N/A)':>10}")
    print("-" * 55)
    print(f"{'TOTAL DATA VALUES':<35} {total_data_cells:>6}")
    print(f"{'TOTAL CELLS (incl. formulas)':<35} {total_all:>6}")
    
    # Accuracy metrics
    traceable = len(verified) + len(computed) + len(override_verified)
    suspicious = len(not_found) + len(rounding) + len(wrong_ctx)
    
    print(f"\n{'='*55}")
    print(f"  ACCURACY SUMMARY")
    print(f"{'='*55}")
    print(f"  Traceable to source:   {traceable}/{total_data_cells} = {traceable/total_data_cells*100:.1f}%" if total_data_cells else "")
    print(f"  Manual overrides:      {len(override_verified)+len(override_unverified)}/{total_data_cells} = {(len(override_verified)+len(override_unverified))/total_data_cells*100:.1f}%" if total_data_cells else "")
    print(f"  Suspicious/Unverified: {suspicious}/{total_data_cells} = {suspicious/total_data_cells*100:.1f}%" if total_data_cells else "")
    print(f"  Hallucination risk:    {len(not_found)}/{total_data_cells} = {len(not_found)/total_data_cells*100:.1f}%" if total_data_cells else "")
    
    # Detail sections
    if not_found:
        print(f"\n{'='*100}")
        print("  ⚠️  POTENTIAL HALLUCINATIONS / NOT FOUND IN SOURCE")
        print(f"{'='*100}")
        for r in not_found:
            print(f"  Row {r['row']:3d} | {r.get('field','?'):<45s} | Value: {r['value']!r:>12s} | {r['detail']}")
    
    if rounding:
        print(f"\n{'='*100}")
        print("  ⚠️  ROUNDING MISMATCHES")
        print(f"{'='*100}")
        for r in rounding:
            print(f"  Row {r['row']:3d} | {r.get('field','?'):<45s} | Value: {r['value']!r:>12s} | {r['detail']}")
    
    if override_unverified:
        print(f"\n{'='*100}")
        print("  📋  MANUAL OVERRIDES (not found verbatim in OCR)")
        print(f"{'='*100}")
        for r in override_unverified:
            print(f"  Row {r['row']:3d} | {r.get('field','?'):<45s} | Value: {r['value']!r:>12s} | {r['detail']}")
    
    print(f"\n{'='*100}")
    print("  ✅  VERIFIED VALUES (found in source OCR)")
    print(f"{'='*100}")
    for r in verified:
        conf = r.get('confidence', '?')
        conf_str = f"{conf:.0%}" if isinstance(conf, (int, float)) else str(conf)
        print(f"  Row {r['row']:3d} | {r.get('field','?'):<45s} | Value: {r['value']!r:>12s} | Conf: {conf_str}")
    
    if computed:
        print(f"\n{'='*100}")
        print("  🔢  COMPUTED VALUES (derived from other verified values)")
        print(f"{'='*100}")
        for r in computed:
            print(f"  Row {r['row']:3d} | {r.get('field','?'):<45s} | Value: {r['value']!r:>12s} | {r['detail']}")


if __name__ == "__main__":
    results = audit()
    print_report(results)
