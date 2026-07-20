"""
CMA extraction service using LlmAdapter.
Chunks document text into overlapping windows and routes sections to relevant chunks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import asyncio
from pathlib import Path
from typing import Optional

from app.exceptions import LLMQuotaExceededError
from app.schemas.cma_fields import CMA_SECTIONS, CMA_FIELDS_FLAT
from app.services.client_factory import get_llm_adapter
from app.utils.json_tools import parse_json_object
logger = logging.getLogger(__name__)

# Bumped because chunk-routing fallback, entity-type overlays, and page
# trimming changed extraction behavior — old cached results used the
# narrower pre-v17 logic and must not be served as if they still apply.
CACHE_VERSION = "v17"
CHUNK_SIZE    = 6000
CHUNK_OVERLAP = 1500
SPLIT_AT      = 4
DEFAULT_ENTITY_TYPE = "private_limited"

# ── Cache helpers ──────────────────────────────────────────────────────────────

def _ai_cache_dir() -> Path:
    settings = get_settings_for_cache()
    d = settings.output_dir / "ai_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_settings_for_cache():
    from app.core_config import get_settings
    return get_settings()

def _extraction_fingerprint(entity_type: str, start_page: Optional[int], end_page: Optional[int], notes: str) -> str:
    """
    Identifies the set of extraction hints used for a run. Cached results are
    only reused if the uploader's entity_type/page-range/notes haven't
    changed since the cache was written — otherwise a stale extraction from
    a different (or absent) hint set would silently be served.
    """
    raw = f"{entity_type}|{start_page}|{end_page}|{(notes or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def _load_ai_cache(doc_id: str, fingerprint: str = "") -> Optional[dict]:
    if not doc_id:
        return None
    try:
        cp = _ai_cache_dir() / f"{doc_id}.json"
        if cp.exists():
            data = json.loads(cp.read_text())
            if data.get("_cache_version") == CACHE_VERSION and data.get("_fingerprint", "") == fingerprint:
                logger.info(f"AI cache hit: {doc_id}")
                return data
            logger.info(f"Cache stale for {doc_id} (version or extraction hints changed) — re-extracting")
    except Exception:
        pass
    return None

def _save_ai_cache(doc_id: str, data: dict, fingerprint: str = "") -> None:
    if not doc_id:
        return
    try:
        data["_cache_version"] = CACHE_VERSION
        data["_fingerprint"] = fingerprint
        (_ai_cache_dir() / f"{doc_id}.json").write_text(json.dumps(data))
    except Exception as e:
        logger.warning(f"AI cache save failed: {e}")

def clear_ai_cache(doc_id: str) -> bool:
    try:
        cp = _ai_cache_dir() / f"{doc_id}.json"
        if cp.exists():
            cp.unlink()
            return True
    except Exception:
        pass
    return False

# ── Self-learning term store ────────────────────────────────────────────────
# Fully automatic (no human review step — chosen explicitly): after every
# extraction run, any field found with very high confidence has its evidence
# label mined for a candidate synonym/keyword term. If new, it's persisted
# to disk and merged into keyword routing + targeted search for every future
# run — the system gets better at recognizing unfamiliar formats over time
# without any model fine-tuning happening (every LLM call stays stateless;
# this only grows the deterministic keyword/synonym dictionaries around it).
#
# Risk of full automation: one mislabeled high-confidence match can pollute
# routing for every future extraction with no one catching it. Mitigated
# (not eliminated) by requiring MIN_LEARN_CONFIDENCE and sane term-length
# bounds before anything is learned.

LEARNED_TERMS_VERSION = "v1"
MIN_LEARN_CONFIDENCE = 0.95

def _learned_terms_path() -> Path:
    settings = get_settings_for_cache()
    return settings.output_dir / "learned_terms.json"

def load_learned_terms() -> dict:
    """
    Re-read from disk on every call (not cached at import) so learning from
    one extraction is visible to the very next one within the same
    long-running server process.
    """
    try:
        p = _learned_terms_path()
        if p.exists():
            data = json.loads(p.read_text())
            if data.get("_version") == LEARNED_TERMS_VERSION:
                return data
    except Exception as e:
        logger.warning(f"Failed to load learned terms: {e}")
    return {"_version": LEARNED_TERMS_VERSION, "field_terms": {}, "section_keywords": {}}

def _save_learned_terms(data: dict) -> None:
    try:
        data["_version"] = LEARNED_TERMS_VERSION
        _learned_terms_path().write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save learned terms: {e}")

_EVIDENCE_LABEL_RE = re.compile(r"^\[page \d+\]\s*", re.IGNORECASE)

def _extract_candidate_term(evidence: str) -> Optional[str]:
    """
    Pulls the textual label portion out of an evidence quote, e.g.
    "Freight & Handling Income | 45,230.18 | 38,910.43" -> "freight & handling income".
    Returns None if there's nothing usable (empty, too short/long, or
    evidence that's just a bare number with no label).
    """
    if not evidence:
        return None
    text = _EVIDENCE_LABEL_RE.sub("", evidence.strip())
    label = text.split("|")[0].strip(" -:\t").lower()
    label = re.sub(r"\s+", " ", label)
    if len(label) < 4 or len(label) > 80:
        return None
    if not re.search(r"[a-z]{3,}", label):
        return None
    return label

def learn_from_extraction(sections_out: dict) -> None:
    """
    Scans a completed extraction's results for very-high-confidence fields
    and grows the persistent learned-term store with any new label wording
    found in their evidence. See module-level comment above for the
    automatic-vs-reviewed tradeoff.
    """
    learned = load_learned_terms()
    field_terms = learned.setdefault("field_terms", {})
    section_kw = learned.setdefault("section_keywords", {})
    changed = False

    for section_key, section_data in sections_out.items():
        for field_name, entry in section_data.get("fields", {}).items():
            for year_key in ("current", "previous"):
                y = entry.get(year_key, {}) if isinstance(entry, dict) else {}
                conf = float(y.get("confidence") or 0)
                if conf < MIN_LEARN_CONFIDENCE:
                    continue
                term = _extract_candidate_term(y.get("evidence", ""))
                if not term or term == field_name.lower():
                    continue

                existing = {t.lower() for t in FIELD_SEARCH_TERMS.get(field_name, [])}
                existing |= {t.lower() for t in field_terms.get(field_name, [])}
                if term in existing:
                    continue

                field_terms.setdefault(field_name, []).append(term)
                bucket = section_kw.setdefault(section_key, [])
                if term not in [t.lower() for t in bucket]:
                    bucket.append(term)
                changed = True
                logger.info(f"Learned new term for '{field_name}' ({section_key}): '{term}'")

    if changed:
        _save_learned_terms(learned)


def clear_all_ai_caches() -> int:
    count = 0
    try:
        for f in _ai_cache_dir().glob("*.json"):
            f.unlink()
            count += 1
    except Exception as e:
        logger.warning(f"AI cache clear failed: {e}")
    return count


# ── Unit detection ─────────────────────────────────────────────────────────────

def detect_unit(pages: list[dict]) -> str:
    """
    Detect whether the document reports figures in Lakhs, Crores, or Thousands.
    Scans the first 15 pages where the unit declaration typically appears.
    """
    sample = " ".join(p.get("text", "") for p in pages[:15]).lower()
    crore_signals  = ["in crores", "rs. in crores", "₹ in crores", "rupees in crores",
                      "(rs. crores)", "(₹ crores)", "amount in crore", "figures in crore"]
    lakh_signals   = ["in lakhs", "rs. in lakhs", "₹ in lakhs", "rupees in lakhs",
                      "(rs. lakhs)", "(₹ lakhs)", "amount in lakh", "figures in lakh"]
    thous_signals  = ["in thousands", "rs. in thousands", "₹ in thousands"]

    if any(x in sample for x in crore_signals):
        return "Crores"
    if any(x in sample for x in thous_signals):
        return "Thousands"
    if any(x in sample for x in lakh_signals):
        return "Lakhs"
    return "Lakhs"  # default


# ── Page range trimming ─────────────────────────────────────────────────────────

def trim_pages(pages: list[dict], start_page: Optional[int], end_page: Optional[int]) -> list[dict]:
    """
    Restrict extraction to the uploader-specified page range (e.g. "Balance
    Sheet and P&L are on pages 4-9"). This both cuts noise out of chunking
    (fewer irrelevant chunks competing for the top-k slots per section) and
    scopes unit/FY detection to the pages that actually matter.

    Falls back to the full page set if the requested range matches nothing,
    since a bad page range should degrade to "no hint given", not to zero
    extractable text.
    """
    if start_page is None and end_page is None:
        return pages
    lo = start_page or 1
    hi = end_page if end_page is not None else max((p["page"] for p in pages), default=lo)
    trimmed = [p for p in pages if lo <= p["page"] <= hi]
    if not trimmed:
        logger.warning(f"Page range ({start_page},{end_page}) matched no pages — using full document")
        return pages
    return trimmed


# ── Entity-type overlays ─────────────────────────────────────────────────────────
# Pvt/Public Ltd companies report under Schedule III / Ind AS with "Share
# Capital" and "Reserves and Surplus". LLPs, partnerships, proprietorships,
# and HUFs use materially different capital-structure vocabulary and often
# aren't in Schedule III format at all. Without these overlays, keyword-based
# chunk routing and synonym matching silently fail on every non-company
# document because none of the base vocabulary ever appears in the text.

ENTITY_TYPE_LABELS: dict[str, str] = {
    "private_limited": "Private Limited Company",
    "public_limited":  "Public Limited Company",
    "llp":             "Limited Liability Partnership (LLP)",
    "partnership":     "Partnership Firm",
    "proprietorship":  "Sole Proprietorship",
    "huf":             "Hindu Undivided Family (HUF)",
    "other":           "Unspecified entity type",
}

ENTITY_TYPE_GUIDANCE: dict[str, str] = {
    "llp": (
        "This is an LLP. There is NO 'Share Capital' or 'Equity Share Capital' line. "
        "Use 'Partners' Capital Account' / 'Partners' Fixed Capital' for the Capital field, and "
        "'Partners' Current Account' for Other reserves / Net Worth build-up."
    ),
    "partnership": (
        "This is a Partnership Firm. There is NO 'Share Capital' line. Use 'Partners' Capital Account' "
        "for Capital and 'Partners' Current Account' (net of Drawings) for Other reserves. "
        "The statement may be a simple Balance Sheet / Trading and P&L Account, NOT Schedule III format — "
        "do not expect note-number references."
    ),
    "proprietorship": (
        "This is a Sole Proprietorship. There is NO 'Share Capital' line. Use 'Proprietor's Capital Account' "
        "(opening capital + profit - drawings) for the Capital and Net Worth fields. "
        "The statement is typically a simple Balance Sheet / Trading and P&L Account, NOT Schedule III format."
    ),
    "huf": (
        "This is a Hindu Undivided Family (HUF). Use 'Karta's Capital Account' / 'HUF Capital Account' "
        "for the Capital and Net Worth fields."
    ),
}

ENTITY_TYPE_EXTRA_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "llp": {
        "net_worth": ["partners' capital", "partners capital", "partner's capital account",
                      "partners' current account", "partners current account",
                      "partners' fixed capital", "total partners' funds"],
    },
    "partnership": {
        "net_worth": ["partners' capital", "partners capital account", "partners' current account",
                      "capital account", "drawings"],
    },
    "proprietorship": {
        "net_worth": ["proprietor's capital", "proprietor capital account", "capital account", "drawings"],
    },
    "huf": {
        "net_worth": ["karta's capital", "huf capital account", "capital account"],
    },
}

ENTITY_TYPE_EXTRA_SYNONYMS: dict[str, list[str]] = {
    "llp": [
        "- \"Capital\" = \"Partners' Capital Account\" = \"Partners' Fixed Capital\" = \"Partners Capital\"",
        "- \"Other reserves\" = \"Partners' Current Account\" = \"Partners Current Account Balance\"",
        "- \"Net Worth\" = \"Total Partners' Funds\" = Partners' Capital + Partners' Current Account",
    ],
    "partnership": [
        "- \"Capital\" = \"Partners' Capital Account\" = \"Partners Capital\"",
        "- \"Other reserves\" = \"Partners' Current Account\" = Accumulated Profit less Drawings",
        "- \"Net Worth\" = Partners' Capital + Partners' Current Account - Drawings",
    ],
    "proprietorship": [
        "- \"Capital\" = \"Proprietor's Capital Account\" = \"Capital Account\"",
        "- \"Net Worth\" = \"Proprietor's Capital Account Balance\" (Capital + Profit - Drawings)",
    ],
    "huf": [
        "- \"Capital\" = \"Karta's Capital Account\" = \"HUF Capital Account\"",
        "- \"Net Worth\" = \"HUF Capital Account Balance\"",
    ],
}

ENTITY_TYPE_EXTRA_SEARCH_TERMS: dict[str, dict[str, list[str]]] = {
    "llp": {
        "Capital":       ["partners' capital", "partners capital account", "partners' fixed capital"],
        "Other reserves": ["partners' current account", "partners current account"],
        "Net Worth":     ["total partners' funds", "partners' capital and current account"],
    },
    "partnership": {
        "Capital":       ["partners' capital account", "partners capital"],
        "Other reserves": ["partners' current account", "accumulated profit"],
        "Net Worth":     ["total partners' capital"],
    },
    "proprietorship": {
        "Capital":   ["proprietor's capital account", "capital account"],
        "Net Worth": ["proprietor's capital account balance"],
    },
    "huf": {
        "Capital":   ["karta's capital account", "huf capital account"],
        "Net Worth": ["huf capital account balance"],
    },
}


def build_entity_context(entity_type: str, notes: str = "") -> str:
    """
    Builds the per-document context block injected into the extraction
    prompt: entity-type guidance plus the uploader's free-text notes
    (e.g. "Balance Sheet is on page 4, P&L on page 5").
    """
    entity_type = entity_type or DEFAULT_ENTITY_TYPE
    label = ENTITY_TYPE_LABELS.get(entity_type, ENTITY_TYPE_LABELS["other"])
    lines = [f"ENTITY TYPE: {label}"]
    guidance = ENTITY_TYPE_GUIDANCE.get(entity_type)
    if guidance:
        lines.append(guidance)
    notes = (notes or "").strip()
    if notes:
        lines.append(f"UPLOADER NOTES (treat as authoritative for this document): {notes}")
    return "\n".join(lines)


# ── Chunking ───────────────────────────────────────────────────────────────────

def _build_chunks(pages: list[dict]) -> list[str]:
    """
    Concatenate ALL pages into overlapping text windows.
    """
    ordered = sorted(pages, key=lambda p: p["page"])
    full_text = ""
    for p in ordered:
        full_text += f"\n=== Page {p['page']} ===\n{p['text']}\n"

    if not full_text.strip():
        return []

    chunks = []
    start  = 0
    total  = len(full_text)
    while start < total:
        end = min(start + CHUNK_SIZE, total)
        chunks.append(full_text[start:end])
        if end >= total:
            break
        start = end - CHUNK_OVERLAP

    return chunks


# ── Chunk relevance routing ────────────────────────────────────────────────────

SECTION_KEYWORDS: dict[str, list[str]] = {
    "operating_details": [
        "operating months", "capacity utilization", "installed capacity",
        "production capacity", "operating cycle",
    ],
    "sales": [
        "revenue from operations", "net sales", "turnover", "domestic sale",
        "export sale", "trade discount", "gross sales", "net revenue",
        "sale of products", "sale of services", "note 21", "freight & handling income",
    ],
    "cost_of_sales": [
        "purchases", "raw material", "cost of production", "depreciation",
        "inventories consumed", "manufacturing expenses", "packing", "transport",
        "fuel", "repair", "stores and spares", "godown rent", "stock in process",
        "finished goods", "cost of goods sold", "freight", "operating expenses",
        "vehicle running", "insurance", "handling", "loading", "note 23",
        "packing", "forwarder", "forwarding", "loading & unloading",
    ],
    "profitability": [
        "gross profit", "selling expenses", "administrative expenses",
        "operating profit", "distribution expenses", "employee benefit",
        "rent", "legal", "travelling", "promotion", "miscellaneous", "note 26",
        "note 24", "auditor", "rates & taxes", "business promotion", "employee benefits",
    ],
    "interest": [
        "interest expense", "finance costs", "finance charges",
        "borrowing costs", "interest on cc", "interest on term loan",
        "bank charges", "note 25", "bank interest", "other borrowing costs",
    ],
    "other_income_expenses": [
        "other income", "dividend income", "royalty", "miscellaneous income",
        "non-operating income", "other expenses", "exceptional item",
        "creditors written back", "other interest", "interest income", "note 22",
        "dividend", "rental income", "exchange gain",
    ],
    "pnl": [
        "profit before tax", "profit after tax", "net profit",
        "provision for tax", "deferred tax", "mat credit",
        "total comprehensive income", "pbdit", "ebitda",
        "tax expense", "income tax", "pbt", "pat", "note 2", "profit/(loss)",
    ],
    "current_liabilities": [
        "current liabilities", "trade payables", "sundry creditors",
        "short term borrowings", "current maturities", "other current liabilities",
        "statutory dues", "advance from customers", "provision for tax",
        "note 7", "note 8", "note 9", "note 10", "statutory liabilities",
    ],
    "term_liabilities": [
        "long term borrowings", "term loan", "debentures",
        "non-current liabilities", "long term provisions",
        "unsecured loans", "deferred payment", "note 5", "note 6",
    ],
    "net_worth": [
        "share capital", "reserves and surplus", "shareholders equity",
        "net worth", "equity share capital", "other equity",
        "securities premium", "retained earnings", "general reserve", "share premium",
        "note 3", "note 4",
    ],
    "current_assets": [
        "current assets", "inventories", "trade receivables", "unbilled receivables",
        "cash and cash equivalents", "cash and bank balance", "loans and advances",
        "sundry debtors", "short term investments", "advance to suppliers", "security deposit", "security deposits",
        "note 16", "note 17", "note 18", "note 19", "note 20", "prepaid expenses",
    ],
    "fixed_assets": [
        "fixed assets", "property plant and equipment", "gross block",
        "net block", "capital work in progress", "accumulated depreciation",
        "tangible assets", "schedule of fixed assets", "note 11", "note 12",
        "note 13", "note 14", "note 15", "non-current assets", "deferred tax asset",
    ],
    "intangibles": [
        "intangible assets", "goodwill", "preliminary expenses",
        "deferred revenue expenditure", "total assets", "note 11", "software",
    ],
    "ratios": [
        "current ratio", "debt equity ratio", "net working capital",
        "tangible net worth", "roce", "return on capital", "note 35",
    ],
    "additional_info": [
        "contingent liabilities", "arrears of depreciation",
        "disputed tax", "gratuity liability", "off balance sheet",
        "commitments", "contingencies", "note 28", "contingent liability",
    ],
    "working_capital": [
        "working capital", "nwc", "debtor days", "creditor days",
        "inventory days", "bank finance", "working capital gap",
        "cc limit", "drawing power",
    ],
    "fund_flow": [
        "fund flow", "sources of funds", "application of funds",
        "changes in working capital", "cash flow from financing",
        "proceeds from issue",
    ],
    "break_even": [
        "break even", "contribution margin", "variable cost",
        "fixed cost", "margin of safety", "bep", "p/v ratio",
        "contribution to sales",
    ],
}

def _chunk_relevance(
    chunk: str, section_key: str, entity_type: str = DEFAULT_ENTITY_TYPE,
    learned: Optional[dict] = None,
) -> int:
    keywords = list(SECTION_KEYWORDS.get(section_key, []))
    keywords += ENTITY_TYPE_EXTRA_KEYWORDS.get(entity_type, {}).get(section_key, [])
    keywords += (learned or {}).get("section_keywords", {}).get(section_key, [])
    t = chunk.lower()
    return min(100, sum(8 for kw in keywords if kw in t))

def _spread_fallback_indices(total_chunks: int, k: int) -> set[int]:
    """
    Evenly-spaced chunk indices across the whole document, used when keyword
    routing finds zero matches for a section (typically because the document
    uses vocabulary our keyword lists don't cover yet — an unfamiliar
    format/entity type). The old behavior fell back to chunks 0-1, which are
    almost always the cover page / director's report, not the financial
    statements — that guaranteed a miss. Spreading across the document at
    least samples pages where the real tables are likely to live.
    """
    if total_chunks <= k:
        return set(range(total_chunks))
    step = total_chunks / k
    return {int(i * step) for i in range(k)}

def _get_relevant_chunks(
    chunks: list[str], section_key: str, entity_type: str = DEFAULT_ENTITY_TYPE, top_k: int = 4,
    learned: Optional[dict] = None,
) -> list[str]:
    """
    Return the top-k most relevant chunks for a section (default 4 to increase recall).
    """
    if len(chunks) <= top_k:
        return chunks

    scored = [(i, _chunk_relevance(c, section_key, entity_type, learned)) for i, c in enumerate(chunks)]
    scored.sort(key=lambda x: -x[1])

    top_indices = {i for i, score in scored[:top_k] if score > 0}

    # No chunk matched any keyword — our vocabulary missed this document's
    # format entirely. Sample spread across the document instead of just the
    # first two chunks, and look a bit wider than top_k since we have no
    # signal to prioritize with.
    if not top_indices:
        fallback_k = min(len(chunks), max(top_k * 2, 8))
        top_indices = _spread_fallback_indices(len(chunks), fallback_k)

    return [chunks[i] for i in sorted(top_indices) if i < len(chunks)]


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_MSG = """You are a senior financial analyst specializing in Indian company CMA (Credit Monitoring Arrangement) data extraction from audited financial statements.

You are expert at reading OCR-extracted text from Indian financial reports in Schedule III format and Ind AS standards.

ABSOLUTE RULES — follow these exactly:
1. NULL POLICY: If a field is NOT PRESENT in the document text, return {"value": null, "confidence": 0, "evidence": "not found", "page": null}. NEVER invent, estimate, or interpolate a value. Returning null is always correct when data is absent.
2. DIFFERENT VALUES: NEVER return the same non-zero value for both current and previous year unless the document explicitly shows the identical number in both columns. They will almost always be different.
3. NEGATIVE NUMBERS: Bracketed numbers are negative. (1,234.56) → -1234.56. Loss amounts in parentheses → negative.
4. PLAIN NUMBERS ONLY: Strip all currency symbols (₹, Rs.), commas, and unit labels. Return raw floats. "1,234.56 Lakhs" → 1234.56. If you need to sum multiple numbers to compute a field, perform the calculation yourself and return only the final single float number. NEVER output arithmetic expressions like "9.85 + 86.36" as the value.
5. CONFIDENCE SCORES: 0.95-1.0 = found directly in table. 0.80-0.94 = computed from nearby data. Below 0.80 = uncertain. 0 = not found."""

SYNONYMS_TEXT = """FIELD SYNONYMS — use these to find fields even when named differently in the document:
- "Net Sales" = "Revenue from Operations" = "Turnover" = "Net Revenue" = "Sale of Products/Services"
- "Net Profit/Loss (PAT)" = "Profit for the year" = "PAT" = "Profit after tax" = "Total Comprehensive Income"
- "Profit before Tax" = "PBT" = "Profit Before Taxation" = "Profit before tax and exceptional items"
- "Depreciation" = "Depreciation and Amortization Expense" = "D&A" = "Depreciation on Fixed Assets"
- "Capital" = "Share Capital" = "Paid Up Capital" = "Equity Share Capital" = "Called Up Capital"
- "Other reserves" = "Reserves and Surplus" = "Other Equity" = "Securities Premium + Retained Earnings"
- "Net Worth" = "Shareholders Funds" = "Total Equity" = "Equity + Reserves"
- "Total Current Assets" = "Current Assets (Total)"
- "Total Current Liabilities" = "Current Liabilities (Total)"
- "Short Term loans from Applicant Bank" = "Cash Credit" = "Working Capital Loan" = "CC from Bank" = "OD/CC"
- "Domestic Receivables" = "Trade Receivables" = "Sundry Debtors" = "Debtors"
- "Sundry Creditors (Trade)" = "Trade Payables" = "Creditors for Goods" = "Accounts Payable"
- "Cash & Bank Balances" = "Cash and Cash Equivalents" + "Bank Balances other than Cash Equivalents"
- "Total Interest" = "Finance Costs" = "Finance Expenses" = "Interest and Finance Charges"
- "PBDIT" = "EBITDA" = PBT + Total Interest + Depreciation
- "Cash Accruals" = PAT + Depreciation
- "Long Term provisions" = "Long-Term Provisions" = gratuity + leave encashment (long-term)
- "Installments of term Loan" = "Current Maturities of Long-Term Debt" = "Current portion of TL"
- "Deferred Tax Asset" = "DTA" = "Deferred Tax Asset (Net)"
- "Gross Block" = "Property, Plant and Equipment (Gross)" = "Cost/Gross Value of Fixed Assets"
- "Net Block" = "Property, Plant and Equipment (Net)" = "Written Down Value of Fixed Assets"
- "Depreciation to Date" = "Accumulated Depreciation" = "Total Depreciation till date"
- "Total Outside Liabilities" = Total Term Liabilities + Total Current Liabilities
- "Contingent Liabilities" = "Contingencies" = "Off Balance Sheet Obligations"
- "Opening Stock in Process" = "Opening WIP" = "Work in Progress (opening)"
- "Closing Stock in Process" = "Closing WIP" = "Work in Progress (closing)" 
- "Security Deposits" = "Security Deposit" = "Deposits for Godown & Office" = "Rental Deposit" = "Lease Deposit"
- "Share Premium" = "Securities Premium" = "Securities Premium Reserve" = "Premium on Issue of Shares"
- "Other Income" = "Miscellaneous Income" = "Non Operating Income" = "Non-Operating Income"
- "Other Interest" = "Other Interests" = "Interest Income" = "Interest Earned" = "Interest Received"
- "Interest/Dividend/Royalties etc.." = "Dividend Income" = "Royalty Income" = "Dividend Received" = "Interest and Dividend Income"
- "Freight & Handling Expenses" = "Freight Expenses" = "Freight Charges" = "Freight and Forwarding" = "Freight & Forwarding" = "Handling Charges"
- "Vehicle Running Expenses" = "Vehicle Expenses" = "Motor Vehicle Expenses" = "Vehicle Maintenance Expenses" = "Car Running Expenses"
- "Insurance" = "Insurance Expenses" = "Insurance Charges" = "Insurance Premium"
- "Transport Expenses" = "Transport" = "Transportation Charges" = "Transportation Expenses" = "Transport Charges"
- "Short Term loans From Other banks" = "Short Term Borrowings from Others" = "Borrowings from Other Banks" = "Other Bank Loans"
- "Unsecured Loans from Directors" = "Loan from Directors" = "Directors Loan" = "Loans from Directors"
- "Advances to Suppliers/Transport" = "Advances to Suppliers" = "Supplier Advances" = "Advance to Vendors" = "Vendor Advances"
- "Other Statutory Liab. (Due within one Year)" = "Other Statutory Liabilities" = "Statutory Liabilities" = "Statutory Dues Payable"
- "Advance Tax/TDS" = "Advance Tax" = "Taxes Paid in Advance" = "Income Tax Receivable" = "Tax Recoverable"
- "Deferred receivables(due within one year)" = "Current Deferred Receivables" = "Deferred Receivables Current"
- "Deferred Receivables(Maturng after a year)" = "Non Current Deferred Receivables" = "Long Term Deferred Receivables"
- "Fixed Deposits with Banks" = "Fixed Deposits" = "Term Deposits" = "Bank Fixed Deposits"
- "Fixed Deposits (More Than One Year)" = "Long Term Fixed Deposits" = "Non Current Fixed Deposits" """

USER_TEMPLATE = """EXTRACTION TASK: {section}

DOCUMENT UNIT: All figures in this document are in ₹ {unit}. Return values as plain numbers in the same unit.

FINANCIAL YEARS:
- CURRENT YEAR  = {current_fy}  → LEFT column in every financial statement table
- PREVIOUS YEAR = {previous_fy} → RIGHT column in every financial statement table

TWO-COLUMN RULE (CRITICAL):
Indian financial statements ALWAYS have two data columns side by side. Example:
  "Revenue from Operations    45,230.18    38,910.43"
  → {current_fy} value = 45230.18  (LEFT)
  → {previous_fy} value = 38910.43  (RIGHT)
Always read BOTH columns and return BOTH values.

{entity_context}

{candidate_hints}

FIELDS TO EXTRACT:
{fields}

{synonyms}

REQUIRED JSON STRUCTURE (no markdown, no prose):
{{
  "FieldName": {{
    "current":  {{"value": <number or null>, "confidence": <0.0-1.0>, "evidence": "<exact text from document>", "page": <page number or null>}},
    "previous": {{"value": <number or null>, "confidence": <0.0-1.0>, "evidence": "<exact text from document>", "page": <page number or null>}}
  }}
}}

NULL POLICY: Return null when the field is absent from this chunk. Do not estimate.

--- DOCUMENT TEXT (chunk {chunk_num} of {total_chunks}) ---
{context}"""


# ── LLM extraction call ────────────────────────────────────────────────────────

def _get_filtered_synonyms(fields: list[str], entity_type: str = DEFAULT_ENTITY_TYPE) -> str:
    lines = SYNONYMS_TEXT.strip().split("\n")
    header = lines[0]
    matched = []
    for line in lines[1:]:
        line_lower = line.lower()
        if any(f.lower() in line_lower for f in fields):
            matched.append(line)

    for line in ENTITY_TYPE_EXTRA_SYNONYMS.get(entity_type, []):
        line_lower = line.lower()
        if any(f.lower() in line_lower for f in fields):
            matched.append(line)

    if not matched:
        return ""
    return header + "\n" + "\n".join(matched)


def _get_candidate_hints(candidates: list[dict], fields: list[str], chunk_text: str = "") -> str:
    from app.services.cma_extraction_service import FIELD_SEARCH_TERMS
    import re
    
    # Parse page numbers from chunk_text if available
    chunk_pages = set()
    if chunk_text:
        for m in re.finditer(r"=== Page (\d+) ===", chunk_text):
            chunk_pages.add(int(m.group(1)))
            
    hints = []
    for field_name in fields:
        terms = FIELD_SEARCH_TERMS.get(field_name, [field_name])
        matches = []
        for c in candidates:
            c_page = c.get("page_number")
            # Filter matches by chunk pages if we have them
            if chunk_pages and c_page not in chunk_pages:
                continue
            c_lbl = c.get("label", "").lower()
            if any(t.lower() in c_lbl for t in terms) or any(c_lbl in t.lower() for t in terms):
                matches.append(c)
        if matches:
            hint_lines = []
            seen = set()
            for m in matches:
                key = (m.get("current_year_value"), m.get("previous_year_value"), m.get("label"))
                if key in seen:
                    continue
                seen.add(key)
                hint_lines.append(
                    f"  - Label: {m.get('label')} | Current: {m.get('current_year_value')} | Previous: {m.get('previous_year_value')} (Page {m.get('page_number')})"
                )
                if len(hint_lines) >= 5: # Limit to max 5 hints per field
                    break
            hints.append(f"Field '{field_name}' matches in table:\n" + "\n".join(hint_lines))
    if not hints:
        return ""
    return (
        "RULE-BASED TABLE EXTRACTS (Hints from rule-based OCR parsing - verify these against raw text):\n"
        + "\n\n".join(hints)
        + "\n\nUse these values ONLY if they match the document context. If they are incorrect or from a wrong table, ignore them.\n"
    )


async def _openai_call(
    section_label:   str,
    fields:          list[str],
    context:         str,
    current_fy:      str,
    previous_fy:     str,
    unit:          str,
    chunk_num:       int = 1,
    total_chunks:    int = 1,
    candidate_hints: str = "",
    entity_type:     str = DEFAULT_ENTITY_TYPE,
    entity_context:  str = "",
) -> dict:
    if os.environ.get("OPENAI_API_KEY", "").lower() == "mock":
        mock_result = {}
        for f in fields:
            f_esc = re.escape(f)
            pattern = rf"Field '{f_esc}' matches in table:\s+-\s+Label:\s+[^\n\|]+\|\s+Current:\s*([^\n\|]+)\|\s+Previous:\s*([^\n\(]+)\(Page\s*(\d+)\)"
            match = re.search(pattern, candidate_hints)
            if match:
                curr_val_str = match.group(1).strip()
                prev_val_str = match.group(2).strip()
                page_str = match.group(3).strip()
                
                def clean_val(v_str):
                    v_str = re.sub(r"[^\d\.-]", "", v_str)
                    try:
                        return float(v_str)
                    except ValueError:
                        return None
                        
                curr_val = clean_val(curr_val_str)
                prev_val = clean_val(prev_val_str)
                page = int(page_str) if page_str.isdigit() else None
                
                mock_result[f] = {
                    "current": {"value": curr_val, "confidence": 0.95, "evidence": f"Mock Normalizer match: {curr_val_str}", "page": page},
                    "previous": {"value": prev_val, "confidence": 0.95, "evidence": f"Mock Normalizer match: {prev_val_str}", "page": page}
                }
            else:
                mock_result[f] = {
                    "current": {"value": None, "confidence": 0.0, "evidence": "Mock default: not found", "page": None},
                    "previous": {"value": None, "confidence": 0.0, "evidence": "Mock default: not found", "page": None}
                }
        return mock_result

    adapter = get_llm_adapter()

    prompt = USER_TEMPLATE.format(
        section         = section_label,
        unit            = unit,
        current_fy      = current_fy,
        previous_fy     = previous_fy,
        fields          = "\n".join(f"- {f}" for f in fields),
        synonyms        = _get_filtered_synonyms(fields, entity_type),
        context         = context,
        chunk_num       = chunk_num,
        total_chunks    = total_chunks,
        candidate_hints = candidate_hints,
        entity_context  = entity_context or build_entity_context(entity_type),
    )

    raw = ""
    for attempt in range(3):
        try:
            raw = await adapter.chat(
                messages=[
                    {"role": "system", "content": SYSTEM_MSG},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.0
            )
            return parse_json_object(raw)
        except LLMQuotaExceededError:
            # Retrying a zero-balance/quota-exhausted account can't succeed —
            # every one of the ~190 calls in this document would otherwise
            # burn 3 retries with growing backoff before silently returning
            # nulls. Fail this call immediately and let it propagate all the
            # way up so the job ends in a clear "error" state instead of a
            # near-empty "success".
            raise
        except Exception as e:
            if raw:
                import sys
                sys.stderr.write(f"RAW LLM RESPONSE on '{section_label}' failure:\n{raw}\n")
                sys.stderr.flush()
            wait = 5 * (attempt + 1)
            logger.warning(f"LLM extraction error ({attempt+1}/3) on '{section_label}': {e}. Retry in {wait}s")
            await asyncio.sleep(wait)
    return {}


# ── Entry normalisation ────────────────────────────────────────────────────────

def safe_eval_arithmetic(s: str) -> Optional[float]:
    # Check if the string only contains numbers, operators (+, -, *, /), and spaces
    if not re.match(r'^[\d\s\.\+\-\*\/\(\)]+$', s):
        return None
    try:
        if '+' in s and not any(op in s for op in ('*', '/')):
            parts = s.split('+')
            total = 0.0
            for p in parts:
                clean_p = re.sub(r"[^\d\.\-]", "", p)
                if clean_p:
                    total += float(clean_p)
            return total
    except Exception:
        pass
    return None


def _norm_entry(entry) -> dict:
    if not isinstance(entry, dict):
        return {"value": None, "confidence": 0, "evidence": "parse error", "page": None}

    val  = entry.get("value")
    conf = float(entry.get("confidence") or 0)

    if val == 0 and conf == 0:
        val = None

    if isinstance(val, (int, float)) and val != 0:
        val = float(val)  # preserve exact precision — no rounding
    elif val is not None:
        s = str(val).strip()
        if s in ("", "null", "None", "N/A", "NA", "-"):
            val = None
        else:
            # Try to safely evaluate simple additions first
            evaluated = safe_eval_arithmetic(s)
            if evaluated is not None:
                val = evaluated  # preserve exact precision — no rounding
            else:
                try:
                    cleaned = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
                    val = float(cleaned) if cleaned else None  # preserve exact precision — no rounding
                except (ValueError, TypeError):
                    val = None

    return {
        "value":      val,
        "confidence": round(conf, 2),
        "evidence":   str(entry.get("evidence") or "")[:300],
        "page":       entry.get("page"),
    }


# ── Extract one section from one chunk ────────────────────────────────────────

async def _extract_section_from_chunk(
    section_key:   str,
    meta:          dict,
    context:       str,
    current_fy:    str,
    previous_fy:   str,
    unit:          str,
    chunk_num:     int,
    total_chunks:  int,
    candidates:    list[dict] = None,
    entity_type:   str = DEFAULT_ENTITY_TYPE,
    notes:         str = "",
) -> dict[str, dict]:
    fields = meta["fields"]
    label  = meta["label"]

    hints = _get_candidate_hints(candidates or [], fields, context)
    entity_context = build_entity_context(entity_type, notes)

    raw = {}
    if len(fields) > SPLIT_AT:
        batches = [fields[i : i + SPLIT_AT] for i in range(0, len(fields), SPLIT_AT)]
        for idx, batch_fields in enumerate(batches):
            batch_label = f"{label} ({idx+1}/{len(batches)})"
            batch_raw = await _openai_call(
                batch_label, batch_fields, context,
                current_fy, previous_fy, unit,
                chunk_num, total_chunks, candidate_hints=hints,
                entity_type=entity_type, entity_context=entity_context,
            )
            raw.update(batch_raw)
    else:
        raw = await _openai_call(
            label, fields, context, current_fy, previous_fy, unit, chunk_num, total_chunks,
            candidate_hints=hints, entity_type=entity_type, entity_context=entity_context,
        )

    result = {}
    for field in fields:
        entry = raw.get(field, {})
        if isinstance(entry, dict) and ("current" in entry or "previous" in entry):
            result[field] = {
                "current":  _norm_entry(entry.get("current",  {})),
                "previous": _norm_entry(entry.get("previous", {})),
            }
        else:
            result[field] = {
                "current":  _norm_entry(entry),
                "previous": {"value": None, "confidence": 0, "evidence": "not extracted", "page": None},
            }
    return result


# ── Multi-chunk merge ──────────────────────────────────────────────────────────

def _merge_chunk_results(fields: list[str], chunk_results: list[dict]) -> dict[str, dict]:
    merged = {}
    for field in fields:
        best_cur = {"value": None, "confidence": 0, "evidence": "not found", "page": None}
        best_pre = {"value": None, "confidence": 0, "evidence": "not found", "page": None}

        for cr in chunk_results:
            entry = cr.get(field, {})
            cur   = entry.get("current", {})
            pre   = entry.get("previous", {})

            if cur.get("value") is not None:
                if float(cur.get("confidence", 0)) > float(best_cur.get("confidence", 0)):
                    best_cur = cur

            if pre.get("value") is not None:
                if float(pre.get("confidence", 0)) > float(best_pre.get("confidence", 0)):
                    best_pre = pre

        merged[field] = {"current": best_cur, "previous": best_pre}

    return merged


# ── Post-extraction computed fields ───────────────────────────────────────────

def _safe(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _compute_for_year(fields_by_section: dict, year_key: str) -> dict:
    """
    Compute derived fields for one year (current / previous).
    """
    def _get(sk: str, fn: str):
        return _safe(fields_by_section.get(sk, {}).get(fn, {}).get(year_key, {}).get("value"))

    def _set(sk: str, fn: str, val: float, ev: str):
        if sk in fields_by_section and fn in fields_by_section[sk]:
            existing = fields_by_section[sk][fn].get(year_key, {})
            if existing.get("value") is None and val is not None:
                fields_by_section[sk][fn][year_key] = {
                    "value":      val,  # preserve exact precision — no rounding
                    "confidence": 0.92,
                    "evidence":   f"[COMPUTED] {ev}",
                    "page":       None,
                }

    ns   = _get("sales",               "Net Sales")
    pat  = _get("pnl",                 "Net Profit/Loss (PAT)")
    pbt  = _get("pnl",                 "Profit before Tax")
    dep  = _get("cost_of_sales",       "Depreciation") or _get("pnl", "Depreciation adjustments")
    int_ = _get("interest",            "Total Interest")
    ca   = _get("current_assets",      "Total Current Assets")
    cl   = _get("current_liabilities", "Total Current Liabilities")
    tl   = _get("term_liabilities",    "Total Term Liabilities")
    tol  = _get("term_liabilities",    "Total Outside Liabilities")
    nw   = _get("net_worth",           "Net Worth")
    ta   = _get("intangibles",         "Total Assets")
    intg = _get("intangibles",         "Total Intangible Assets")
    op   = _get("cost_of_sales",       "Total Cost of Sales")
    div  = _get("pnl",                 "Dividend paid") or 0
    sc   = _get("net_worth",           "Capital")
    res  = _get("net_worth",           "Other reserves")
    dr   = _get("current_assets",      "Domestic Receivables")
    tp   = _get("current_liabilities", "Sundry Creditors (Trade)")
    ocl  = _get("current_liabilities", "Other current Liabilities")

    # Net Worth from components
    if sc is not None and res is not None:
        _set("net_worth", "Net Worth", sc + res, f"Capital({sc})+Reserves({res})")
    nw = _get("net_worth", "Net Worth") or nw

    # Cash Accruals = PAT + Depreciation
    if pat is not None and dep is not None:
        _set("pnl", "Cash Accruals", pat + dep, f"PAT({pat})+Dep({dep})")
    cash_acc = _get("pnl", "Cash Accruals")

    # Retained Profit / Retained Cash Profits
    if pat is not None:
        _set("pnl", "Retained Profit", pat - div, f"PAT({pat})-Div({div})")
    if cash_acc is not None:
        _set("pnl", "Retained Cash Profits", cash_acc - div, f"CashAcc({cash_acc})-Div({div})")

    # PBDIT = PBT + Interest + Depreciation
    if pbt is not None and int_ is not None and dep is not None:
        pbdit = pbt + int_ + dep
        _set("pnl", "PBDIT", pbdit, f"PBT({pbt})+Int({int_})+Dep({dep})")
    pbdit = _get("pnl", "PBDIT")

    # Operating Profit before interest
    if pbt is not None and int_ is not None:
        _set("profitability", "Operating Profit before interest", pbt + int_, f"PBT({pbt})+Int({int_})")

    # Gross Profit = Net Sales - Cost of Sales
    if ns is not None and op is not None:
        _set("profitability", "Gross profit", ns - op, f"Sales({ns})-CostOfSales({op})")
    gp = _get("profitability", "Gross profit")

    # Ratio calculations
    if ns and ns != 0:
        if gp is not None:
            _set("profitability", "Gross Profit/Sales",      round(gp / ns, 4),     f"GP({gp})/Sales({ns})")
        if pbdit is not None:
            _set("pnl",           "PBDIT/Sales",             round(pbdit / ns, 4),  f"PBDIT({pbdit})/Sales({ns})")
        opbi = _get("profitability", "Operating Profit before interest")
        if opbi is not None:
            _set("pnl",           "Operating Profits/Sales", round(opbi / ns, 4),   f"OpProfit({opbi})/Sales({ns})")
        if pbt is not None:
            _set("pnl",           "PBT/Sales",               round(pbt / ns, 4),    f"PBT({pbt})/Sales({ns})")
        if pat is not None:
            _set("pnl",           "PAT/Sales",               round(pat / ns, 4),    f"PAT({pat})/Sales({ns})")
        ca2 = _get("pnl", "Cash Accruals")
        if ca2 is not None:
            _set("pnl",           "Cash Accruals/Sales",     round(ca2 / ns, 4),    f"CashAcc({ca2})/Sales({ns})")
        if op is not None:
            _set("pnl",           "RM Content in sales",     round(op / ns, 4),     f"CostOfSales({op})/Sales({ns})")
        if dr is not None:
            _set("working_capital", "Domestic receivables - Days Gross Domestic Sales",
                 round(dr / ns * 365, 1), f"({dr}/{ns})*365")
        total_recv = _get("working_capital", "Total Receivables") or dr
        if total_recv is not None:
            _set("working_capital", "Total Receivables/Gross Sales",
                 round(total_recv / ns * 365, 1), f"({total_recv}/{ns})*365 days")

    # Tangible Net Worth
    tnw = (nw - intg) if (nw is not None and intg is not None) else nw
    if tnw is not None:
        _set("ratios", "Tangible Net Worth", tnw, f"NetWorth({nw})-Intangibles({intg or 0})")

    # Working capital metrics
    if ca is not None and cl is not None:
        nwc = ca - cl
        _set("ratios", "Net Working Capital", nwc, f"CA({ca})-CL({cl})")
        if cl != 0:
            _set("ratios", "Current Ratio", round(ca / cl, 2), f"CA({ca})/CL({cl})")
        nwc2 = _get("ratios", "Net Working Capital")
        if nwc2 is not None and ca != 0:
            _set("working_capital", "NWC % to Current Assets",
                 round(nwc2 / ca * 100, 2), f"NWC({nwc2})/CA({ca})*100")

    # Leverage ratios
    tnw_val = _get("ratios", "Tangible Net Worth") or tnw
    if tl is not None and tnw_val is not None and tnw_val != 0:
        _set("ratios", "Debt/Equity",  round(tl  / tnw_val, 2), f"TermLiab({tl})/TNW({tnw_val})")
    if tol is not None and tnw_val is not None and tnw_val != 0:
        _set("ratios", "TOL/Equity",   round(tol / tnw_val, 2), f"TOL({tol})/TNW({tnw_val})")
    if pbdit is not None and ta is not None and ta != 0:
        _set("ratios", "ROCE",         round(pbdit / ta * 100, 2), f"PBDIT({pbdit})/TotalAssets({ta})*100")

    # Working capital gap
    if ca is not None and tp is not None:
        other = ocl or 0
        _set("working_capital", "Working Capital gap",
             round(ca - tp - other, 2), f"CA({ca})-TradePay({tp})-OtherCL({other})")

    # Break-even analysis
    sales_be = _get("break_even", "Sales") or ns
    vc       = _get("break_even", "Total Variable Costs")
    fc       = _get("break_even", "Fixed Costs")
    dep_be   = dep or 0
    if sales_be and vc and fc and sales_be != 0:
        contrib = sales_be - vc
        _set("break_even", "Contribution", contrib, f"Sales({sales_be})-VarCosts({vc})")
        if contrib > 0:
            pvr = contrib / sales_be
            bep = fc / pvr
            _set("break_even", "Break Even Level of Sales",
                 round(bep, 2), f"FC({fc})/PVR({round(pvr,4)})")
            _set("break_even", "Cash Break Even of Sales",
                 round((fc - dep_be) / pvr, 2), f"(FC({fc})-Dep({dep_be}))/PVR({round(pvr,4)})")

    if dep is not None:
        _set("pnl", "Depreciation adjustments", dep, f"Same as Depreciation({dep})")

    if fields_by_section.get("pnl", {}).get("Transfer to Reserves", {}).get(year_key, {}).get("value") is None:
        _set("pnl", "Transfer to Reserves", 0, "Not mentioned — defaulted to 0")

    return fields_by_section


# ── Targeted second-pass: hunt for null fields ────────────────────────────────

FIELD_SEARCH_TERMS: dict[str, list[str]] = {
    "Domestic Sale":         ["domestic sale", "sale of products", "freight & handling income", "freighl & handling"],
    "Export Sale":           ["export sale", "export revenue", "foreign revenue"],
    "Net Sales":             ["revenue from operations", "net revenue", "net sales", "total revenue from operations", "turnover"],
    "Trade Discount":        ["trade discount", "rebate"],
    "Purchases":             ["cost of materials consumed", "purchases of stock-in-trade", "raw material consumed", "material consumed", "operating expenses", "freight & handling expenses"],
    "Services":              ["service expenses", "subcontracting", "contract labour"],
    "Freight & Handling Expenses": ["freight & handling","freight and handling","freight charges","handling charges","freight expenses"],
    "Vehicle Running Expenses":    ["vehicle running","vehicle expenses","motor vehicle expenses","vehicle maintenance"],
    "Insurance":             ["insurance","insurance expenses","insurance charges","insurance premium"],
    "Depreciation":          ["depreciation and amortization", "depreciation expense", "depreciation for the year"],
    "Transport Expenses":    ["transport expenses", "freight charges", "vehicle running", "transport", "transportation charges"],
    "Fuel Expenses":         ["fuel expenses", "power and fuel", "fuel cost"],
    "Repair & Maintenance":  ["repair & maintenance", "repairs and maintenance"],
    "Total Cost of Sales":   ["total operating expenses", "total cost of sales", "total expenses"],
    "Profit before Tax":     ["profit before tax", "profit/(loss) before tax", "profit before exceptional items and tax"],
    "Provision for Taxes":   ["tax expense", "provision for tax", "current tax"],
    "Net Profit/Loss (PAT)": ["profit for the year", "profit after tax", "net profit/(loss)", "profit/(loss) for the year"],
    "Total Interest":        ["finance costs", "interest expense", "finance charges", "bank interest"],
    "PBDIT":                 ["pbdit", "ebitda", "earnings before interest"],
    "Dividend paid":         ["dividend paid", "interim dividend", "final dividend"],
    "Other Income":          ["other income","miscellaneous income","non operating income","non-operating income"],
    "Other Interests":       ["other interests","interest income","interest earned","interest received","other interest"],
    "Interest/Dividend/Royalties etc..": ["dividend income","royalty income","dividend received","interest and dividend income"],
    "Capital":               ["equity share capital", "share capital", "paid-up share capital"],
    "Share Premium":         ["share premium","securities premium","share premium account","securities premium reserve"],
    "Other reserves":        ["reserves and surplus", "other equity", "securities premium", "surplus in statement"],
    "Net Worth":             ["total equity", "shareholders' funds", "net worth", "shareholders equity"],
    "Total Current Liabilities":  ["total current liabilities"],
    "Total Term Liabilities":     ["total long-term liabilities", "total non-current liabilities"],
    "Short Term loans from Applicant Bank": ["cash credit from bank", "cc from bank", "cash credit", "working capital loan"],
    "Short Term loans From Other banks":    ["short term borrowings from others","borrowings from other banks","other bank loans","working capital loan from other banks" ],
    "Sundry Creditors (Trade)":   ["trade payables", "sundry creditors", "creditors for goods"],
    "Installments of term Loan":  ["current maturities of", "current maturity of long term", "installment due"],
    "Other current Liabilities":  ["other current liabilities"],
    "Other Statutory Liab. (Due within one Year)": ["other statutory liabilities","statutory liabilities","statutory dues payable","government dues payable"],
    "Long Term provisions":       ["long term provisions", "provision for gratuity", "provision for leave"],
    "Term Loan from Bank":        ["term loan from bank", "term loan", "long term borrowings"],
    "Unsecured Loans":            ["unsecured loans", "loan from director", "inter corporate deposit"],
    "Unsecured Loans from Directors": ["loan from directors","directors loan","loans from directors"],
    "Total Outside Liabilities":  ["total outside liabilities", "total debt", "total liabilities"],
    "Total Current Assets":   ["total current assets"],
    "Total Assets":           ["total assets", "total net assets"],
    "Cash & Bank Balances":   ["cash and cash equivalents", "cash in hand", "bank balances"],
    "Deferred receivables(due within one year)": ["deferred receivables","current deferred receivables"],
    "Domestic Receivables":   ["trade receivables", "sundry debtors", "debtors"],
    "Finished Goods":         ["finished goods", "stock of finished goods"],
    "Stock in Process":       ["work in progress", "work-in-progress", "wip"],
    "Imported Raw Material":  ["imported raw material", "imported rm"],
    "Indigenous Rawmaterial": ["indigenous raw material", "indigenous rm", "domestic raw material"],
    "Advances to Suppliers":  ["advances to suppliers", "advance to vendor", "capital advance"],
    "Advances to Suppliers/Transport": ["advances to suppliers", "supplier advances", "advance to vendors", "vendor advances"],
    "Other Current Assets":   ["other current assets", "prepaid expenses"],
    "Gross Block":            ["gross block", "cost of assets", "total gross block", "property plant and equipment"],
    "Net Block":              ["net block", "written down value", "net carrying value", "property plant"],
    "Depreciation to Date":   ["accumulated depreciation", "depreciation to date", "total depreciation till"],
    "Deferred Tax Asset":     ["deferred tax asset", "dta", "deferred tax"],
    "Fixed Deposits with Banks": ["fixed deposits","bank fixed deposits","term deposits"],
    "Deferred Receivables(Maturng after a year)": ["long term deferred receivables","non current deferred receivables"],
    "Security Deposits":      ["security deposit","security deposits","deposit for office","deposit for godown","rental deposit"],
    "Advance Tax/TDS":        ["advance tax","advance tax tds","taxes paid in advance","income tax receivable"],
    "Capital expenditure in work-in-process": ["capital work in progress", "cwip"],
    "Net Working Capital":    ["net working capital", "nwc"],
    "Current Ratio":          ["current ratio"],
    "Debt/Equity":            ["debt equity ratio", "debt/equity"],
    "Contingent Liabilities": ["contingent liabilities", "contingencies", "off balance sheet"],
    "Gratuity Liability":     ["gratuity liability", "provision for gratuity", "gratuity"],
    "Interest on TL":         ["interest on term loan", "interest on tl", "bank interest"],
    "Interest on CC":         ["interest on cc", "interest on cash credit", "interest on working capital"],
    "Selling Expenses":       ["selling expenses", "selling & distribution", "marketing expenses"],
    "Administrative Expenses": ["administrative expenses", "general and admin", "general & administration"],
}

def _find_field_snippets(
    pages: list[dict], fields: list[str], entity_type: str = DEFAULT_ENTITY_TYPE,
    learned: Optional[dict] = None,
) -> dict[str, str]:
    results: dict[str, str] = {}
    full_pages = sorted(pages, key=lambda p: p["page"])
    entity_terms = ENTITY_TYPE_EXTRA_SEARCH_TERMS.get(entity_type, {})
    learned_terms = (learned or {}).get("field_terms", {})

    for field in fields:
        search_terms = (
            [field.lower()]
            + [t.lower() for t in FIELD_SEARCH_TERMS.get(field, [])]
            + [t.lower() for t in entity_terms.get(field, [])]
            + [t.lower() for t in learned_terms.get(field, [])]
        )
        matches = []

        for page in full_pages:
            lines = page.get("text", "").split("\n")
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if any(term in line_lower for term in search_terms):
                    start = max(0, i - 2)
                    end   = min(len(lines), i + 4)
                    snippet = "\n".join(lines[start:end]).strip()
                    if snippet:
                        matches.append(f"[Page {page['page']}]\n{snippet}")
                    break

        if matches:
            results[field] = "\n\n".join(matches[:6])

    return results

SECOND_PASS_TEMPLATE = """You are extracting SPECIFIC MISSING fields from targeted document snippets.

FINANCIAL YEARS: Current = {current_fy} (LEFT column) | Previous = {previous_fy} (RIGHT column)
DOCUMENT UNIT: ₹ {unit}
SECTION: {section}

{entity_context}

FIELDS TO EXTRACT (these were null in the first pass — find them now):
{fields}

TARGETED TEXT SNIPPETS (found by searching document for each field name):
{snippets}

TWO-COLUMN RULE: LEFT column = {current_fy}, RIGHT column = {previous_fy}.
NEGATIVE NUMBERS: (485.16) → -485.16.
NULL POLICY: Return null only if genuinely not present in the snippets.

Return ONLY valid JSON:
{{
  "FieldName": {{
    "current":  {{"value": <number|null>, "confidence": <0-1>, "evidence": "<exact quote>", "page": <int|null>}},
    "previous": {{"value": <number|null>, "confidence": <0-1>, "evidence": "<exact quote>", "page": <int|null>}}
  }}
}}"""

async def _run_second_pass(
    sections_out:  dict,
    pages:         list[dict],
    current_fy:    str,
    previous_fy:   str,
    unit:          str,
    entity_type:   str = DEFAULT_ENTITY_TYPE,
    notes:         str = "",
    learned:       Optional[dict] = None,
) -> dict:
    if os.environ.get("OPENAI_API_KEY", "").lower() == "mock":
        return sections_out

    entity_context = build_entity_context(entity_type, notes)
    adapter = get_llm_adapter()

    for section_key, section_data in sections_out.items():
        label  = section_data["label"]
        fields = section_data["fields"]

        null_fields = [
            fn for fn, fv in fields.items()
            if fv.get("current",  {}).get("value") is None
            and fv.get("previous", {}).get("value") is None
        ]
        if not null_fields:
            continue

        logger.info(f"Second pass: {label} — {len(null_fields)} null fields")

        snippets = _find_field_snippets(pages, null_fields, entity_type, learned)
        if not snippets:
            logger.info(f"  → no snippets found for any null field in {label}")
            continue

        fields_with_snippets = [f for f in null_fields if f in snippets]
        if not fields_with_snippets:
            continue

        BATCH = 8
        for batch_start in range(0, len(fields_with_snippets), BATCH):
            batch_fields = fields_with_snippets[batch_start: batch_start + BATCH]
            batch_snips  = "\n\n---\n\n".join(
                f"FIELD: {fn}\n{snippets[fn]}" for fn in batch_fields
            )
            batch_prompt = SECOND_PASS_TEMPLATE.format(
                current_fy     = current_fy,
                previous_fy    = previous_fy,
                unit           = unit,
                section        = label,
                fields         = "\n".join(f"- {f}" for f in batch_fields),
                snippets       = batch_snips,
                entity_context = entity_context,
            )

            for attempt in range(3):
                try:
                    resp = await adapter.chat(
                        messages=[
                            {"role": "system", "content": SYSTEM_MSG},
                            {"role": "user",   "content": batch_prompt},
                        ],
                        temperature=0.0
                    )
                    raw = parse_json_object(resp)

                    for fn in batch_fields:
                        entry = raw.get(fn, {})
                        if not isinstance(entry, dict):
                            continue
                        cur = _norm_entry(entry.get("current",  {}))
                        pre = _norm_entry(entry.get("previous", {}))
                        if cur.get("value") is not None:
                            fields[fn]["current"]  = cur
                        if pre.get("value") is not None:
                            fields[fn]["previous"] = pre

                    found_now = sum(
                        1 for fn in batch_fields
                        if fields[fn].get("current",  {}).get("value") is not None
                        or fields[fn].get("previous", {}).get("value") is not None
                    )
                    logger.info(f"  Second pass batch {batch_start//BATCH+1}: {found_now}/{len(batch_fields)} recovered")
                    break

                except LLMQuotaExceededError:
                    # Every remaining call in this second pass (and every
                    # other section/batch still queued) would fail on the
                    # same zero-balance account — stop immediately instead
                    # of retrying and looping through all of them first.
                    raise
                except Exception as e:
                    logger.error(f"Second pass error for {label}: {e}")
                    await asyncio.sleep(5 * (attempt + 1))

    return sections_out


# ── Pre-flight cost/token estimation ────────────────────────────────────────────
# Heuristic, not measured: OpenAI doesn't expose a dry-run token counter for
# this pipeline's actual prompts (each includes variable-length OCR'd chunk
# text, candidate hints, and synonym lists), so these are reasoned averages,
# not exact figures. Purpose is to give a ballpark before spending real money
# on a multi-document company, not a precise invoice.

# USD per 1M tokens. Update if OpenAI repricing changes these.
MODEL_PRICING_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o":      {"input": 2.50, "output": 10.00},
}

# Per OCR'd page: a rendered page image at "auto" detail (this pipeline sets
# no explicit detail level) plus a short instruction prompt, and typical
# markdown-table output length for a dense financial-statement page.
OCR_INPUT_TOKENS_PER_PAGE  = 1700
OCR_OUTPUT_TOKENS_PER_PAGE = 900

# Per document: extraction makes one full pass over all 18 CMA sections
# regardless of page count (bounded by field/section count, not page count) —
# ~18 sections x ~3 relevant chunks x ~3 field-batches, plus a second pass
# for null fields. Observed range across real runs this session: 150-200
# calls/document; this uses the midpoint.
EXTRACTION_CALLS_PER_DOCUMENT_ESTIMATE  = 190
EXTRACTION_INPUT_TOKENS_PER_CALL  = 1800
EXTRACTION_OUTPUT_TOKENS_PER_CALL = 350


def _model_cost(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    pricing = MODEL_PRICING_PER_1M_TOKENS.get(model)
    if not pricing:
        return None
    return round(
        input_tokens / 1_000_000 * pricing["input"] + output_tokens / 1_000_000 * pricing["output"],
        4,
    )


def estimate_pipeline_cost(
    doc_page_counts: list[int],
    docling_model: str,
    extraction_model: str,
) -> dict:
    """
    Estimate OCR + extraction token usage and USD cost for a set of
    documents (one page count per document, after any start_page/end_page
    trimming has already been applied by the caller).
    """
    total_pages = sum(doc_page_counts)
    num_docs = len(doc_page_counts)

    ocr_input_tokens  = total_pages * OCR_INPUT_TOKENS_PER_PAGE
    ocr_output_tokens = total_pages * OCR_OUTPUT_TOKENS_PER_PAGE

    total_extraction_calls   = num_docs * EXTRACTION_CALLS_PER_DOCUMENT_ESTIMATE
    extraction_input_tokens  = total_extraction_calls * EXTRACTION_INPUT_TOKENS_PER_CALL
    extraction_output_tokens = total_extraction_calls * EXTRACTION_OUTPUT_TOKENS_PER_CALL

    ocr_cost        = _model_cost(docling_model, ocr_input_tokens, ocr_output_tokens)
    extraction_cost = _model_cost(extraction_model, extraction_input_tokens, extraction_output_tokens)
    total_cost = (
        round(ocr_cost + extraction_cost, 4)
        if ocr_cost is not None and extraction_cost is not None
        else None
    )

    return {
        "documents": num_docs,
        "total_pages": total_pages,
        "estimated_ocr_calls": total_pages,
        "estimated_extraction_calls": total_extraction_calls,
        "estimated_tokens": {
            "ocr_input": ocr_input_tokens,
            "ocr_output": ocr_output_tokens,
            "extraction_input": extraction_input_tokens,
            "extraction_output": extraction_output_tokens,
            "total": ocr_input_tokens + ocr_output_tokens + extraction_input_tokens + extraction_output_tokens,
        },
        "estimated_cost_usd": {
            "ocr": ocr_cost,
            "extraction": extraction_cost,
            "total": total_cost,
        },
        "pricing_available_for": {
            "docling_model": docling_model in MODEL_PRICING_PER_1M_TOKENS,
            "extraction_model": extraction_model in MODEL_PRICING_PER_1M_TOKENS,
        },
        "note": (
            "Heuristic estimate based on typical page/call sizes observed in this "
            "pipeline, not a measured token count — actual usage varies with "
            "document density and how many fields need second-pass recovery. "
            "$ cost only available for models with known OpenAI pricing above; "
            "self-hosted/HF-hosted models show token estimates only."
        ),
    }


# ── Unmapped line items ──────────────────────────────────────────────────────
# The rule-based OCR table normalizer (ocr_normalizer.normalize_ocr_output)
# already scans every table row in the document to build "candidate_hints"
# for the 206 known CMA fields — it finds every labeled row regardless of
# whether that label maps to anything we track. Reusing that (already-free,
# no extra LLM calls) list to surface whatever DOESN'T map to a known field
# means nothing the document actually contains gets silently dropped just
# because it isn't part of the standardized CMA schema.

def _all_known_field_terms(learned: Optional[dict] = None) -> set[str]:
    terms: set[str] = set()
    for field_name in CMA_FIELDS_FLAT:
        terms.add(field_name.lower())
        terms.update(t.lower() for t in FIELD_SEARCH_TERMS.get(field_name, []))
    for entity_terms in ENTITY_TYPE_EXTRA_SEARCH_TERMS.values():
        for extra in entity_terms.values():
            terms.update(t.lower() for t in extra)
    if learned:
        for field_terms in learned.get("field_terms", {}).values():
            terms.update(t.lower() for t in field_terms)
    return terms


def _is_mapped_label(label: str, known_terms: set[str]) -> bool:
    """
    Heuristic substring match, not exact — a candidate label is considered
    "already covered" if it shares a reasonably long fragment with any known
    field/alias. Terms under 4 chars are skipped to avoid short-token false
    positives (e.g. "pat" matching inside "Repatriation"); the cost of that
    is a few borderline duplicates surfacing in the unmapped list rather
    than a real line item getting silently suppressed, which is the
    direction we want to err in for an additive, non-authoritative section.
    """
    label_lower = (label or "").strip().lower()
    if not label_lower:
        return True
    for term in known_terms:
        if len(term) < 4:
            continue
        if term in label_lower or label_lower in term:
            return True
    return False


def find_unmapped_candidates(candidates: list[dict], learned: Optional[dict] = None) -> list[dict]:
    """
    Candidates the table normalizer found that don't match any of the 206
    standard CMA fields (by name, search alias, entity-type override, or
    self-learned term). Deduplicated by label.
    """
    known_terms = _all_known_field_terms(learned)
    seen: set[str] = set()
    unmapped: list[dict] = []
    for c in candidates:
        label = (c.get("label") or "").strip()
        if not label or _is_mapped_label(label, known_terms):
            continue
        if c.get("current_year_value") is None and c.get("previous_year_value") is None:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        unmapped.append({
            "label":           label,
            "page":            c.get("page_number"),
            "current_value":   c.get("current_year_value"),
            "previous_value":  c.get("previous_year_value"),
        })
    return unmapped


# ── Public extraction API ──────────────────────────────────────────────────────

async def extract_cma_fields(
    pages:        list[dict],
    source_file:  str = "",
    doc_id:       str = "",
    current_fy:   str = "unknown",
    previous_fy:  str = "unknown",
    raw:          bool = False,
    entity_type:  str = DEFAULT_ENTITY_TYPE,
    start_page:   Optional[int] = None,
    end_page:     Optional[int] = None,
    notes:        str = "",
) -> dict:
    """
    Extract all CMA fields for BOTH years from document pages using local LLM.

    entity_type/notes bias keyword routing, synonym matching, and the prompt
    itself toward the uploader-declared legal structure (LLP/partnership/
    proprietorship/etc.) instead of assuming Pvt Ltd Schedule III vocabulary.
    start_page/end_page restrict extraction to the financial-statement pages.
    """
    entity_type = entity_type or DEFAULT_ENTITY_TYPE
    fingerprint = _extraction_fingerprint(entity_type, start_page, end_page, notes)
    cache_key = doc_id
    cached = _load_ai_cache(cache_key, fingerprint)
    if cached:
        return cached

    pages = trim_pages(pages, start_page, end_page)
    learned = load_learned_terms()

    unit = detect_unit(pages)
    logger.info(
        f"{source_file}: unit={unit}, pages={len(pages)}, entity_type={entity_type}, "
        f"page_range=({start_page},{end_page})"
    )

    try:
        from app.services.ocr_normalizer import normalize_ocr_output
        candidates = normalize_ocr_output(pages).get("candidates", [])
    except Exception as e:
        logger.warning(f"Failed to run normalizer: {e}")
        candidates = []

    chunks = _build_chunks(pages)
    if not chunks:
        logger.error(f"{source_file}: no text extracted — aborting")
        return {"meta": {"source_file": source_file, "error": "no text"}, "sections": {}}

    logger.info(f"{source_file}: {len(pages)} pages → {len(chunks)} chunks")

    sections_out = {}
    found_total  = 0
    total_fields = 0

    sections_def = CMA_SECTIONS
    for section_key, meta in sections_def.items():
        fields_count = len(meta["fields"])
        logger.info(f"Extracting: {meta['label']} ({fields_count} fields)")

        relevant_chunks = _get_relevant_chunks(chunks, section_key, entity_type, top_k=3, learned=learned)
        logger.info(f"  → {len(relevant_chunks)} relevant chunks (of {len(chunks)} total)")

        chunk_results = []
        for idx, chunk in enumerate(relevant_chunks):
            chunk_result = await _extract_section_from_chunk(
                section_key, meta, chunk,
                current_fy, previous_fy, unit,
                chunk_num=idx + 1,
                total_chunks=len(relevant_chunks),
                candidates=candidates,
                entity_type=entity_type,
                notes=notes,
            )
            chunk_results.append(chunk_result)

        merged_fields = _merge_chunk_results(meta["fields"], chunk_results)

        found = sum(
            1 for v in merged_fields.values()
            if v.get("current", {}).get("value") is not None
            or v.get("previous", {}).get("value") is not None
        )
        found_total  += found
        total_fields += fields_count

        sections_out[section_key] = {
            "label":  meta["label"],
            "fields": merged_fields,
        }
        logger.info(f"  → {found}/{fields_count} fields found")

    logger.info(f"{source_file}: running second pass for null fields...")
    sections_out = await _run_second_pass(sections_out, pages, current_fy, previous_fy, unit, entity_type, notes, learned)

    fields_lookup: dict = {
        sk: {fn: sv["fields"][fn] for fn in sv["fields"]}
        for sk, sv in sections_out.items()
    }

    for year_key in ("current", "previous"):
        _compute_for_year(fields_lookup, year_key)

    for sk in sections_out:
        for fn in sections_out[sk]["fields"]:
            sections_out[sk]["fields"][fn] = fields_lookup[sk][fn]

    found_after = sum(
        1 for sv in sections_out.values()
        for fv in sv["fields"].values()
        if fv.get("current", {}).get("value") is not None
        or fv.get("previous", {}).get("value") is not None
    )

    unmapped_items = find_unmapped_candidates(candidates, learned)

    result = {
        "meta": {
            "source_file":      source_file,
            "doc_id":           doc_id,
            "current_fy":       current_fy,
            "previous_fy":      previous_fy,
            "unit":             unit,
            "entity_type":      entity_type,
            "start_page":       start_page,
            "end_page":         end_page,
            "notes":            notes or None,
            "total_fields":     total_fields,
            "fields_found":     found_after,
            "fields_not_found": total_fields - found_after,
            "coverage_pct":     round(found_after / total_fields * 100, 1) if total_fields else 0,
            "chunks_used":      len(chunks),
            "total_pages":      len(pages),
        },
        "sections": sections_out,
        "unmapped_items": unmapped_items,
    }

    _save_ai_cache(cache_key, result, fingerprint)

    try:
        learn_from_extraction(sections_out)
    except Exception as e:
        logger.warning(f"Self-learning step failed (non-fatal): {e}")

    logger.info(f"{source_file}: extraction complete — {found_after}/{total_fields} fields ({result['meta']['coverage_pct']}%)")
    return result
