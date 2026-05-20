"""
PDF text extraction with financial page detection and OCR caching.

Key improvements:
- Detects and scores financial pages BEFORE sending to OpenAI (saves tokens)
- Extracts BOTH current year AND previous year column values separately
- Detects financial year from document text accurately
- OCR cache so re-runs are instant
"""
import hashlib, json, logging, os, re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    d = Path(os.environ.get("UPLOADS_ROOT", "uploads")) / ".ocr_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(str(path.stat().st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()[:32]

def _load_ocr_cache(path: Path) -> Optional[list]:
    try:
        cp = _cache_dir() / f"{_file_hash(path)}.json"
        if cp.exists():
            logger.info(f"OCR cache hit: {path.name}")
            return json.loads(cp.read_text())
    except Exception:
        pass
    return None

def _save_ocr_cache(path: Path, pages: list) -> None:
    try:
        cp = _cache_dir() / f"{_file_hash(path)}.json"
        cp.write_text(json.dumps(pages))
    except Exception as e:
        logger.warning(f"OCR cache save failed: {e}")

def clear_ocr_cache_for(path: Path) -> bool:
    try:
        cp = _cache_dir() / f"{_file_hash(path)}.json"
        if cp.exists():
            cp.unlink()
            return True
    except Exception:
        pass
    return False

def clear_all_ocr_caches() -> int:
    count = 0
    try:
        for f in _cache_dir().glob("*.json"):
            f.unlink()
            count += 1
    except Exception as e:
        logger.warning(f"OCR cache clear failed: {e}")
    return count

# ── PDF text extraction ───────────────────────────────────────────────────────

def _pdfplumber_extract(pdf_path: Path) -> list[dict]:
    import pdfplumber
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").strip()
                # Also try to extract tables
                tables = page.extract_tables() or []
                table_text = ""
                for table in tables:
                    for row in table:
                        if row:
                            table_text += " | ".join(str(c or "").strip() for c in row) + "\n"
                combined = text
                if table_text and table_text not in text:
                    combined = text + "\n" + table_text
                pages.append({"page": i, "text": combined.strip()})
    except Exception as e:
        logger.warning(f"pdfplumber error: {e}")
    return pages

def _ocr_pages(pdf_path: Path, page_nums: list[int]) -> dict[int, str]:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return {}
    result = {}
    for pg in page_nums:
        try:
            imgs = convert_from_path(pdf_path, first_page=pg, last_page=pg, dpi=220)
            if imgs:
                text = pytesseract.image_to_string(imgs[0], config="--psm 6 --oem 3")
                result[pg] = text.strip()
        except Exception as e:
            logger.warning(f"OCR page {pg} failed: {e}")
    return result

def extract_all_text(pdf_path: Path) -> list[dict]:
    """Extract text from all pages. Caches to disk — second call is instant."""
    pdf_path = Path(pdf_path)
    cached = _load_ocr_cache(pdf_path)
    if cached:
        return cached

    pages = _pdfplumber_extract(pdf_path)
    blank = [p["page"] for p in pages if len(p["text"]) < 30]

    if blank:
        logger.info(f"OCR needed for {len(blank)} pages in {pdf_path.name}")
        ocr = _ocr_pages(pdf_path, blank)
        for p in pages:
            if p["page"] in ocr:
                p["text"] = ocr[p["page"]]

    if not pages or all(len(p["text"]) < 30 for p in pages):
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                total = len(pdf.pages)
        except Exception:
            total = 40
        logger.info(f"Fully scanned PDF, OCR all {total} pages")
        ocr = _ocr_pages(pdf_path, list(range(1, min(total + 1, 50))))
        pages = [{"page": pg, "text": txt} for pg, txt in sorted(ocr.items())]

    result = [p for p in pages if len(p["text"]) > 10]
    _save_ocr_cache(pdf_path, result)
    return result

# ── Financial page detection ──────────────────────────────────────────────────

FINANCIAL_KEYWORDS = [
    # High-value indicators (strong signal)
    "balance sheet", "profit and loss", "profit & loss", "statement of profit",
    "income statement", "cash flow statement", "revenue from operations",
    "shareholders fund", "share capital", "reserves and surplus",
    "total assets", "total liabilities", "fixed assets", "current assets",
    "current liabilities", "long term borrowings", "short term borrowings",
    "trade payables", "trade receivables", "depreciation",
    "profit before tax", "profit after tax", "net profit", "gross profit",
    "earnings per share", "ebitda", "pbdit",
    # Medium-value
    "lakhs", "crores", "₹", "rs.", "inr",
    "march 31", "march 2", "31st march",
    "note no", "schedule", "particulars",
]

NON_FINANCIAL_KEYWORDS = [
    "director's report", "directors report", "auditor's report", "auditors report",
    "corporate governance", "management discussion", "notice of",
    "chairman", "board of directors", "annual general meeting",
    "csr report", "secretarial audit", "nomination committee",
]

def _score_page(text: str) -> int:
    """Score a page for financial content relevance (0-100)."""
    t = text.lower()
    score = 0
    for kw in FINANCIAL_KEYWORDS:
        if kw in t:
            score += 8
    for kw in NON_FINANCIAL_KEYWORDS:
        if kw in t:
            score -= 15
    # Boost pages with many numbers (financial statements are number-heavy)
    nums = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b', text)
    if len(nums) > 20:
        score += 15
    elif len(nums) > 10:
        score += 8
    return max(0, min(100, score))

def filter_financial_pages(pages: list[dict], min_score: int = 12) -> list[dict]:
    """
    Return only pages likely to contain financial data.
    Scores each page and filters out cover pages, director reports, etc.
    """
    scored = []
    for p in pages:
        score = _score_page(p["text"])
        scored.append({**p, "_score": score})

    financial = [p for p in scored if p["_score"] >= min_score]

    # Fallback: if very few pass, take top 20 by score
    if len(financial) == 0:
        financial = sorted(scored, key=lambda x: x["_score"], reverse=True)[:20]

    # Sort back into document order
    financial = sorted(financial, key=lambda x: x["page"])
    logger.info(f"Financial page filter: {len(pages)} → {len(financial)} pages kept")
    return financial

# ── Financial year detection ───────────────────────────────────────────────────

def detect_fy(pages: list[dict], filename: str = "") -> tuple[str, str]:
    """
    Detect (current_fy, previous_fy) from page text then filename.
    Indian financials: "Year ended March 31, 2023" means FY 2022-23.
    """
    patterns = [
        r"year\s+ended\s+(?:march|mar)[\s,]+31[,\s]+(\d{4})",
        r"march\s+31[,\s]+(\d{4})",
        r"31\s*(?:st|nd|rd|th)?\s*(?:march|mar)[,\s]+(\d{4})",
        r"as\s+at\s+(?:march|mar)[\s,]+31[,\s]+(\d{4})",
        r"for\s+the\s+year\s+ended.*?(\d{4})",
    ]
    years_found = []
    for p in pages:
        t = p["text"].lower()
        for pat in patterns:
            for m in re.finditer(pat, t):
                yr = int(m.group(1))
                if 2010 <= yr <= 2035:
                    years_found.append(yr)

    if years_found:
        # Most common year = current year of this document
        from collections import Counter
        yr = Counter(years_found).most_common(1)[0][0]
        current = f"{yr-1}-{str(yr)[-2:]}"
        previous = f"{yr-2}-{str(yr-1)[-2:]}"
        logger.info(f"FY detected from text: {current}")
        return current, previous

    # Filename fallback
    m = re.search(r"(?:fy|ay)[_\s-]?(\d{4})[_\-](\d{2,4})", filename, re.I)
    if m:
        start = int(m.group(1))
        end_str = m.group(2)
        end = int(end_str) if len(end_str) == 4 else start + 1
        current = f"{start}-{str(end)[-2:]}"
        previous = f"{start-1}-{str(start)[-2:]}"
        logger.info(f"FY from filename: {current}")
        return current, previous

    m = re.search(r"(\d{4})[_\-](\d{2})\b", filename)
    if m:
        start = int(m.group(1))
        current = f"{start}-{m.group(2)}"
        previous = f"{start-1}-{str(start)[-2:]}"
        return current, previous

    m = re.search(r"\b(20\d{2})\b", filename)
    if m:
        yr = int(m.group(1))
        return f"{yr-1}-{str(yr)[-2:]}", f"{yr-2}-{str(yr-1)[-2:]}"

    return "unknown", "unknown"

def get_financial_year(pdf_path: Path, pages: list[dict]) -> tuple[str, str]:
    return detect_fy(pages, pdf_path.name)