"""
Merges CMA extraction results from multiple documents into one year-wise table.
"""
from app.cma_fields import CMA_SECTIONS


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


def _better(existing: dict, candidate: dict, is_primary: bool) -> bool:
    """Return True if candidate should replace existing."""
    if existing["value"] is None:
        return candidate.get("value") is not None
    if is_primary and not existing["is_primary"]:
        return True
    if is_primary == existing["is_primary"]:
        return float(candidate.get("confidence") or 0) > float(existing.get("confidence") or 0)
    return False


def merge_documents(
    company_slug: str,
    company_name: str,
    doc_results:  list[dict],
) -> dict:
    """
    Merge per-document extraction results into a single year-wise CMA table.

    Each doc_result must have:
      doc_id, filename, primary_fy, secondary_fy, status, extraction (or None)
    """
    # Initialise scaffold from CMA_SECTIONS
    cma_data: dict = {
        sk: {"label": sm["label"], "fields": {f: {} for f in sm["fields"]}}
        for sk, sm in CMA_SECTIONS.items()
    }

    all_fys:  list[str] = []
    warnings: list[str] = []
    docs_out: list[dict] = []

    for doc in doc_results:
        doc_id       = doc["doc_id"]
        filename     = doc["filename"]
        primary_fy   = doc.get("primary_fy") or "unknown"
        secondary_fy = doc.get("secondary_fy") or "unknown"
        status       = doc.get("status", "unknown")
        extraction   = doc.get("extraction")

        fields_found = 0
        if extraction:
            fields_found = extraction.get("meta", {}).get("fields_found", 0)

        docs_out.append({
            "doc_id":       doc_id,
            "filename":     filename,
            "primary_fy":   primary_fy,
            "secondary_fy": secondary_fy,
            "status":       status,
            "fields_found": fields_found,
        })

        if not extraction or status != "success":
            warnings.append(f"{filename}: skipped ({status})")
            continue

        if primary_fy != "unknown":
            all_fys.append(primary_fy)
        if secondary_fy != "unknown":
            all_fys.append(secondary_fy)

        for sk, section_data in extraction.get("sections", {}).items():
            if sk not in cma_data:
                continue
            for field_name, fv in section_data.get("fields", {}).items():
                if field_name not in cma_data[sk]["fields"]:
                    continue

                bucket = cma_data[sk]["fields"][field_name]

                def _try_set(fy: str, is_primary: bool):
                    if not fy or fy == "unknown":
                        return
                    candidate = {
                        "value":           fv.get("value"),
                        "confidence":      fv.get("confidence"),
                        "evidence":        fv.get("evidence"),
                        "page":            fv.get("page"),
                        "source_doc_id":   doc_id,
                        "source_filename": filename,
                        "is_primary":      is_primary,
                    }
                    if candidate["value"] is None:
                        return
                    existing = bucket.get(fy)
                    if existing is None or _better(existing, candidate, is_primary):
                        bucket[fy] = candidate

                _try_set(primary_fy,   is_primary=True)
                _try_set(secondary_fy, is_primary=False)

    financial_years = sorted(set(fy for fy in all_fys if fy != "unknown"), key=_fy_sort_key)

    # Fill missing year slots with empty so FE always gets consistent shape
    for sk in cma_data:
        for field_name in cma_data[sk]["fields"]:
            bucket = cma_data[sk]["fields"][field_name]
            for fy in financial_years:
                if fy not in bucket:
                    bucket[fy] = _empty_entry()

    return {
        "company_slug":    company_slug,
        "company_name":    company_name,
        "financial_years": financial_years,
        "documents":       docs_out,
        "cma_data":        cma_data,
        "warnings":        warnings,
    }