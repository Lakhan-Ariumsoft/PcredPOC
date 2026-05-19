"""
CMA extractor for image-based (scanned) financial PDFs.

Public surface
--------------
detect_financial_year(pdf_path)   → (current_fy, previous_fy)  e.g. ("2022-23","2021-22")
extract_cma_data(pdf_path, ...)   → dict  (single-document result)
"""

import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd
import pytesseract
from pdf2image import convert_from_path
from PIL import Image

from app.constants import (
    CMA_FIELDS,
    MSME_KEYWORDS,
    OTHER_CREDITORS_KEYWORDS,
    SECTION_LABELS,
)
from app.matcher import match_cma_field

logger = logging.getLogger(__name__)

# ── Column thresholds (% of page width) ──────────────────────────────────────
LABEL_MAX_PCT = 54
CY_MIN_PCT    = 60
CY_MAX_PCT    = 77
PY_MIN_PCT    = 77

# OCR / rendering settings
DPI_SEARCH  = 120
DPI_EXTRACT = 250
TESS_CONFIG = "--psm 6 --oem 3"
MIN_CONF    = 40
ROW_GAP_PX  = 10

BS_PAGE_HINT  = 14
BS_SEARCH_WIN = 3


# ── Value helpers ─────────────────────────────────────────────────────────────

def _empty_yv() -> dict:
    return {"value": None, "source_page": None, "matched_text": None, "confidence": None}


def _yv(value, page, text, conf) -> dict:
    return {
        "value":        round(value, 2) if value is not None else None,
        "source_page":  page,
        "matched_text": text,
        "confidence":   round(conf, 1) if conf is not None else None,
    }


# ── Amount parsing ────────────────────────────────────────────────────────────

def _parse_amount(text: str) -> Optional[float]:
    """
    '1,335.12'→1335.12  '772,16'→772.16 (OCR comma-decimal)  '(28.82)'→-28.82
    Rejects note numbers like '4','10' (no decimal in string).
    """
    if not text:
        return None
    text = str(text).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(" ", "")
    text = re.sub(r"^[^0-9]+", "", text)
    if not text:
        return None

    if "." not in text and "," in text:
        if re.search(r",\d{2}$", text):   # OCR decimal-as-comma: 772,16 → 772.16
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")   # thousands comma only → reject later
    else:
        text = text.replace(",", "")

    if "." not in text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return None if value == 0 else (-value if negative else value)


def _all_amounts_in(text: str) -> list[float]:
    return [
        v for tok in re.findall(r"[\d,]+\.\d+", text)
        if (v := _parse_amount(tok)) is not None
    ]


# ── Financial year detection ──────────────────────────────────────────────────

def _ending_year_to_fy(ending_year: int) -> str:
    """2023 → '2022-23',  2024 → '2023-24'"""
    return f"{ending_year - 1}-{str(ending_year)[-2:]}"


def _parse_fy_from_text(text: str) -> Optional[tuple[str, str]]:
    """
    Search text for 'March 31, YYYY' or '31st March YYYY' and return
    (current_fy, previous_fy).  Returns None if no year found.
    """
    t = text.lower()
    patterns = [
        r"march\s+31[,\s]+(\d{4})",
        r"31\s*(?:st|nd|rd|th)?\s+march[,\s]+(\d{4})",
        r"march[,\s]+(\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            ending_year = int(m.group(1))
            if 2015 <= ending_year <= 2035:
                return _ending_year_to_fy(ending_year), _ending_year_to_fy(ending_year - 1)
    return None


def _parse_fy_from_filename(name: str) -> Optional[tuple[str, str]]:
    """
    Try to extract FY from filename.
    '2022-23' → ('2022-23','2021-22')  /  'FY2023' → ('2022-23','2021-22')
    """
    # pattern: 4digit-2digit  e.g. 2022-23
    m = re.search(r"(\d{4})[_\-](\d{2})\b", name)
    if m:
        start = int(m.group(1))
        end   = start + 1
        fy    = f"{start}-{m.group(2)}"
        return fy, _ending_year_to_fy(start)

    # pattern: standalone 4-digit year e.g. FY2023 or _2023
    m = re.search(r"\b(20\d{2})\b", name)
    if m:
        ending_year = int(m.group(1))
        return _ending_year_to_fy(ending_year), _ending_year_to_fy(ending_year - 1)

    return None


def detect_financial_year(pdf_path) -> tuple[str, str]:
    """
    Detect (current_fy, previous_fy) from a financial PDF.

    Strategy:
    1. OCR the top of the balance sheet page (fast, 120 DPI, header only).
    2. Parse 'March 31, YYYY' to derive FY.
    3. Fall back to filename parsing.
    4. Fall back to a safe default.

    Returns e.g. ("2022-23", "2021-22").
    """
    pdf_path = Path(pdf_path)
    bs_page  = _find_bs_page(pdf_path)

    try:
        imgs = convert_from_path(pdf_path, first_page=bs_page, last_page=bs_page, dpi=DPI_SEARCH)
        if imgs:
            img = imgs[0]
            w, h = img.size
            # Crop top 18% — enough to capture the "as at March 31, YYYY" header
            header_img = img.crop((0, 0, w, int(h * 0.18)))
            text = pytesseract.image_to_string(header_img, config=TESS_CONFIG)
            result = _parse_fy_from_text(text)
            if result:
                logger.info(f"Year detected from OCR header: {result[0]}")
                return result
    except Exception as exc:
        logger.warning(f"OCR year detection failed: {exc}")

    # Filename fallback
    result = _parse_fy_from_filename(pdf_path.name)
    if result:
        logger.info(f"Year detected from filename: {result[0]}")
        return result

    logger.warning(f"Could not detect year for {pdf_path.name}, defaulting to unknown")
    return "unknown", "unknown"


# ── OCR → rows ────────────────────────────────────────────────────────────────

def _ocr_words(image: Image.Image) -> pd.DataFrame:
    raw = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, config=TESS_CONFIG
    )
    df = pd.DataFrame(raw)
    df = df[df["conf"] >= MIN_CONF].copy()
    df = df[df["text"].str.strip() != ""].copy()
    if df.empty:
        return df
    width = image.size[0]
    df["x_pct"] = (df["left"] + df["width"] / 2) / width * 100
    df = df.sort_values(["top", "left"]).reset_index(drop=True)
    df["row_group"] = (df["top"].diff().abs() > ROW_GAP_PX).cumsum()
    return df


def _build_raw_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, grp in df.groupby("row_group"):
        grp     = grp.sort_values("left")
        label   = " ".join(grp[grp["x_pct"] < LABEL_MAX_PCT]["text"])
        cy_text = " ".join(grp[(grp["x_pct"] >= CY_MIN_PCT) & (grp["x_pct"] <= CY_MAX_PCT)]["text"])
        py_text = " ".join(grp[grp["x_pct"] > PY_MIN_PCT]["text"])
        raw     = " ".join(grp["text"])
        rows.append({
            "label":    label.strip(),
            "cy_text":  cy_text.strip(),
            "py_text":  py_text.strip(),
            "raw_line": raw.strip(),
        })
    return rows


_CONTINUATION_PREFIXES = (
    "- term", "-term", "term borrowings", "term provisions",
    "term loans", "enterprises",
)


def _merge_split_rows(rows: list[dict]) -> list[dict]:
    merged = []
    i = 0
    while i < len(rows):
        row      = rows[i]
        label_lc = row["label"].lower().strip()
        if i + 1 < len(rows):
            next_lc = rows[i + 1]["label"].lower().strip()
            is_cont = (
                any(next_lc.startswith(p) for p in _CONTINUATION_PREFIXES)
                or (label_lc in ("long", "short") and next_lc.startswith(("-", "term")))
            )
            if is_cont:
                nxt = rows[i + 1]
                merged.append({
                    "label":    (row["label"] + " " + nxt["label"]).strip(),
                    "cy_text":  nxt["cy_text"] or row["cy_text"],
                    "py_text":  nxt["py_text"] or row["py_text"],
                    "raw_line": (row["raw_line"] + " " + nxt["raw_line"]).strip(),
                })
                i += 2
                continue
        merged.append(row)
        i += 1
    return merged


# ── Balance sheet page finder ─────────────────────────────────────────────────

def _bs_score(text: str) -> int:
    t = text.lower()
    return sum([
        ("balance sheet"           in t) * 8,
        ("equity and liabilities"  in t) * 8,
        ("share capital"           in t) * 5,
        ("reserves"                in t) * 3,
        ("non current liabilities" in t) * 3,
        ("short term borrowings"   in t) * 3,
        ("trade payables"          in t) * 3,
    ])


def _find_bs_page(pdf_path: Path) -> int:
    candidates  = range(
        max(1, BS_PAGE_HINT - BS_SEARCH_WIN),
        min(BS_PAGE_HINT + BS_SEARCH_WIN + 1, 45),
    )
    best_page, best_score = BS_PAGE_HINT, 0
    for pg in candidates:
        try:
            imgs  = convert_from_path(pdf_path, first_page=pg, last_page=pg, dpi=DPI_SEARCH)
            text  = pytesseract.image_to_string(imgs[0], config=TESS_CONFIG)
            score = _bs_score(text)
            if score > best_score:
                best_score, best_page = score, pg
        except Exception:
            continue
    logger.info(f"Balance sheet → page {best_page} (score={best_score})")
    return best_page


def _company_name(pdf_path: Path) -> Optional[str]:
    try:
        imgs = convert_from_path(pdf_path, first_page=1, last_page=1, dpi=DPI_SEARCH)
        text = pytesseract.image_to_string(imgs[0], config=TESS_CONFIG)
        for line in text.splitlines():
            line = line.strip()
            if "limited" in line.lower() and len(line) > 5:
                return line
    except Exception:
        pass
    return None


# ── Trade payables (year-aware) ───────────────────────────────────────────────

def _process_trade_payables(
    rows: list[dict],
    page_num: int,
    current_fy: str,
    previous_fy: str,
) -> dict:
    """Extract MSME / Others / Total using the detected financial years as keys."""

    def _empty_sub():
        return {current_fy: _empty_yv(), previous_fy: _empty_yv()}

    detail = {"msme": _empty_sub(), "others": _empty_sub(), "total": _empty_sub()}

    def _has(text, kws):
        return any(k in text.lower() for k in kws)

    in_tp = False
    for row in rows:
        label, raw = row["label"].lower(), row["raw_line"].lower()

        if "trade payable" in label or "trade payable" in raw:
            in_tp = True
        if not in_tp:
            continue

        cy = _parse_amount(row["cy_text"]) if row["cy_text"] else None
        py = _parse_amount(row["py_text"]) if row["py_text"] else None
        if cy is None and py is None:
            nums = _all_amounts_in(row["raw_line"])
            cy = nums[0] if len(nums) > 0 else None
            py = nums[1] if len(nums) > 1 else None

        is_msme = (
            (_has(label, MSME_KEYWORDS) or _has(raw, MSME_KEYWORDS))
            and "other than" not in label
            and "other than" not in raw
        )
        is_others = (
            _has(label, OTHER_CREDITORS_KEYWORDS)
            or _has(raw, OTHER_CREDITORS_KEYWORDS)
            or "other than" in label
            or "other than" in raw
        )

        if is_msme:
            if cy is not None:
                detail["msme"][current_fy]  = _yv(cy, page_num, row["raw_line"], 100)
            if py is not None:
                detail["msme"][previous_fy] = _yv(py, page_num, row["raw_line"], 100)

        elif is_others:
            if cy is not None:
                detail["others"][current_fy]  = _yv(cy, page_num, row["raw_line"], 100)
            if py is not None:
                detail["others"][previous_fy] = _yv(py, page_num, row["raw_line"], 100)
            # compute totals
            for yr in (current_fy, previous_fy):
                m = detail["msme"][yr]["value"]
                o = detail["others"][yr]["value"]
                if m is not None and o is not None:
                    detail["total"][yr] = _yv(m + o, page_num, "MSME+Others", 100)
            in_tp = False

    return detail


# ── Main extraction function ──────────────────────────────────────────────────

def extract_cma_data(
    pdf_path,
    source_name: str = "",
    doc_id: str = "",
) -> dict:
    """
    Extract CMA Equity & Liabilities from a PDF.

    Returns
    -------
    {
      meta:                  { company_name, currency, unit, source_file,
                               balance_sheet_page, current_fy, previous_fy }
      fields:                { field_key → { label, section, section_label,
                                             current_year: YV, previous_year: YV } }
      trade_payables_detail: { msme/others/total → { fy → YV } }
      warnings:              [str]
    }
    Where YV = { value, source_page, matched_text, confidence }
    """
    pdf_path    = Path(pdf_path)
    source_name = source_name or pdf_path.name
    warnings    = []

    # Detect financial year first (fast, low-res)
    current_fy, previous_fy = detect_financial_year(pdf_path)

    fields = {
        key: {
            "label":         m["label"],
            "section":       m["section"],
            "section_label": SECTION_LABELS[m["section"]],
            "current_year":  _empty_yv(),
            "previous_year": _empty_yv(),
        }
        for key, m in CMA_FIELDS.items()
    }
    tp_detail    = None
    company_name = None
    bs_page      = None

    try:
        company_name = _company_name(pdf_path)
        bs_page      = _find_bs_page(pdf_path)

        images = convert_from_path(pdf_path, first_page=bs_page, last_page=bs_page, dpi=DPI_EXTRACT)
        if not images:
            warnings.append(f"Could not render page {bs_page}.")
            return _out(fields, tp_detail, company_name, bs_page,
                        source_name, doc_id, current_fy, previous_fy, warnings)

        df = _ocr_words(images[0])
        if df.empty:
            warnings.append("OCR returned no words.")
            return _out(fields, tp_detail, company_name, bs_page,
                        source_name, doc_id, current_fy, previous_fy, warnings)

        raw_rows = _build_raw_rows(df)
        rows     = _merge_split_rows(raw_rows)
        logger.info(f"Rows after merge: {len(rows)} (raw {len(raw_rows)})")

        matched: set[str] = set()

        for row in rows:
            label = row["label"]
            if not label or len(label.strip()) < 3:
                continue

            field_key, confidence = match_cma_field(label)
            if field_key is None or field_key in matched:
                continue

            # Deferred Tax Asset must NOT match deferred_tax_liabilities_net
            if field_key == "deferred_tax_liabilities_net" and "asset" in label.lower():
                continue

            cy = _parse_amount(row["cy_text"])
            py = _parse_amount(row["py_text"])

            if cy is None and py is None:
                nums = _all_amounts_in(row["raw_line"])
                cy = nums[0] if len(nums) > 0 else None
                py = nums[1] if len(nums) > 1 else None

            if cy is None and py is None:
                continue

            matched.add(field_key)
            fields[field_key]["current_year"]  = _yv(cy, bs_page, label, confidence)
            fields[field_key]["previous_year"] = _yv(py, bs_page, label, confidence)
            logger.debug(f"  {field_key}: CY={cy} PY={py} [{confidence}]")

        # Trade Payables
        tp_detail = _process_trade_payables(rows, bs_page, current_fy, previous_fy)
        tp_cy = tp_detail["total"].get(current_fy, {}).get("value")
        tp_py = tp_detail["total"].get(previous_fy, {}).get("value")
        if tp_cy or tp_py:
            fields["trade_payables"]["current_year"]  = _yv(tp_cy, bs_page, "MSME+Others", 100)
            fields["trade_payables"]["previous_year"] = _yv(tp_py, bs_page, "MSME+Others", 100)

        OPTIONAL = {
            "money_received_against_share_warrants",
            "share_application_money_pending_allotment",
            "deferred_tax_liabilities_net",
        }
        unmatched = [
            k for k, v in fields.items()
            if v["current_year"]["value"] is None
            and v["previous_year"]["value"] is None
            and k not in OPTIONAL
        ]
        if unmatched:
            warnings.append(f"Values not found for: {', '.join(unmatched)}")

    except Exception as exc:
        logger.exception("Extraction failed")
        warnings.append(f"Extraction error: {exc}")

    return _out(fields, tp_detail, company_name, bs_page,
                source_name, doc_id, current_fy, previous_fy, warnings)


def _out(fields, tp_detail, company, bs_page, source_name,
         doc_id, current_fy, previous_fy, warnings) -> dict:
    return {
        "meta": {
            "company_name":       company,
            "document_type":      "Standalone Balance Sheet",
            "currency":           "INR",
            "unit":               "Lakhs",
            "source_file":        source_name,
            "doc_id":             doc_id,
            "balance_sheet_page": bs_page,
            "current_fy":         current_fy,
            "previous_fy":        previous_fy,
        },
        "fields":                fields,
        "trade_payables_detail": tp_detail,
        "warnings":              warnings,
    }