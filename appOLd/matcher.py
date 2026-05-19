"""
CMA field matcher using rapidfuzz with post-match exclusion validation.
"""

from __future__ import annotations
from rapidfuzz import fuzz, process
from app.constants import CMA_FIELDS
from app.utils import clean_text

# ---------------------------------------------------------------------------
# Build alias → field_key map at startup
# ---------------------------------------------------------------------------

_alias_to_key: dict[str, str] = {}

for _key, _meta in CMA_FIELDS.items():
    for _alias in _meta["aliases"]:
        _alias_to_key[clean_text(_alias)] = _key

_all_aliases: list[str] = list(_alias_to_key.keys())

# Build exclusion map: field_key → list of lower-case substrings
_exclusions: dict[str, list[str]] = {
    key: [s.lower() for s in meta.get("exclude_if_contains", [])]
    for key, meta in CMA_FIELDS.items()
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _passes_exclusion(field_key: str, raw_label: str) -> bool:
    """
    Return False if raw_label contains any exclusion keyword for field_key.
    This prevents false positives like:
    - "Long - Term Loans and Advances" → long_term_borrowings  (should be rejected)
    - "Other Current Liabilities" → other_long_term_liabilities (should be rejected)
    """
    raw_lower = raw_label.lower()
    for excl in _exclusions.get(field_key, []):
        if excl in raw_lower:
            return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_cma_field(
    raw_label: str,
    threshold: int = 72,
) -> tuple[str | None, int]:
    """
    Fuzzy-match raw_label against CMA field aliases.

    Returns (field_key, score) or (None, 0).

    Uses token_set_ratio which handles word-order variance well.
    After a candidate match is found, runs exclusion validation to
    reject false positives.
    """
    cleaned = clean_text(raw_label)
    if len(cleaned) < 4:
        return None, 0

    # Get top candidates (in case the best match is excluded)
    results = process.extract(
        cleaned,
        _all_aliases,
        scorer=fuzz.token_set_ratio,
        limit=5,
        score_cutoff=threshold,
    )

    for alias, score, _ in results:
        field_key = _alias_to_key[alias]
        if _passes_exclusion(field_key, raw_label):
            return field_key, int(score)

    return None, 0