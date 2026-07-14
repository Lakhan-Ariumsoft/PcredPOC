import re
import logging
from pathlib import Path
from collections import Counter

logger = logging.getLogger(__name__)

def detect_fy(pages: list[dict], filename: str = "") -> tuple[str, str]:
    """
    Detect (current_fy, previous_fy) from document text, then filename fallback.
    Indian financials: "Year ended March 31, 2024" means FY 2023-24.
    """
    patterns = [
        r"year\s+ended\s+(?:march|mar)[\s,]+31[,\s]+(\d{4})",
        r"march\s+31[,\s]+(\d{4})",
        r"31\s*(?:st|nd|rd|th)?\s*(?:march|mar)[,\s]+(\d{4})",
        r"as\s+at\s+(?:march|mar)[\s,]+31[,\s]+(\d{4})",
        r"for\s+the\s+year\s+ended.*?(\d{4})",
        r"year\s+ended.*?31.*?(\d{4})",
    ]
    years_found = []
    for p in pages:
        t = p.get("text", "").lower()
        for pat in patterns:
            for m in re.finditer(pat, t):
                yr = int(m.group(1))
                if 2010 <= yr <= 2035:
                    years_found.append(yr)

    if years_found:
        yr = Counter(years_found).most_common(1)[0][0]
        current = f"{yr - 1}-{str(yr)[-2:]}"
        previous = f"{yr - 2}-{str(yr - 1)[-2:]}"
        logger.info(f"FY detected from text: {current} / {previous}")
        return current, previous

    m = re.search(r"(?:fy|ay)[_\s-]?(\d{4})[_\-](\d{2,4})", filename, re.I)
    if m:
        start = int(m.group(1))
        end_str = m.group(2)
        end = int(end_str) if len(end_str) == 4 else start + 1
        current = f"{start}-{str(end)[-2:]}"
        previous = f"{start - 1}-{str(start)[-2:]}"
        logger.info(f"FY from filename: {current}")
        return current, previous

    m = re.search(r"(\d{4})[_\-](\d{2})\b", filename)
    if m:
        start = int(m.group(1))
        current = f"{start}-{m.group(2)}"
        previous = f"{start - 1}-{str(start)[-2:]}"
        return current, previous

    m = re.search(r"\b(20\d{2})\b", filename)
    if m:
        yr = int(m.group(1))
        return f"{yr - 1}-{str(yr)[-2:]}", f"{yr - 2}-{str(yr - 1)[-2:]}"

    return "unknown", "unknown"

def get_financial_year(pdf_path: Path, pages: list[dict]) -> tuple[str, str]:
    return detect_fy(pages, pdf_path.name)
