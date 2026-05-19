"""
PDF text extraction with OCR caching.
"""
import hashlib, json, logging, os, re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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


def _pdfplumber_extract(pdf_path: Path) -> list[dict]:
    import pdfplumber
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").strip()
                pages.append({"page": i, "text": text})
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
            imgs = convert_from_path(pdf_path, first_page=pg, last_page=pg, dpi=200)
            if imgs:
                text = pytesseract.image_to_string(imgs[0], config="--psm 6 --oem 3")
                result[pg] = text.strip()
        except Exception as e:
            logger.warning(f"OCR page {pg} failed: {e}")
    return result


def extract_all_text(pdf_path: Path) -> list[dict]:
    """Extract text from all pages. Caches result to disk."""
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

    # Fully scanned — pdfplumber returned nothing
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


def detect_fy(pages: list[dict], filename: str = "") -> tuple[str, str]:
    """Detect (current_fy, previous_fy) from page text then filename."""
    patterns = [
        r"march\s+31[,\s]+(\d{4})",
        r"31\s*(?:st|nd|rd|th)?\s+march[,\s]+(\d{4})",
        r"year\s+ended.*?(\d{4})",
    ]
    # Check all pages (not just first 5) for year
    for p in pages:
        t = p["text"].lower()
        for pat in patterns:
            m = re.search(pat, t)
            if m:
                yr = int(m.group(1))
                if 2010 <= yr <= 2035:
                    return f"{yr-1}-{str(yr)[-2:]}", f"{yr-2}-{str(yr-1)[-2:]}"

    # Filename fallback
    m = re.search(r"(\d{4})[_\-](\d{2})\b", filename)
    if m:
        start = int(m.group(1))
        return f"{start}-{m.group(2)}", f"{start-1}-{str(start)[-2:]}"
    m = re.search(r"\b(20\d{2})\b", filename)
    if m:
        yr = int(m.group(1))
        return f"{yr-1}-{str(yr)[-2:]}", f"{yr-2}-{str(yr-1)[-2:]}"

    return "unknown", "unknown"


# keep old name for compatibility
def get_financial_year(pdf_path: Path, pages: list[dict]) -> tuple[str, str]:
    return detect_fy(pages, pdf_path.name)