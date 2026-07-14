"""
Merges CMA extraction results from multiple documents into one year-wise table.
"""

from app.schemas.cma_fields import CMA_SECTIONS


def _fy_sort_key(fy: str) -> int:
    try:
        return int(fy.split("-")[0])
    except Exception:
        return 0


def _empty_entry() -> dict:
    return {
        "value": None, "confidence": None,
        "evidence": None, "page": None,
        "source_doc_id": None, "source_filename": None, "is_primary": False,
    }


def _should_replace(existing: dict, candidate: dict, is_primary: bool) -> bool:
    """Return True if candidate is better than existing."""
    if existing.get("value") is None:
        return candidate.get("value") is not None
    if is_primary and not existing.get("is_primary", False):
        return True
    if is_primary == existing.get("is_primary", False):
        return float(candidate.get("confidence") or 0) > float(existing.get("confidence") or 0)
    return False


def merge_documents(
    company_slug: str,
    company_name: str,
    doc_results:  list[dict],
    raw: bool = False,
) -> dict:
    """
    Merge per-document extraction results into a single year-wise CMA table.
    """
    sections_def = CMA_SECTIONS

    # Initialise scaffold
    cma_data: dict = {
        sk: {"label": sm["label"], "fields": {f: {} for f in sm["fields"]}}
        for sk, sm in sections_def.items()
    }

    all_fys:  list[str] = []
    warnings: list[str] = []
    docs_out: list[dict] = []

    for doc in doc_results:
        doc_id     = doc["doc_id"]
        filename   = doc["filename"]
        current_fy  = doc.get("current_fy") or doc.get("primary_fy") or "unknown"
        previous_fy = doc.get("previous_fy") or doc.get("secondary_fy") or "unknown"
        status      = doc.get("status", "unknown")
        extraction  = doc.get("extraction")

        fields_found = 0
        if extraction:
            fields_found = extraction.get("meta", {}).get("fields_found", 0)

        docs_out.append({
            "doc_id":       doc_id,
            "filename":     filename,
            "primary_fy":   current_fy,
            "secondary_fy": previous_fy,
            "status":       status,
            "fields_found": fields_found,
        })

        if not extraction or status != "success":
            warnings.append(f"{filename}: skipped ({status})")
            continue

        if current_fy  != "unknown": all_fys.append(current_fy)
        if previous_fy != "unknown": all_fys.append(previous_fy)

        for sk, section_data in extraction.get("sections", {}).items():
            if sk not in cma_data:
                continue
            for field_name, fv in section_data.get("fields", {}).items():
                if field_name not in cma_data[sk]["fields"]:
                    continue

                bucket = cma_data[sk]["fields"][field_name]

                def _try_set(fy: str, year_key: str, is_primary: bool):
                    if not fy or fy == "unknown":
                        return
                    year_data = fv.get(year_key, {})
                    if not isinstance(year_data, dict):
                        return
                    candidate = {
                        "value":           year_data.get("value"),
                        "confidence":      year_data.get("confidence"),
                        "evidence":        year_data.get("evidence"),
                        "page":            year_data.get("page"),
                        "source_doc_id":   doc_id,
                        "source_filename": filename,
                        "is_primary":      is_primary,
                    }
                    if candidate["value"] is None:
                        return
                    existing = bucket.get(fy)
                    if existing is None or _should_replace(existing, candidate, is_primary):
                        bucket[fy] = candidate

                _try_set(current_fy,  "current",  is_primary=True)
                _try_set(previous_fy, "previous", is_primary=False)

    financial_years = sorted(
        set(fy for fy in all_fys if fy != "unknown"),
        key=_fy_sort_key
    )

    # Fill missing year slots with empty entry
    for sk in cma_data:
        for fn in cma_data[sk]["fields"]:
            bucket = cma_data[sk]["fields"][fn]
            for fy in financial_years:
                if fy not in bucket:
                    bucket[fy] = _empty_entry()

    return {
        "company_slug":    company_slug,
        "company_name":    company_name,
        "financial_years": financial_years,
        "documents":       docs_out,
        "cma_data":        cma_data,
        "field_mode":      "full_199",
        "warnings":        warnings,
    }
