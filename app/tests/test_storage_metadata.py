import uuid

from filelock import FileLock

from app.services.storage import (
    LOCK_FILE,
    _read_registry,
    _write_registry,
    delete_document,
    get_document_meta,
    list_all_documents,
    list_company_documents,
    store_document,
)


def _pdf_bytes(tag: str) -> bytes:
    return f"%PDF-1.4 test content {tag}".encode()


def _unique_company(prefix: str = "Metadata Test Co") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def test_store_document_persists_entity_metadata():
    meta = store_document(
        _unique_company(), "financials.pdf", _pdf_bytes("a"),
        entity_type="llp", start_page=3, end_page=9, notes="BS on pg 4",
    )
    try:
        assert meta["entity_type"] == "llp"
        assert meta["start_page"] == 3
        assert meta["end_page"] == 9
        assert meta["notes"] == "BS on pg 4"

        fetched = get_document_meta(meta["company_slug"], meta["doc_id"])
        assert fetched is not None
        assert fetched["entity_type"] == "llp"
        assert fetched["start_page"] == 3
        assert fetched["end_page"] == 9
        assert fetched["notes"] == "BS on pg 4"
    finally:
        delete_document(meta["company_slug"], meta["doc_id"])


def test_store_document_without_metadata_defaults_to_none():
    meta = store_document(_unique_company(), "financials.pdf", _pdf_bytes("b"))
    try:
        assert meta["entity_type"] is None
        assert meta["start_page"] is None
        assert meta["end_page"] is None
        assert meta["notes"] is None
    finally:
        delete_document(meta["company_slug"], meta["doc_id"])


def test_store_document_blank_notes_normalized_to_none():
    meta = store_document(_unique_company(), "financials.pdf", _pdf_bytes("c"), notes="   ")
    try:
        assert meta["notes"] is None
    finally:
        delete_document(meta["company_slug"], meta["doc_id"])


def test_get_document_meta_missing_doc_returns_none():
    assert get_document_meta("nonexistent-company-slug", "nonexistent-doc") is None


def test_list_functions_surface_metadata_fields():
    meta = store_document(
        _unique_company(), "financials.pdf", _pdf_bytes("d"),
        entity_type="partnership", start_page=1, end_page=5, notes="see note",
    )
    try:
        company_docs = list_company_documents(meta["company_slug"])
        doc_entry = next(d for d in company_docs["documents"] if d["doc_id"] == meta["doc_id"])
        assert doc_entry["entity_type"] == "partnership"
        assert doc_entry["start_page"] == 1
        assert doc_entry["end_page"] == 5
        assert doc_entry["notes"] == "see note"

        all_docs = list_all_documents()
        company_out = next(c for c in all_docs["companies"] if c["slug"] == meta["company_slug"])
        doc_entry_all = next(d for d in company_out["documents"] if d["doc_id"] == meta["doc_id"])
        assert doc_entry_all["entity_type"] == "partnership"
        assert doc_entry_all["notes"] == "see note"
    finally:
        delete_document(meta["company_slug"], meta["doc_id"])


def test_lookups_are_case_insensitive_on_company_slug():
    """
    _slugify() always lowercases on store, but a company_slug arriving
    later as a URL path parameter is passed through exactly as typed.
    Every lookup function must match regardless of case.
    """
    meta = store_document(_unique_company("Charbhuja"), "financials.pdf", _pdf_bytes("e"))
    slug = meta["company_slug"]  # already lowercase, e.g. "charbhuja-a1b2c3d4"
    try:
        for variant in (slug.upper(), slug.title(), f"  {slug.upper()}  "):
            fetched = get_document_meta(variant, meta["doc_id"])
            assert fetched is not None, f"get_document_meta missed case variant: {variant!r}"

            listing = list_company_documents(variant)
            assert listing is not None, f"list_company_documents missed case variant: {variant!r}"
            assert listing["slug"] == slug
    finally:
        # deletion must also be case-insensitive
        assert delete_document(slug.upper(), meta["doc_id"]) is True


def test_list_functions_handle_legacy_entries_without_metadata_keys():
    """
    Registry entries written before this feature existed won't have the
    entity_type/start_page/end_page/notes keys. list_* must not KeyError on
    them — it should surface None for each missing field instead.
    """
    slug = f"legacy-test-co-{uuid.uuid4().hex[:8]}"
    doc_id = uuid.uuid4().hex
    with FileLock(str(LOCK_FILE)):
        registry = _read_registry()
        registry["companies"][slug] = {
            "slug": slug,
            "display_name": "Legacy Test Co",
            "created_at": "2020-01-01T00:00:00+00:00",
            "documents": [{
                "doc_id": doc_id,
                "filename": "old.pdf",
                "stored_name": f"{doc_id}_old.pdf",
                "size_bytes": 10,
                "sha256": "deadbeef",
                "uploaded_at": "2020-01-01T00:00:00+00:00",
                # no entity_type/start_page/end_page/notes keys on purpose
            }],
        }
        _write_registry(registry)
    try:
        result = list_company_documents(slug)
        assert result is not None
        doc_entry = result["documents"][0]
        assert doc_entry["entity_type"] is None
        assert doc_entry["start_page"] is None
        assert doc_entry["end_page"] is None
        assert doc_entry["notes"] is None

        meta = get_document_meta(slug, doc_id)
        assert meta is not None
        assert meta.get("entity_type") is None
    finally:
        with FileLock(str(LOCK_FILE)):
            registry = _read_registry()
            registry["companies"].pop(slug, None)
            _write_registry(registry)
