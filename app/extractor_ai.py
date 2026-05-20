"""
OpenAI CMA extractor — v7.0

Critical fixes:
1. TWO-COLUMN EXTRACTION: Every Indian financial statement has 2 columns
   (current year LEFT, previous year RIGHT). We now explicitly extract BOTH
   columns separately, so 2022-23 doc gives correct values for both years.
2. Financial page pre-filtering: only financial pages sent to OpenAI.
3. CACHE_VERSION = "v7" — auto-invalidates all old incorrect caches.
4. Stronger prompt: explicit column instruction + bracketed negatives rule.
"""

import json, logging, os, time
from pathlib import Path
from typing import Optional

from openai import OpenAI, InternalServerError, RateLimitError
from app.cma_fields import CMA_SECTIONS

logger = logging.getLogger(__name__)

CACHE_VERSION = "v7"

# ── Cache ─────────────────────────────────────────────────────────────────────

def _ai_cache_dir() -> Path:
    d = Path(os.environ.get("UPLOADS_ROOT", "uploads")) / ".ai_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _load_ai_cache(doc_id: str) -> Optional[dict]:
    if not doc_id:
        return None
    try:
        cp = _ai_cache_dir() / f"{doc_id}.json"
        if cp.exists():
            data = json.loads(cp.read_text())
            if data.get("_cache_version") == CACHE_VERSION:
                logger.info(f"AI cache hit: {doc_id}")
                return data
            logger.info(f"Cache stale for {doc_id} — re-extracting")
    except Exception:
        pass
    return None

def _save_ai_cache(doc_id: str, data: dict) -> None:
    if not doc_id:
        return
    try:
        data["_cache_version"] = CACHE_VERSION
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

def clear_all_ai_caches() -> int:
    count = 0
    try:
        for f in _ai_cache_dir().glob("*.json"):
            f.unlink()
            count += 1
    except Exception as e:
        logger.warning(f"AI cache clear failed: {e}")
    return count

# ── OpenAI client ─────────────────────────────────────────────────────────────

_client: Optional[OpenAI] = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise EnvironmentError("OPENAI_API_KEY is not set")
        _client = OpenAI(api_key=key)
    return _client

# ── Context builder ───────────────────────────────────────────────────────────

MAX_CHARS = 36_000

def build_context(pages: list[dict]) -> str:
    """Build context from financial pages in document order."""
    ordered = sorted(pages, key=lambda p: p["page"])
    context = ""
    for p in ordered:
        chunk = f"\n=== Page {p['page']} ===\n{p['text']}\n"
        if len(context) + len(chunk) > MAX_CHARS:
            break
        context += chunk
    return context.strip()

# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_MSG = (
    "You are an expert financial analyst specialising in Indian company financials "
    "and CMA (Credit Monitoring Arrangement) data. "
    "You have deep knowledge of Schedule III format, Indian Accounting Standards (Ind AS), "
    "and can identify financial data accurately from OCR-extracted text."
)

# This prompt is the most critical part — it fixes the wrong-year-same-value bug
USER_TEMPLATE = """You are extracting data from a financial document for year: {current_fy}
The previous year column in this document represents: {previous_fy}

CRITICAL TWO-COLUMN RULE:
Indian financial statements ALWAYS have TWO columns of numbers:
- LEFT column = CURRENT YEAR = {current_fy}
- RIGHT column = PREVIOUS YEAR = {previous_fy}

Example: "Revenue from Operations  17,012.85  20,039.43"
- {current_fy} value = 17,012.85  (LEFT)
- {previous_fy} value = 20,039.43  (RIGHT)

SECTION: {section}
FIELDS TO EXTRACT:
{fields}

MANDATORY RULES:
1. Return ONLY valid JSON. No markdown, no explanation.
2. Extract BOTH years separately — they will have DIFFERENT values in most cases.
3. Keys must EXACTLY match the field names above.
4. Return this structure for each field:
   {{
     "current": {{"value": <number or null>, "confidence": <0.95-1.0>, "evidence": "<exact text from doc>", "page": <int>}},
     "previous": {{"value": <number or null>, "confidence": <0.95-1.0>, "evidence": "<exact text from doc>", "page": <int>}}
   }}
5. Values must be plain numbers (remove ₹, Rs., commas, "Lakhs"). Example: "1,020.00" → 1020.0
6. Bracketed numbers are NEGATIVE: "(485.16)" → -485.16
7. If a field is genuinely absent: {{"value": null, "confidence": 0, "evidence": "not found", "page": null}}
8. NEVER return the same value for both current and previous year unless the document actually shows the same number in both columns.
9. Confidence 0.95+ = found directly in table. 0.8-0.94 = computed/inferred. Below 0.8 = uncertain.
10. Prefer table values over narrative text.

SYNONYMS (use these to find fields even if named differently):
- "Net Sales" = "Revenue from Operations" = "Turnover"
- "Net Profit/Loss (PAT)" = "Profit for the year" = "PAT"
- "Profit before Tax" = "PBT" = "Profit Before Taxation"
- "Depreciation" = "Depreciation and Amortization Expense" = "D&A"
- "Capital" = "Share Capital" = "Paid Up Capital"
- "Other reserves" = "Reserves and Surplus" = "Retained Earnings"
- "Net Worth" = "Shareholders Funds" = "Total Equity"
- "Total Current Assets" = "Current Assets Total"
- "Total Current Liabilities" = "Current Liabilities Total"
- "Short Term loans from Applicant Bank" = "Cash Credit" = "Working Capital Loan"
- "Domestic Receivables" = "Trade Receivables" = "Sundry Debtors"
- "Sundry Creditors (Trade)" = "Trade Payables"
- "Cash & Bank Balances" = "Cash and Cash Equivalents" + "Bank Balance other than cash equivalents"
- "Total Interest" = "Finance Costs" = "Finance Expenses"
- "PBDIT" = "EBITDA" = PBT + Interest + Depreciation
- "Cash Accruals" = PAT + Depreciation
- "Long Term provisions" = "Long Term Provisions" (gratuity, leave encashment)
- "Installments of term Loan" = "Current Maturities of Long Term Debt"
- "Deferred Tax Asset" = "Deferred Tax Asset (Net)" = "DTA"

DOCUMENT TEXT (financial pages only):
{context}"""

# ── OpenAI call ───────────────────────────────────────────────────────────────

SPLIT_AT = 7

def _openai_call(
    section_label: str,
    fields: list[str],
    context: str,
    current_fy: str,
    previous_fy: str,
) -> dict:
    client = _get_client()
    prompt = USER_TEMPLATE.format(
        current_fy   = current_fy,
        previous_fy  = previous_fy,
        section      = section_label,
        fields       = "\n".join(f"- {f}" for f in fields),
        context      = context,
    )
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model           = os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages        = [
                    {"role": "system", "content": SYSTEM_MSG},
                    {"role": "user",   "content": prompt},
                ],
                temperature     = 0,
                response_format = {"type": "json_object"},
                timeout         = 90,
            )
            raw = resp.choices[0].message.content
            return json.loads(raw)
        except (InternalServerError, RateLimitError) as e:
            wait = 5 * (attempt + 1)
            logger.warning(f"Rate/server error ({attempt+1}/3) '{section_label}': {e}. Wait {wait}s")
            time.sleep(wait)
        except Exception as e:
            logger.error(f"OpenAI error '{section_label}': {e}")
            break
    return {}

def _norm_entry(entry) -> dict:
    """Normalise a single year's field entry to standard format."""
    if not isinstance(entry, dict):
        return {"value": None, "confidence": 0, "evidence": "parse error", "page": None}
    val = entry.get("value")
    conf = float(entry.get("confidence") or 0)
    if val == 0 and conf == 0:
        val = None
    if isinstance(val, float):
        val = round(val, 4)
    return {
        "value":      val,
        "confidence": round(conf, 2),
        "evidence":   str(entry.get("evidence") or "")[:300],
        "page":       entry.get("page"),
    }

def _extract_section(
    section_key: str,
    meta: dict,
    context: str,
    current_fy: str,
    previous_fy: str,
) -> dict[str, dict]:
    """
    Extract all fields in one section.
    Returns {field_name: {current: {...}, previous: {...}}}
    """
    fields = meta["fields"]
    label  = meta["label"]

    if len(fields) > SPLIT_AT:
        mid  = len(fields) // 2
        raw  = _openai_call(f"{label} (1/2)", fields[:mid],  context, current_fy, previous_fy)
        raw2 = _openai_call(f"{label} (2/2)", fields[mid:],  context, current_fy, previous_fy)
        raw.update(raw2)
    else:
        raw = _openai_call(label, fields, context, current_fy, previous_fy)

    result = {}
    for field in fields:
        entry = raw.get(field, {})
        if isinstance(entry, dict) and ("current" in entry or "previous" in entry):
            # New two-column format
            result[field] = {
                "current":  _norm_entry(entry.get("current", {})),
                "previous": _norm_entry(entry.get("previous", {})),
            }
        else:
            # Fallback: old format or raw value — treat as current year only
            result[field] = {
                "current":  _norm_entry(entry),
                "previous": {"value": None, "confidence": 0, "evidence": "not extracted", "page": None},
            }
    return result

# ── Post-extraction computed fields ───────────────────────────────────────────

def _safe(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def _compute_for_year(fields_by_section: dict, year_key: str) -> dict:
    """
    Compute derived fields for one year (current or previous).
    Fills in fields that are null but calculable from other extracted values.
    Returns {section_key: {field_name: {value, confidence, evidence, page}}}
    """
    def _get(sk: str, fn: str):
        return _safe(fields_by_section.get(sk, {}).get(fn, {}).get(year_key, {}).get("value"))

    def _set(sk: str, fn: str, val: float, ev: str):
        if sk in fields_by_section and fn in fields_by_section[sk]:
            existing = fields_by_section[sk][fn].get(year_key, {})
            if existing.get("value") is None and val is not None:
                fields_by_section[sk][fn][year_key] = {
                    "value":      round(val, 4),
                    "confidence": 0.92,
                    "evidence":   f"[COMPUTED] {ev}",
                    "page":       None,
                }

    ns  = _get("sales",           "Net Sales")
    pat = _get("pnl",             "Net Profit/Loss (PAT)")
    pbt = _get("pnl",             "Profit before Tax")
    dep = _get("cost_of_sales",   "Depreciation") or _get("pnl", "Depreciation adjustments")
    int_= _get("interest",        "Total Interest")
    ca  = _get("current_assets",  "Total Current Assets")
    cl  = _get("current_liabilities", "Total Current Liabilities")
    tl  = _get("term_liabilities", "Total Term Liabilities")
    tol = _get("term_liabilities", "Total Outside Liabilities")
    nw  = _get("net_worth",       "Net Worth")
    ta  = _get("intangibles",     "Total Assets")
    intg = _get("intangibles",    "Total Intangible Assets")
    op  = _get("cost_of_sales",   "Total Cost of Sales")
    div = _get("pnl",             "Dividend paid") or 0
    sc  = _get("net_worth",       "Capital")
    res = _get("net_worth",       "Other reserves")
    dr  = _get("current_assets",  "Domestic Receivables")
    tp  = _get("current_liabilities", "Sundry Creditors (Trade)")
    ocl = _get("current_liabilities", "Other current Liabilities")

    # Net Worth from components
    if sc is not None and res is not None:
        _set("net_worth", "Net Worth", sc + res, f"ShareCap({sc}) + Reserves({res})")
    nw = _get("net_worth", "Net Worth") or nw

    # Cash Accruals = PAT + Dep
    if pat is not None and dep is not None:
        _set("pnl", "Cash Accruals", pat + dep, f"PAT({pat}) + Dep({dep})")
    cash_acc = _get("pnl", "Cash Accruals")

    # Retained Profit / Cash Profits
    if pat is not None:
        _set("pnl", "Retained Profit",        pat - div, f"PAT({pat}) - Div({div})")
    if cash_acc is not None:
        _set("pnl", "Retained Cash Profits",  cash_acc - div, f"CashAccruals({cash_acc}) - Div({div})")

    # PBDIT = PBT + Interest + Dep
    if pbt is not None and int_ is not None and dep is not None:
        pbdit = pbt + int_ + dep
        _set("pnl", "PBDIT", pbdit, f"PBT({pbt}) + Int({int_}) + Dep({dep})")
    pbdit = _get("pnl", "PBDIT")

    # Operating Profit before interest = PBT + Interest
    if pbt is not None and int_ is not None:
        _set("profitability", "Operating Profit before interest",
             pbt + int_, f"PBT({pbt}) + Int({int_})")

    # Gross Profit = Net Sales - Cost of Sales
    if ns is not None and op is not None:
        _set("profitability", "Gross profit", ns - op, f"Sales({ns}) - CostOfSales({op})")
    gp = _get("profitability", "Gross profit")

    # Ratios
    if ns and ns != 0:
        if gp is not None:
            _set("profitability", "Gross Profit/Sales", gp / ns, f"GP({gp})/Sales({ns})")
        if pbdit is not None:
            _set("pnl", "PBDIT/Sales", pbdit / ns, f"PBDIT({pbdit})/Sales({ns})")
        opbi = _get("profitability", "Operating Profit before interest")
        if opbi is not None:
            _set("pnl", "Operating Profits/Sales", opbi / ns, f"OpProfit({opbi})/Sales({ns})")
        if pbt is not None:
            _set("pnl", "PBT/Sales", pbt / ns, f"PBT({pbt})/Sales({ns})")
        if pat is not None:
            _set("pnl", "PAT/Sales", pat / ns, f"PAT({pat})/Sales({ns})")
        ca2 = _get("pnl", "Cash Accruals")
        if ca2 is not None:
            _set("pnl", "Cash Accruals/Sales", ca2 / ns, f"CashAcc({ca2})/Sales({ns})")
        if op is not None:
            _set("pnl", "RM Content in sales", op / ns, f"OpEx({op})/Sales({ns})")
        if dr is not None:
            days = dr / ns * 365
            _set("working_capital", "Domestic receivables - Days Gross Domestic Sales",
                 round(days, 1), f"({dr}/{ns})*365")
        total_recv = _get("working_capital", "Total Receivables") or dr
        if total_recv is not None:
            _set("working_capital", "Total Receivables/Gross Sales",
                 round(total_recv / ns * 365, 1), f"({total_recv}/{ns})*365 days")

    # NWC, TNW, ratios
    tnw = (nw - intg) if (nw is not None and intg is not None) else nw
    if tnw is not None:
        _set("ratios", "Tangible Net Worth", tnw, f"NetWorth({nw}) - Intangibles({intg or 0})")

    if ca is not None and cl is not None:
        nwc = ca - cl
        _set("ratios", "Net Working Capital", nwc, f"CA({ca}) - CL({cl})")
        if cl != 0:
            _set("ratios", "Current Ratio", round(ca / cl, 2), f"CA({ca})/CL({cl})")
        nwc2 = _get("ratios", "Net Working Capital")
        if nwc2 is not None and ca != 0:
            _set("working_capital", "NWC % to Current Assets",
                 round(nwc2 / ca * 100, 2), f"NWC({nwc2})/CA({ca})*100")

    if tl is not None and tnw is not None and tnw != 0:
        _set("ratios", "Debt/Equity",    round(tl / tnw, 2), f"TermLiab({tl})/TNW({tnw})")
    if tol is not None and tnw is not None and tnw != 0:
        _set("ratios", "TOL/Equity",     round(tol / tnw, 2), f"TOL({tol})/TNW({tnw})")
    if pbdit is not None and ta is not None and ta != 0:
        _set("ratios", "ROCE",           round(pbdit / ta * 100, 2), f"PBDIT({pbdit})/TotalAssets({ta})*100")

    # Working capital gap
    if ca is not None and tp is not None:
        other = ocl or 0
        _set("working_capital", "Working Capital gap",
             round(ca - tp - other, 2), f"CA({ca}) - TradePay({tp}) - OtherCL({other})")

    # Break even
    sales_be = _get("break_even", "Sales") or ns
    vc       = _get("break_even", "Total Variable Costs")
    fc       = _get("break_even", "Fixed Costs")
    dep_be   = dep or 0
    if sales_be and vc and fc and sales_be != 0:
        contrib = sales_be - vc
        _set("break_even", "Contribution", contrib, f"Sales({sales_be}) - VarCosts({vc})")
        if contrib > 0:
            bep = fc / (contrib / sales_be)
            _set("break_even", "Break Even Level of Sales", round(bep, 2),
                 f"FC({fc})/(Contrib({contrib})/Sales({sales_be}))")
            _set("break_even", "Cash Break Even of Sales",
                 round((fc - dep_be) / (contrib / sales_be), 2),
                 f"(FC({fc})-Dep({dep_be}))/PVRatio")

    # Depreciation adjustments = Depreciation
    if dep is not None:
        _set("pnl", "Depreciation adjustments", dep, f"Same as Depreciation({dep})")

    # Transfer to Reserves default 0
    if fields_by_section.get("pnl", {}).get("Transfer to Reserves", {}).get(year_key, {}).get("value") is None:
        _set("pnl", "Transfer to Reserves", 0, "Not mentioned in doc — defaulted to 0")

    return fields_by_section

# ── Public API ────────────────────────────────────────────────────────────────

def extract_cma_fields(
    pages:        list[dict],
    source_file:  str = "",
    doc_id:       str = "",
    current_fy:   str = "unknown",
    previous_fy:  str = "unknown",
) -> dict:
    """
    Extract all CMA fields for BOTH years from a document.

    Parameters
    ----------
    pages       : all pages from pdf_reader (already OCR'd)
    source_file : original filename
    doc_id      : stored doc_id for caching
    current_fy  : e.g. "2022-23" (LEFT column in financial statement)
    previous_fy : e.g. "2021-22" (RIGHT column in financial statement)

    Returns
    -------
    {
      meta: { ... },
      sections: {
        section_key: {
          label: str,
          fields: {
            field_name: {
              "current":  { value, confidence, evidence, page },
              "previous": { value, confidence, evidence, page }
            }
          }
        }
      }
    }
    """
    # Cache hit → return immediately
    cached = _load_ai_cache(doc_id)
    if cached:
        return cached

    # Filter to financial pages only
    from app.pdf_reader import filter_financial_pages
    financial_pages = filter_financial_pages(pages)
    context = build_context(financial_pages)

    sections_out = {}
    found_total  = 0
    total_fields = 0

    for section_key, meta in CMA_SECTIONS.items():
        logger.info(f"Extracting: {meta['label']} ({len(meta['fields'])} fields) | FY={current_fy}/{previous_fy}")
        fields_result = _extract_section(section_key, meta, context, current_fy, previous_fy)

        found = sum(
            1 for v in fields_result.values()
            if v.get("current", {}).get("value") is not None
            or v.get("previous", {}).get("value") is not None
        )
        found_total  += found
        total_fields += len(fields_result)

        sections_out[section_key] = {
            "label":  meta["label"],
            "fields": fields_result,
        }
        logger.info(f"  → {found}/{len(fields_result)} found")

    # Compute derived fields for both years
    # Build a lookup structure for _compute_for_year
    fields_lookup: dict = {
        sk: {fn: sv["fields"][fn] for fn in sv["fields"]}
        for sk, sv in sections_out.items()
    }

    for year_key in ("current", "previous"):
        _compute_for_year(fields_lookup, year_key)

    # Write computed values back into sections_out
    for sk in sections_out:
        for fn in sections_out[sk]["fields"]:
            sections_out[sk]["fields"][fn] = fields_lookup[sk][fn]

    # Recount after computation
    found_after = sum(
        1 for sv in sections_out.values()
        for fv in sv["fields"].values()
        if fv.get("current", {}).get("value") is not None
        or fv.get("previous", {}).get("value") is not None
    )

    result = {
        "meta": {
            "source_file":      source_file,
            "doc_id":           doc_id,
            "current_fy":       current_fy,
            "previous_fy":      previous_fy,
            "total_fields":     total_fields,
            "fields_found":     found_after,
            "fields_not_found": total_fields - found_after,
            "coverage_pct":     round(found_after / total_fields * 100, 1) if total_fields else 0,
            "financial_pages":  len(financial_pages),
            "total_pages":      len(pages),
        },
        "sections": sections_out,
    }

    _save_ai_cache(doc_id, result)
    return result