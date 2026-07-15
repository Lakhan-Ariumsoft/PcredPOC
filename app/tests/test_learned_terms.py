from pathlib import Path

import pytest

from app.services import cma_extraction_service as svc


@pytest.fixture(autouse=True)
def isolated_learned_terms(tmp_path, monkeypatch):
    """
    learned_terms.json is a single shared file — point every test at a
    private tmp_path copy so tests can't see each other's learned state or
    pollute the real on-disk store used by actual extraction runs.
    """
    path = tmp_path / "learned_terms.json"
    monkeypatch.setattr(svc, "_learned_terms_path", lambda: path)
    yield path


# ── Candidate term extraction from evidence text ──────────────────────────────

def test_extract_candidate_term_strips_numbers_and_pipes():
    term = svc._extract_candidate_term("Freight & Handling Income | 45,230.18 | 38,910.43")
    assert term == "freight & handling income"


def test_extract_candidate_term_strips_page_prefix():
    term = svc._extract_candidate_term("[Page 12] Partners' Current Account 1,234.56")
    assert term == "partners' current account 1,234.56" or term.startswith("partners' current account")


def test_extract_candidate_term_rejects_too_short():
    assert svc._extract_candidate_term("12 | 34") is None


def test_extract_candidate_term_rejects_empty():
    assert svc._extract_candidate_term("") is None
    assert svc._extract_candidate_term(None) is None


def test_extract_candidate_term_rejects_pure_numeric_label():
    # No letters at all in the label portion — not a usable term.
    assert svc._extract_candidate_term("12345 | 1,000.00") is None


# ── learn_from_extraction ──────────────────────────────────────────────────────

def _sections_with_field(field_name: str, section_key: str, confidence: float, evidence: str) -> dict:
    return {
        section_key: {
            "label": section_key,
            "fields": {
                field_name: {
                    "current": {"value": 100.0, "confidence": confidence, "evidence": evidence, "page": 3},
                    "previous": {"value": None, "confidence": 0, "evidence": "not found", "page": None},
                }
            },
        }
    }


def test_learn_from_extraction_persists_high_confidence_term():
    sections = _sections_with_field(
        "Net Sales", "sales", 0.97, "Freight & Handling Income | 45,230.18 | 38,910.43"
    )
    svc.learn_from_extraction(sections)

    learned = svc.load_learned_terms()
    assert "freight & handling income" in [t.lower() for t in learned["field_terms"].get("Net Sales", [])]
    assert "freight & handling income" in [t.lower() for t in learned["section_keywords"].get("sales", [])]


def test_learn_from_extraction_skips_low_confidence():
    sections = _sections_with_field(
        "Net Sales", "sales", 0.80, "Some Uncertain Label | 45,230.18"
    )
    svc.learn_from_extraction(sections)

    learned = svc.load_learned_terms()
    assert learned["field_terms"] == {}


def test_learn_from_extraction_does_not_duplicate_existing_search_terms():
    # "revenue from operations" is already a known FIELD_SEARCH_TERMS entry
    # for Net Sales — learning it again should be a no-op, not a duplicate.
    sections = _sections_with_field(
        "Net Sales", "sales", 0.99, "Revenue from Operations | 45,230.18"
    )
    svc.learn_from_extraction(sections)

    learned = svc.load_learned_terms()
    assert learned["field_terms"] == {}


def test_learned_terms_persist_across_loads():
    sections = _sections_with_field(
        "Capital", "net_worth", 0.96, "Partners' Fixed Capital Account | 5,000.00"
    )
    svc.learn_from_extraction(sections)

    reloaded = svc.load_learned_terms()
    assert "partners' fixed capital account" in [t.lower() for t in reloaded["field_terms"].get("Capital", [])]


# ── Learned terms actually change routing behavior ─────────────────────────────

def test_chunk_relevance_uses_learned_section_keywords():
    learned = {"section_keywords": {"sales": ["zzz-custom-marker"]}, "field_terms": {}}
    score_without = svc._chunk_relevance("this chunk has zzz-custom-marker in it", "sales", learned=None)
    score_with = svc._chunk_relevance("this chunk has zzz-custom-marker in it", "sales", learned=learned)
    assert score_without == 0
    assert score_with > 0


def test_find_field_snippets_uses_learned_field_terms():
    pages = [{"page": 1, "text": "Some Header\nzzz-custom-marker 123.45\nFooter"}]
    learned = {"field_terms": {"Net Sales": ["zzz-custom-marker"]}, "section_keywords": {}}

    without = svc._find_field_snippets(pages, ["Net Sales"], learned=None)
    with_learned = svc._find_field_snippets(pages, ["Net Sales"], learned=learned)

    assert "Net Sales" not in without
    assert "Net Sales" in with_learned
