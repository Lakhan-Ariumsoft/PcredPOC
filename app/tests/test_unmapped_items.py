from app.services.cma_extraction_service import find_unmapped_candidates
from app.services.merger import merge_documents


# ── find_unmapped_candidates ────────────────────────────────────────────────────

def test_finds_candidate_with_no_matching_field():
    candidates = [
        {"label": "Some Extremely Unusual Line Item", "page_number": 4,
         "current_year_value": 55.5, "previous_year_value": 44.4},
    ]
    unmapped = find_unmapped_candidates(candidates)
    assert len(unmapped) == 1
    assert unmapped[0]["label"] == "Some Extremely Unusual Line Item"
    assert unmapped[0]["page"] == 4


def test_excludes_candidate_matching_known_field_name():
    candidates = [
        {"label": "Net Sales", "page_number": 2, "current_year_value": 100.0, "previous_year_value": 90.0},
    ]
    assert find_unmapped_candidates(candidates) == []


def test_excludes_candidate_matching_known_search_term():
    # "Revenue from Operations" is a known FIELD_SEARCH_TERMS alias for Net Sales.
    candidates = [
        {"label": "Revenue from Operations", "page_number": 2,
         "current_year_value": 100.0, "previous_year_value": 90.0},
    ]
    assert find_unmapped_candidates(candidates) == []


def test_excludes_candidate_with_no_values():
    candidates = [
        {"label": "Some Header With No Numbers", "page_number": 1,
         "current_year_value": None, "previous_year_value": None},
    ]
    assert find_unmapped_candidates(candidates) == []


def test_deduplicates_by_label():
    candidates = [
        {"label": "Some Extremely Unusual Line Item", "page_number": 4,
         "current_year_value": 55.5, "previous_year_value": None},
        {"label": "some extremely unusual line item", "page_number": 9,
         "current_year_value": 55.5, "previous_year_value": None},
    ]
    unmapped = find_unmapped_candidates(candidates)
    assert len(unmapped) == 1


def test_uses_learned_terms_to_suppress_known_matches():
    candidates = [
        {"label": "Zzz Totally Custom Marker Phrase", "page_number": 3,
         "current_year_value": 10.0, "previous_year_value": None},
    ]
    assert len(find_unmapped_candidates(candidates)) == 1

    learned = {"field_terms": {"Capital": ["zzz totally custom marker phrase"]}, "section_keywords": {}}
    assert find_unmapped_candidates(candidates, learned) == []


# ── merger.py carries unmapped_items through ────────────────────────────────────

def test_merge_documents_aggregates_unmapped_items():
    doc_results = [
        {
            "doc_id": "d1", "filename": "doc1.pdf",
            "current_fy": "2023-24", "previous_fy": "2022-23", "status": "success",
            "extraction": {
                "sections": {},
                "unmapped_items": [
                    {"label": "Custom Item A", "page": 3, "current_value": 1.0, "previous_value": None},
                ],
            },
        },
        {
            "doc_id": "d2", "filename": "doc2.pdf",
            "current_fy": "2024-25", "previous_fy": "2023-24", "status": "success",
            "extraction": {
                "sections": {},
                "unmapped_items": [
                    {"label": "Custom Item A", "page": 5, "current_value": 2.0, "previous_value": None},
                    {"label": "Custom Item B", "page": 1, "current_value": 3.0, "previous_value": None},
                ],
            },
        },
    ]
    merged = merge_documents("acme", "Acme", doc_results)
    labels = [i["label"] for i in merged["unmapped_items"]]
    assert labels == ["Custom Item A", "Custom Item B"]
    assert merged["unmapped_items"][0]["source_filename"] == "doc1.pdf"


def test_merge_documents_skips_unmapped_items_from_failed_docs():
    doc_results = [
        {
            "doc_id": "d1", "filename": "doc1.pdf",
            "current_fy": "2023-24", "previous_fy": "2022-23", "status": "error: boom",
            "extraction": None,
        },
    ]
    merged = merge_documents("acme", "Acme", doc_results)
    assert merged["unmapped_items"] == []
