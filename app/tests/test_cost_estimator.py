from app.services.cma_extraction_service import (
    estimate_pipeline_cost,
    OCR_INPUT_TOKENS_PER_PAGE,
    OCR_OUTPUT_TOKENS_PER_PAGE,
    EXTRACTION_CALLS_PER_DOCUMENT_ESTIMATE,
)


def test_estimate_scales_ocr_tokens_with_total_pages():
    est = estimate_pipeline_cost([10, 20], "gpt-4o-mini", "gpt-4o-mini")
    assert est["total_pages"] == 30
    assert est["estimated_tokens"]["ocr_input"] == 30 * OCR_INPUT_TOKENS_PER_PAGE
    assert est["estimated_tokens"]["ocr_output"] == 30 * OCR_OUTPUT_TOKENS_PER_PAGE


def test_estimate_scales_extraction_calls_with_document_count_not_pages():
    # Extraction is bounded by section/field count per document, not by how
    # many pages that document has.
    est_one_big_doc = estimate_pipeline_cost([100], "gpt-4o-mini", "gpt-4o-mini")
    est_two_small_docs = estimate_pipeline_cost([10, 10], "gpt-4o-mini", "gpt-4o-mini")

    assert est_one_big_doc["estimated_extraction_calls"] == EXTRACTION_CALLS_PER_DOCUMENT_ESTIMATE
    assert est_two_small_docs["estimated_extraction_calls"] == 2 * EXTRACTION_CALLS_PER_DOCUMENT_ESTIMATE


def test_estimate_computes_known_model_pricing():
    est = estimate_pipeline_cost([1], "gpt-4o-mini", "gpt-4o-mini")
    assert est["estimated_cost_usd"]["ocr"] is not None
    assert est["estimated_cost_usd"]["extraction"] is not None
    assert est["estimated_cost_usd"]["total"] == round(
        est["estimated_cost_usd"]["ocr"] + est["estimated_cost_usd"]["extraction"], 4
    )
    assert est["pricing_available_for"]["docling_model"] is True
    assert est["pricing_available_for"]["extraction_model"] is True


def test_estimate_returns_none_cost_for_unknown_model():
    est = estimate_pipeline_cost([5], "Qwen/Qwen2.5-VL-7B-Instruct", "Qwen/Qwen2.5-VL-7B-Instruct")
    assert est["estimated_cost_usd"]["ocr"] is None
    assert est["estimated_cost_usd"]["extraction"] is None
    assert est["estimated_cost_usd"]["total"] is None
    assert est["pricing_available_for"]["docling_model"] is False


def test_estimate_handles_empty_document_list():
    est = estimate_pipeline_cost([], "gpt-4o-mini", "gpt-4o-mini")
    assert est["documents"] == 0
    assert est["total_pages"] == 0
    assert est["estimated_extraction_calls"] == 0
