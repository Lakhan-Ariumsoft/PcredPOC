"""
Utility helpers: text normalisation, amount parsing, year detection.
"""

from __future__ import annotations
import re


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """
    Normalise a string for fuzzy matching:
    - lowercase
    - keep only alphanumeric, spaces, & symbol
    - collapse whitespace
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s&]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_amounts(text: str) -> str:
    """
    Remove numeric tokens from a line so only the label remains.
    Also removes OCR artefacts like "|", single-char noise.
    """
    # Remove bracketed negatives like (1,020.00)
    text = re.sub(r"\([\d,]+\.?\d*\)", " ", text)
    # Remove plain numbers with optional commas/decimals
    text = re.sub(r"[\d,]+\.?\d*", " ", text)
    # Remove stray pipe characters from OCR
    text = re.sub(r"\|", " ", text)
    # Remove trailing dashes used as zero placeholders
    text = re.sub(r"\s+-\s*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------

# Matches (1,020.00) or 1,020.00 or plain integers
_AMOUNT_PATTERN = re.compile(
    r"\(([\d,]+\.?\d*)\)"   # bracketed negative
    r"|"
    r"([\d,]+\.\d+)"        # decimal number (must have decimal point)
    r"|"
    r"(?<!\d)([\d,]{3,})(?!\d)"  # integer with comma-separator (min 3 chars → avoids note nos)
)


def parse_amount(token: str) -> float | None:
    """Convert an amount string to float. Returns None for invalid input."""
    token = token.strip()
    if token in ("-", "—", "", "nil", "n/a"):
        return None
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()")
    token = token.replace(",", "")
    try:
        val = float(token)
        return -val if negative else val
    except ValueError:
        return None


def extract_amounts_from_line(line: str) -> list[float]:
    """
    Extract all financial amounts from a raw OCR text line.

    Handles:
    - "1,020.00" → 1020.0
    - "(1,020.00)" → -1020.0
    - Note numbers (2, 3, 5, etc.) are stripped by the pattern
      (bare integers < 3 chars are not matched by _AMOUNT_PATTERN)

    Note number handling
    --------------------
    Indian balance sheets put note reference numbers between the label
    and the amounts: "Long Term Provisions 6 49.74 19.08"
    The pattern above only matches integers that are ≥ 3 characters long
    (i.e., comma-separated or 3+ digits) to avoid picking up note numbers.
    Single/double digit standalone integers are NOT matched.

    Edge case: note "10" would be matched as it has 2 digits but no comma.
    Extra guard: if the first value is an integer ≤ 40 and more amounts
    follow, drop it.
    """
    raw: list[float] = []

    for m in _AMOUNT_PATTERN.finditer(line):
        bracket, decimal, integer = m.group(1), m.group(2), m.group(3)
        if bracket:
            token = f"({bracket})"
        elif decimal:
            token = decimal
        else:
            token = integer or ""

        val = parse_amount(token)
        if val is not None:
            raw.append(val)

    if not raw:
        return []

    # Extra guard: drop leading note number (e.g., "10" for Note 10)
    # Condition: first value is a whole number ≤ 40 AND more values follow
    if (
        len(raw) >= 2
        and raw[0] == int(raw[0])      # whole number
        and 1 <= raw[0] <= 40          # in note-number range
    ):
        raw = raw[1:]

    return raw


# ---------------------------------------------------------------------------
# Company name extraction
# ---------------------------------------------------------------------------

def extract_company_name(text: str) -> str | None:
    """Try to extract company name from top of page text."""
    for line in text.split("\n")[:15]:
        line = line.strip()
        if re.search(r"\b(limited|ltd\.?|private|pvt\.?)\b", line, re.IGNORECASE):
            if len(line) > 5 and not re.match(r"^\d", line):
                return line
    return None


# ---------------------------------------------------------------------------
# Financial year detection
# ---------------------------------------------------------------------------

def detect_years_from_text(text: str) -> list[str]:
    """
    Parse financial years like "2022-23" or "March 31, 2023" from text.
    Returns up to 2 years sorted descending (current first).
    """
    # Pattern 1: "2022-23"
    fy = re.findall(r"20\d{2}-\d{2}", text)
    if fy:
        unique = list(dict.fromkeys(fy))
        if len(unique) >= 2:
            return sorted(unique, reverse=True)[:2]

    # Pattern 2: "March 31, 2023"
    march = re.findall(r"[Mm]arch\s+(?:31\s*,?\s*)?(20\d{2})", text)
    if len(march) >= 2:
        ints = sorted(set(int(y) for y in march), reverse=True)[:2]
        return [f"{y - 1}-{str(y)[2:]}" for y in ints]

    return []