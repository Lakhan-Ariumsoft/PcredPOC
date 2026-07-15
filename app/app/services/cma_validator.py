"""
CMA extraction validator.
Runs post-extraction sanity checks before results reach the Excel output.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.75   # below this → flag for human review
BALANCE_TOLERANCE    = 0.02   # 2% tolerance on accounting identity checks


def _val(section: dict, field_name: str, year: str) -> Optional[float]:
    """Safe accessor into merged cma_data structure."""
    try:
        entry = section.get(field_name, {}).get(year)
        if entry and entry.get("value") is not None:
            return float(entry["value"])
    except (TypeError, ValueError):
        pass
    return None


def _pct_diff(a: float, b: float) -> float:
    """Percentage difference between a and b relative to b."""
    if b == 0:
        return 0.0
    return abs(a - b) / abs(b)


def validate_extraction(merged_result: dict) -> dict:
    """
    Run validation checks on a merged CMA result.
    Returns the same dict with a "validation" key added.
    """
    cma_data = merged_result.get("cma_data", {})
    financial_years = merged_result.get("financial_years", [])

    warnings         = []
    low_conf_fields  = []
    unit_warnings    = []
    checks_passed    = []

    for fy in financial_years:
        year_label = f"FY {fy}"

        def get(section_key: str, field_name: str) -> Optional[float]:
            section = cma_data.get(section_key, {}).get("fields", {})
            return _val(section, field_name, fy)

        # ── 1. Accounting identity: Total Assets ≈ Total CL + Total TL + Net Worth ──
        total_assets = get("intangibles",         "Total Assets")
        total_cl     = get("current_liabilities", "Total Current Liabilities")
        total_tl     = get("term_liabilities",    "Total Term Liabilities")
        net_worth    = get("net_worth",            "Net Worth")

        if all(v is not None for v in [total_assets, total_cl, total_tl, net_worth]):
            total_liab_nw = total_cl + total_tl + net_worth
            diff = _pct_diff(total_assets, total_liab_nw)
            if diff > BALANCE_TOLERANCE:
                warnings.append({
                    "check":   "accounting_identity",
                    "year":    fy,
                    "message": (
                        f"{year_label}: Total Assets ({total_assets:.2f}) ≠ "
                        f"CL({total_cl:.2f}) + TL({total_tl:.2f}) + NW({net_worth:.2f}) "
                        f"= {total_liab_nw:.2f} (diff {diff*100:.1f}%)"
                    ),
                    "severity": "high" if diff > 0.05 else "medium",
                })
            else:
                checks_passed.append(f"{year_label}: accounting identity OK ({diff*100:.2f}% diff)")

        # ── 2. Revenue must be positive ───────────────────────────────────────
        net_sales = get("sales", "Net Sales")
        if net_sales is not None and net_sales < 0:
            warnings.append({
                "check":    "sign_check",
                "year":     fy,
                "message":  f"{year_label}: Net Sales is negative ({net_sales}) — likely sign error",
                "severity": "high",
            })

        # ── 3. PAT sign consistency with PBT ─────────────────────────────────
        pbt = get("pnl", "Profit before Tax")
        pat = get("pnl", "Net Profit/Loss (PAT)")
        if pbt is not None and pat is not None:
            if pbt > 0 and pat > pbt * 1.1:
                warnings.append({
                    "check":    "pbt_pat_consistency",
                    "year":     fy,
                    "message":  f"{year_label}: PAT ({pat}) > PBT ({pbt}) by >10% — check tax credit",
                    "severity": "medium",
                })
            elif pbt < 0 and pat > 0:
                warnings.append({
                    "check":    "pbt_pat_consistency",
                    "year":     fy,
                    "message":  f"{year_label}: PBT is negative ({pbt}) but PAT is positive ({pat}) — verify",
                    "severity": "medium",
                })

        # ── 4. Current Assets + Fixed/Non-Current ≈ Total Assets ─────────────
        total_ca = get("current_assets", "Total Current Assets")
        net_block = get("fixed_assets", "Net Block")
        if total_assets is not None and total_ca is not None and net_block is not None:
            sum_assets = total_ca + net_block
            diff = _pct_diff(sum_assets, total_assets)
            if diff <= 0.15:
                checks_passed.append(
                    f"{year_label}: CA({total_ca}) + NetBlock({net_block}) ≈ TotalAssets({total_assets}) (within {diff*100:.1f}%)"
                )

        # ── 5. Unit consistency — detect magnitude outliers ───────────────────
        key_values = {
            "Net Sales":   net_sales,
            "Total Assets": total_assets,
            "Net Worth":   net_worth,
        }
        valid_values = [v for v in key_values.values() if v is not None and v > 0]
        if len(valid_values) >= 2:
            max_v = max(valid_values)
            min_v = min(valid_values)
            if max_v / min_v > 10_000:
                unit_warnings.append({
                    "year":    fy,
                    "message": (
                        f"{year_label}: extreme value range detected "
                        f"(max={max_v:.2f}, min={min_v:.2f}) — possible unit mismatch (Lakhs vs Crores)"
                    ),
                    "values": {k: v for k, v in key_values.items() if v is not None},
                })

    # ── 6. Low-confidence field collection ───────────────────────────────────
    for section_key, section_data in cma_data.items():
        section_label = section_data.get("label", section_key)
        for field_name, field_data in section_data.get("fields", {}).items():
            for fy in financial_years:
                entry = field_data.get(fy, {})
                val  = entry.get("value")
                conf = entry.get("confidence")
                if val is not None and conf is not None and float(conf) < CONFIDENCE_THRESHOLD:
                    low_conf_fields.append({
                        "section":    section_label,
                        "field":      field_name,
                        "year":       fy,
                        "value":      val,
                        "confidence": round(float(conf), 2),
                        "evidence":   entry.get("evidence", ""),
                    })

    validation_summary = {
        "total_warnings":          len(warnings),
        "total_low_conf_fields":   len(low_conf_fields),
        "total_unit_warnings":     len(unit_warnings),
        "total_checks_passed":     len(checks_passed),
        "warnings":                warnings,
        "low_confidence_fields":   low_conf_fields,
        "unit_warnings":           unit_warnings,
        "checks_passed":           checks_passed,
    }

    if warnings:
        logger.warning(f"Validation: {len(warnings)} accounting warnings for {merged_result.get('company_slug')}")
    if low_conf_fields:
        logger.info(f"Validation: {len(low_conf_fields)} low-confidence fields flagged")

    merged_result["validation"] = validation_summary
    return merged_result
