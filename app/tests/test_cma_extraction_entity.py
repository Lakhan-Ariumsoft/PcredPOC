import uuid

import pytest

from app.services import cma_extraction_service as svc


# ── Page trimming ────────────────────────────────────────────────────────────

def test_trim_pages_no_range_returns_all():
    pages = [{"page": i, "text": f"p{i}"} for i in range(1, 6)]
    assert svc.trim_pages(pages, None, None) == pages


def test_trim_pages_restricts_to_range():
    pages = [{"page": i, "text": f"p{i}"} for i in range(1, 11)]
    trimmed = svc.trim_pages(pages, 3, 5)
    assert [p["page"] for p in trimmed] == [3, 4, 5]


def test_trim_pages_open_ended_start_only():
    pages = [{"page": i, "text": f"p{i}"} for i in range(1, 6)]
    trimmed = svc.trim_pages(pages, 3, None)
    assert [p["page"] for p in trimmed] == [3, 4, 5]


def test_trim_pages_range_matches_nothing_falls_back_to_full_doc():
    pages = [{"page": i, "text": f"p{i}"} for i in range(1, 6)]
    trimmed = svc.trim_pages(pages, 50, 60)
    assert trimmed == pages


# ── Entity context / prompt hints ────────────────────────────────────────────

def test_build_entity_context_private_limited_has_no_extra_guidance():
    ctx = svc.build_entity_context("private_limited")
    assert "Private Limited Company" in ctx
    assert "Partners'" not in ctx


def test_build_entity_context_llp_includes_capital_guidance():
    ctx = svc.build_entity_context("llp")
    assert "Limited Liability Partnership (LLP)" in ctx
    assert "Partners' Capital Account" in ctx


def test_build_entity_context_includes_notes():
    ctx = svc.build_entity_context("partnership", "Balance Sheet on page 4, P&L on page 5")
    assert "Balance Sheet on page 4" in ctx
    assert "UPLOADER NOTES" in ctx


def test_build_entity_context_blank_notes_omitted():
    ctx = svc.build_entity_context("llp", "   ")
    assert "UPLOADER NOTES" not in ctx


def test_build_entity_context_unknown_entity_type_falls_back_to_other_label():
    ctx = svc.build_entity_context("not_a_real_type")
    assert "Unspecified entity type" in ctx


# ── Synonym / keyword overlays ───────────────────────────────────────────────

def test_filtered_synonyms_includes_entity_overlay_for_capital_field():
    base = svc._get_filtered_synonyms(["Capital"], "private_limited")
    llp = svc._get_filtered_synonyms(["Capital"], "llp")
    assert "Partners' Capital Account" not in base
    assert "Partners' Capital Account" in llp


def test_chunk_relevance_scores_higher_with_entity_keywords():
    chunk = "Partners' Capital Account balance as at year end 123,456"
    base_score = svc._chunk_relevance(chunk, "net_worth", "private_limited")
    llp_score = svc._chunk_relevance(chunk, "net_worth", "llp")
    assert llp_score > base_score


def test_find_field_snippets_uses_entity_search_terms():
    pages = [{"page": 1, "text": "Some intro text\nPartners' Current Account balance 500 400\nMore text"}]
    base = svc._find_field_snippets(pages, ["Other reserves"], "private_limited")
    llp = svc._find_field_snippets(pages, ["Other reserves"], "llp")
    assert "Other reserves" not in base
    assert "Other reserves" in llp


# ── Chunk-routing fallback ───────────────────────────────────────────────────

def test_spread_fallback_indices_covers_full_range():
    idx = svc._spread_fallback_indices(20, 5)
    assert len(idx) == 5
    assert max(idx) < 20
    assert min(idx) == 0


def test_spread_fallback_indices_returns_all_when_fewer_chunks_than_k():
    idx = svc._spread_fallback_indices(3, 8)
    assert idx == {0, 1, 2}


def test_get_relevant_chunks_falls_back_to_spread_not_just_first_two():
    # None of these chunks contain any "sales" keywords, so keyword routing
    # finds nothing — it must fall back to a spread sample across the whole
    # document, not just chunks 0/1 (which are rarely the real tables).
    chunks = [
        f"=== Page {i} ===\nUnrelated LLP prose with no financial keywords at all."
        for i in range(1, 21)
    ]
    relevant = svc._get_relevant_chunks(chunks, "sales", "llp", top_k=3)
    assert len(relevant) > 2
    assert relevant != chunks[:2]


def test_get_relevant_chunks_prioritizes_keyword_matches_when_present():
    chunks = ["irrelevant filler text"] * 10
    chunks[7] = "Revenue from Operations 45,230.18 38,910.43 net sales turnover"
    relevant = svc._get_relevant_chunks(chunks, "sales", "private_limited", top_k=3)
    assert chunks[7] in relevant


# ── _openai_call prompt construction ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_call_prompt_includes_entity_context(monkeypatch):
    captured = {}

    class FakeAdapter:
        async def chat(self, messages, temperature=0.0):
            captured["prompt"] = messages[1]["content"]
            return (
                '{"Capital": {"current": {"value": 100, "confidence": 0.9, '
                '"evidence": "x", "page": 1}, "previous": {"value": 90, '
                '"confidence": 0.9, "evidence": "y", "page": 1}}}'
            )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(svc, "get_llm_adapter", lambda: FakeAdapter())

    result = await svc._openai_call(
        "Net Worth", ["Capital"], "some document text",
        "2024-25", "2023-24", "Lakhs",
        entity_type="llp",
    )
    assert result["Capital"]["current"]["value"] == 100
    assert "Partners' Capital Account" in captured["prompt"]
    assert "ENTITY TYPE: Limited Liability Partnership" in captured["prompt"]


# ── End-to-end extract_cma_fields ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_cma_fields_end_to_end_includes_entity_metadata(monkeypatch, tmp_path):
    class EmptyAdapter:
        async def chat(self, messages, temperature=0.0):
            return "{}"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(svc, "get_llm_adapter", lambda: EmptyAdapter())
    monkeypatch.setattr(svc, "_ai_cache_dir", lambda: tmp_path)

    pages = [
        {"page": 1, "text": "Cover page, nothing relevant"},
        {"page": 2, "text": "Partners' Capital Account 500 400\nPartners' Current Account 100 80"},
        {"page": 3, "text": "Revenue from Operations 1000 900"},
    ]

    result = await svc.extract_cma_fields(
        pages, source_file="llp_sample.pdf", doc_id=f"test-{uuid.uuid4().hex}",
        current_fy="2024-25", previous_fy="2023-24",
        entity_type="llp", start_page=1, end_page=3, notes="test notes",
    )

    assert result["meta"]["entity_type"] == "llp"
    assert result["meta"]["start_page"] == 1
    assert result["meta"]["end_page"] == 3
    assert result["meta"]["notes"] == "test notes"
    assert "sections" in result


@pytest.mark.asyncio
async def test_extract_cma_fields_applies_page_trim(monkeypatch, tmp_path):
    class EmptyAdapter:
        async def chat(self, messages, temperature=0.0):
            return "{}"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(svc, "get_llm_adapter", lambda: EmptyAdapter())
    monkeypatch.setattr(svc, "_ai_cache_dir", lambda: tmp_path)

    pages = [{"page": i, "text": f"page {i} content"} for i in range(1, 11)]
    result = await svc.extract_cma_fields(
        pages, doc_id=f"test-{uuid.uuid4().hex}",
        start_page=3, end_page=5,
    )
    assert result["meta"]["total_pages"] == 3


@pytest.mark.asyncio
async def test_extract_cma_fields_cache_invalidated_by_entity_type_change(monkeypatch, tmp_path):
    call_count = {"n": 0}

    class CountingAdapter:
        async def chat(self, messages, temperature=0.0):
            call_count["n"] += 1
            return "{}"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(svc, "get_llm_adapter", lambda: CountingAdapter())
    monkeypatch.setattr(svc, "_ai_cache_dir", lambda: tmp_path)

    pages = [{"page": 1, "text": "Revenue from Operations 1000 900"}]
    doc_id = f"test-{uuid.uuid4().hex}"

    r1 = await svc.extract_cma_fields(pages, doc_id=doc_id, entity_type="private_limited")
    calls_after_first = call_count["n"]
    assert calls_after_first > 0

    # Same entity_type on the same doc_id -> cache hit, no new LLM calls.
    r2 = await svc.extract_cma_fields(pages, doc_id=doc_id, entity_type="private_limited")
    assert call_count["n"] == calls_after_first
    assert r2["meta"]["entity_type"] == r1["meta"]["entity_type"]

    # Different entity_type on the same doc_id -> cache must NOT be reused,
    # since the extraction hints changed.
    r3 = await svc.extract_cma_fields(pages, doc_id=doc_id, entity_type="llp")
    assert call_count["n"] > calls_after_first
    assert r3["meta"]["entity_type"] == "llp"
