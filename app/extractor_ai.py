"""
OpenAI CMA extractor — v6.0

Improvements over v5:
1. CORS fix in main.py (handled separately)
2. Comprehensive synonym map embedded in every prompt
3. Computed/derived fields post-extraction (BEP, NWC%, ratios, etc.)
4. Full-context mode: all pages sent in order, no truncation priority trick
5. Per-section retry on failure
6. Cache stores version key — old caches auto-invalidated on upgrade
"""

import json, logging, math, os, time
from pathlib import Path
from typing import Optional

from openai import OpenAI, InternalServerError, RateLimitError
from app.cma_fields import CMA_SECTIONS

logger = logging.getLogger(__name__)

CACHE_VERSION = "v6"   # bump this to invalidate all old caches

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
            logger.info(f"Cache version mismatch for {doc_id}, re-extracting")
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

# ── Context ───────────────────────────────────────────────────────────────────

MAX_CHARS = 38_000

def build_context(pages: list[dict]) -> str:
    """All pages in document order — financials usually pages 10-40."""
    ordered = sorted(pages, key=lambda p: p["page"])
    context = ""
    for p in ordered:
        chunk = f"\n=== Page {p['page']} ===\n{p['text']}\n"
        if len(context) + len(chunk) > MAX_CHARS:
            break
        context += chunk
    return context.strip()

# ── Comprehensive synonym map ─────────────────────────────────────────────────

SYNONYMS = """
CRITICAL SYNONYM & MAPPING RULES — apply these when extracting fields:

SALES & REVENUE:
- "Net Sales" = "Revenue from Operations" = "Total Revenue" = "Turnover" = "Net Revenue"
- "Domestic Sale" = "Domestic Revenue" = "Local Sales" — if no split, use Total Net Sales
- "Export Sale" = "Export Revenue" = "Foreign Sales"
- "Growth in sales" = calculate % change: (CurrentYear - PreviousYear) / PreviousYear × 100

COSTS:
- "Purchases" = "Cost of Materials Consumed" = "Raw Material Consumed" = "Operating Expenses" = "Freight & Handling Expenses" = "Cost of Services"
- "Services" = "Service Charges" = "Subcontracting" = "Job Work Charges"
- "Repair & Maintenance" = "Repairs and Maintenance" = "R&M Expenses"
- "Fuel Expenses" = "Power & Fuel" = "Power and Fuel" = "Electricity Charges"
- "Godown Rent" = "Warehouse Rent" = "Storage Charges" = "Rent Expense" (if only one rent line)
- "Transport Expenses" = "Freight Expenses" = "Freight & Handling" = "Carriage" = "Cartage"
- "Loading & unloading charges" = "Loading/Unloading" = "Handling Charges"
- "Packing and Forwarding expenses" = "Packing Charges" = "Forwarding Charges"
- "Depreciation" = "Depreciation and Amortization" = "Depreciation & Amortization Expense" = "D&A"
- "Total Cost of Sales" = "Total Expenses" = "Total Expenditure" = "Cost of Revenue"
- "Other Manufacturing Expenses" = "Other Operating Expenses" = "Other Expenses" (in cost section)

PROFITABILITY:
- "Gross profit" = Revenue from Operations minus Operating/Direct Expenses
- "Gross Profit/Sales" = Gross Profit ÷ Net Sales (as ratio, e.g. 0.09 for 9%)
- "PBDIT" = "EBITDA" = Profit Before Depreciation Interest and Tax = PBT + Interest + Depreciation
- "Operating Profit before interest" = EBIT = PBT + Interest = PBDIT - Depreciation
- "Operating Profit after Interest" = PBT = "Profit before Tax" = "Profit Before Taxation"
- "Selling Expenses" = "Selling & Distribution Expenses" = "Marketing Expenses"
- "Administrative Expenses" = "General & Administrative Expenses" = "G&A Expenses" = "Overheads"

INTEREST & FINANCE:
- "Interest on CC" = "Bank Interest" = "Interest on Cash Credit" = "Interest on Working Capital" = "Finance Costs" (if single line)
- "Interest on TL" = "Interest on Term Loan" = "Interest on Long Term Borrowings"
- "Total Interest" = "Finance Costs" = "Finance Expenses" = "Total Finance Charges"

OTHER INCOME:
- "Other Income" = "Non-operating Income" = "Miscellaneous Income"
- "Creditors written back" = "Sundry Balance Written Back" = "Creditors Written Off" = "Liabilities Written Back"
- "Interest/Dividend/Royalties" = "Interest Income" + "Dividend Income" (sum both)

P&L:
- "Profit before Tax" = "PBT" = "Profit Before Taxation" = "Net Profit Before Tax"
- "Net Profit/Loss (PAT)" = "Profit for the year" = "PAT" = "Net Profit" = "Profit After Tax"
- "Provision for Taxes" = "Tax Expense" = "Income Tax" = "Current Tax" + "Deferred Tax" (sum)
- "Cash Accruals" = PAT + Depreciation (compute if not stated)
- "Retained Profit" = PAT - Dividend (or Surplus in P&L account closing balance)
- "Retained Cash Profits" = Cash Accruals - Dividend
- "PBDIT/Sales" = PBDIT ÷ Net Sales
- "Operating Profits/Sales" = Operating Profit ÷ Net Sales
- "PBT/Sales" = PBT ÷ Net Sales
- "PAT/Sales" = PAT ÷ Net Sales
- "Cash Accruals/Sales" = Cash Accruals ÷ Net Sales
- "RM Content in sales" = Raw Material / Net Sales (or Operating Expenses / Net Sales for service cos)
- "Transfer to Reserves" = Amount transferred to General Reserve (0 if not mentioned)
- "Depreciation adjustments" = same as Depreciation figure

CURRENT LIABILITIES:
- "Short Term loans from Applicant Bank" = "Cash Credit" = "CC Limit" = "Bank OD" = "Working Capital Loan" (from main bank)
- "Short Term loans From Other banks" = CC/OD from other banks
- "Short Term Borrowings from Others" = "Loans from NBFCs" = "Short Term Loans Others"
- "Sundry Creditors (Trade)" = "Trade Payables" = "Creditors" = "Accounts Payable"
- "Advance Payment from Customers" = "Customer Advances" = "Advances from Customers"
- "Net Provision for Taxation" = "Provision for Tax" = "Tax Payable" = "Current Tax Payable"
- "Other Statutory Liabilities" = "Statutory Dues" = "TDS Payable" = "GST Payable" = "PF Payable" = "ESI Payable"
- "Installments of term Loan" = "Current Maturity of Long Term Debt" = "Current Maturities of Borrowings"
- "Other Current Liabilities & Provisions" = "Other Current Liabilities" + "Short Term Provisions" (sum)
- "Other current Liabilities" = "Other Current Liabilities" line item
- "Provision for Others" = "Short Term Provisions" = "Other Provisions"
- "Other debt due within one year-Unsecured Loans" = "Current maturities of unsecured loans" = "Director Loans (current portion)"
- "Total Current Liabilities" = sum of all current liability items

TERM LIABILITIES:
- "Term Loan from Bank" = "Long Term Borrowings" (secured from banks, excluding current maturities)
- "Term Loan from Other Banks/Institutions" = "Loans from NBFCs" = "Vehicle Loans" (long term)
- "Long Term provisions" = "Long Term Provisions" = "Provision for Gratuity" (long term portion)
- "Unsecured Loans" = "Director Loans" = "Loans from Directors" (long term)
- "Other debts - Unsecured Loans" = "Loan from Others" = "Inter-corporate Deposits"
- "Total Term Liabilities" = "Non-Current Liabilities" total
- "Total Outside Liabilities" = Total Current Liabilities + Total Term Liabilities

NET WORTH:
- "Capital" = "Share Capital" = "Paid Up Capital" = "Equity Share Capital"
- "Other reserves" = "Reserves and Surplus" = "Retained Earnings" = "Securities Premium"
- "Surplus or deficit in Profit & Loss account" = "P&L Account Balance" = "Retained Earnings"
- "Net Worth" = "Shareholders Funds" = Share Capital + Reserves and Surplus
- "Total Liabilities" = "Total Assets" = Balance Sheet Total
- "Unsecured loan as Quasi Capital" = "Director Loans" treated as quasi-equity

CURRENT ASSETS:
- "Cash & Bank Balances" = "Cash and Cash Equivalents" + "Bank Balance other than cash equivalents"
- "Fixed Deposits with Banks" = "Bank Fixed Deposits" = "FD with Banks" = "Bank Balance (FD)"
- "Domestic Receivables" = "Trade Receivables" = "Sundry Debtors" = "Accounts Receivable"
- "Advances to Suppliers" = "Advance to Suppliers" = "Advances Paid" = "Advance to Vendors"
- "Net Advance Payment of Taxes" = "Advance Tax" = "TDS Receivable" = "Advance Tax & TDS"
- "Other Current Assets" = "Prepaid Expenses" + "Other Current Assets"
- "Current Investments & Loans and Advances" = "Short Term Loans and Advances"
- "Total Current Assets" = sum of all current asset items

FIXED ASSETS:
- "Gross Block" = "Property Plant and Equipment (Gross)" = "Fixed Assets (at cost)"
- "Capital expenditure in work-in-process" = "Capital Work in Progress" = "CWIP"
- "Depreciation to Date" = "Accumulated Depreciation" = "Less: Depreciation"
- "Net Block" = "Property Plant and Equipment (Net)" = "Net Fixed Assets" = Gross Block - Accumulated Depreciation
- "Deferred Tax Asset" = "Deferred Tax Asset (Net)" = "DTA"
- "Total Other Non Current Assets" = "Other Non Current Assets"
- "Investment in Others" = "Non-Current Investments" = "Long Term Investments"
- "Deposits for Godown & Office" = "Security Deposits" = "Office Deposits"

INTANGIBLES:
- "Intangible Assets" = Software + Goodwill + Patents (net block)
- "Total Intangible Assets" = sum of all intangible items

RATIOS:
- "Tangible Net Worth" = Net Worth - Intangible Assets
- "Net Working Capital" = Total Current Assets - Total Current Liabilities
- "Current Ratio" = Total Current Assets / Total Current Liabilities
- "Debt/Equity" = Long Term Debt / Net Worth (or TNW)
- "TOL/Equity" = Total Outside Liabilities / Net Worth
- "ROCE" = PBDIT / Total Capital Employed × 100 (or as ratio)

ADDITIONAL INFO:
- "Contingent Liabilities" = look in Notes for "Contingent Liabilities" or "Claims against the company"
- "Gratuity Liability" = "Provision for Gratuity" = total gratuity obligation
- "Disputed Tax Liabilities" = "Disputed Custom/Excise/Tax" = tax demands under dispute

WORKING CAPITAL:
- "Total Receivables" = Domestic Receivables + Export Receivables
- "Bank Finance" = Total Short Term Bank Borrowings = Cash Credit + Other bank limits
- "Working Capital gap" = Total Current Assets - Creditors - Other Current Liabilities (non-bank)
- "NWC % to Current Assets" = Net Working Capital / Total Current Assets × 100
- "Domestic receivables - Days Gross Domestic Sales" = (Domestic Receivables / Net Sales) × 365
- "Total Receivables/Gross Sales" = Total Receivables / Net Sales (as ratio or days)

FUND FLOW (from Cash Flow Statement):
- "Increase in Bank Borrowings" = Net proceeds from short term borrowings
- "Decrease in Receivables" = Reduction in trade receivables (positive means decrease)
- "Increase in Receivables" = Increase in trade receivables
- "Decrease in Cash/Deposits" = Net decrease in cash position
- "Increase in Cash/Deposits" = Net increase in cash position

BREAK EVEN (compute from available data):
- "Sales" = Net Sales = Revenue from Operations
- "Variable Cost" = Operating/Direct Costs (excl. fixed overheads)
- "Total Variable Costs" = same as Variable Cost
- "Fixed Costs" = Depreciation + Interest + Fixed Overheads
- "Contribution" = Sales - Variable Costs
- "Break Even Level of Sales" = Fixed Costs / (Contribution / Sales) — compute if data available
- "Cash Break Even of Sales" = (Fixed Costs - Depreciation) / (Contribution / Sales)
"""

# ── Prompt template ───────────────────────────────────────────────────────────

SYSTEM_MSG = (
    "You are an expert financial analyst specialising in Indian company financials "
    "and CMA (Credit Monitoring Arrangement) data preparation for bank loans. "
    "You understand Indian accounting standards, Schedule III format, and can "
    "recognise equivalent terms across different financial statement formats."
)

USER_TEMPLATE = """{synonyms}

Extract the following CMA financial fields from the document text below.

SECTION: {section}
FIELDS TO EXTRACT:
{fields}

EXTRACTION RULES:
1. Return ONLY a valid JSON object. No markdown, no explanation.
2. Keys must exactly match the field names listed above.
3. Each value: {{"value": <number or null>, "confidence": <0.0-1.0>, "evidence": "<direct quote>", "page": <int or null>}}
4. Numbers only (strip ₹, commas, lakhs notation). E.g. "17,012.85 lakhs" → 17012.85
5. Bracketed numbers are NEGATIVE: "(485.16)" → -485.16
6. If field genuinely absent from this document: {{"value": null, "confidence": 0, "evidence": "not found in document", "page": null}}
7. ALWAYS use synonyms above to find alternative names for fields.
8. For ratio fields: compute from available data if not directly stated. Set confidence 0.85.
9. For computed fields (Cash Accruals = PAT + Depreciation): calculate and set confidence 0.9.
10. "not found" must be a LAST RESORT — first check all synonyms and alternative names.
11. Confidence >= 0.9 = directly found; 0.7-0.9 = computed/inferred; < 0.7 = uncertain.
12. Current year column is LEFT, previous year is RIGHT in Indian financial statements.

DOCUMENT TEXT:
{context}"""

# ── OpenAI call ───────────────────────────────────────────────────────────────

SPLIT_AT = 7  # split sections with > this many fields

def _openai_call(section_label: str, fields: list[str], context: str) -> dict:
    client = _get_client()
    prompt = USER_TEMPLATE.format(
        synonyms      = SYNONYMS,
        section       = section_label,
        fields        = "\n".join(f"- {f}" for f in fields),
        context       = context,
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


def _norm(entry, field: str) -> dict:
    """Normalise any OpenAI response entry to standard dict."""
    if not isinstance(entry, dict):
        return {
            "value":      entry if isinstance(entry, (int, float)) else None,
            "confidence": 0.5,
            "evidence":   str(entry)[:200],
            "page":       None,
        }
    val = entry.get("value")
    # reject value=0 with confidence=0 (means "not found")
    conf = float(entry.get("confidence") or 0)
    if val == 0 and conf == 0:
        val = None
    return {
        "value":      round(val, 4) if isinstance(val, float) else val,
        "confidence": round(conf, 2),
        "evidence":   str(entry.get("evidence") or "")[:300],
        "page":       entry.get("page"),
    }


def _extract_section(section_key: str, meta: dict, context: str) -> dict[str, dict]:
    """Extract all fields in a section, splitting large ones."""
    fields = meta["fields"]
    label  = meta["label"]

    if len(fields) > SPLIT_AT:
        mid  = len(fields) // 2
        raw  = _openai_call(f"{label} (1/2)", fields[:mid],  context)
        raw2 = _openai_call(f"{label} (2/2)", fields[mid:], context)
        raw.update(raw2)
    else:
        raw = _openai_call(label, fields, context)

    return {field: _norm(raw.get(field, {}), field) for field in fields}

# ── Post-extraction computed fields ──────────────────────────────────────────

def _safe(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _compute_derived(sections: dict) -> dict:
    """
    Calculate fields that can be derived from already-extracted values.
    Only fills in fields that are still null.
    Returns sections with computed values added.
    """
    def _get(section_key: str, field: str):
        return _safe(sections.get(section_key, {})
                     .get("fields", {}).get(field, {}).get("value"))

    def _set(section_key: str, field: str, value: float, evidence: str):
        if field in sections.get(section_key, {}).get("fields", {}):
            existing = sections[section_key]["fields"][field]
            if existing.get("value") is None and value is not None:
                sections[section_key]["fields"][field] = {
                    "value":      round(value, 4),
                    "confidence": 0.88,
                    "evidence":   f"[COMPUTED] {evidence}",
                    "page":       None,
                }

    # ── Core financials ───────────────────────────────────────────────────────
    net_sales   = _get("sales",         "Net Sales")
    pat         = _get("pnl",           "Net Profit/Loss (PAT)")
    pbt         = _get("pnl",           "Profit before Tax")
    dep         = _get("cost_of_sales", "Depreciation") or _get("pnl", "Depreciation adjustments")
    interest    = _get("interest",      "Total Interest")
    op_exp      = _get("cost_of_sales", "Total Cost of Sales")
    dividend    = _get("pnl",           "Dividend paid")
    tax         = _get("pnl",           "Provision for Taxes")
    gross_prof  = _get("profitability", "Gross profit")
    op_b_int    = _get("profitability", "Operating Profit before interest")
    curr_assets = _get("current_assets", "Total Current Assets")
    curr_liab   = _get("current_liabilities", "Total Current Liabilities")
    total_assets = _get("intangibles",  "Total Assets")
    net_worth   = _get("net_worth",     "Net Worth")
    term_liab   = _get("term_liabilities", "Total Term Liabilities")
    tol         = _get("term_liabilities", "Total Outside Liabilities")
    net_block   = _get("fixed_assets",  "Net Block")
    intangibles = _get("intangibles",   "Total Intangible Assets")
    dom_recv    = _get("current_assets", "Domestic Receivables")
    trade_pay   = _get("current_liabilities", "Sundry Creditors (Trade)")
    bank_fin    = _get("working_capital", "Bank Finance")
    share_cap   = _get("net_worth",     "Capital")
    reserves    = _get("net_worth",     "Other reserves")

    # Cash Accruals = PAT + Depreciation
    if pat is not None and dep is not None:
        cash_acc = pat + dep
        _set("pnl", "Cash Accruals", cash_acc, f"PAT({pat}) + Dep({dep})")
    else:
        cash_acc = _get("pnl", "Cash Accruals")

    # Retained Profit = PAT - Dividend
    if pat is not None:
        div = dividend or 0
        _set("pnl", "Retained Profit", pat - div, f"PAT({pat}) - Dividend({div})")

    # Retained Cash Profits = Cash Accruals - Dividend
    ca = cash_acc or _get("pnl", "Cash Accruals")
    if ca is not None:
        div = dividend or 0
        _set("pnl", "Retained Cash Profits", ca - div, f"CashAccruals({ca}) - Div({div})")

    # PBDIT = PBT + Interest + Depreciation
    if pbt is not None and interest is not None and dep is not None:
        pbdit = pbt + interest + dep
        _set("pnl", "PBDIT", pbdit, f"PBT({pbt}) + Int({interest}) + Dep({dep})")
    elif pbt is not None and dep is not None and interest is None:
        pbdit_no_int = pbt + dep
        _set("pnl", "PBDIT", pbdit_no_int, f"PBT({pbt}) + Dep({dep})")
    pbdit = _get("pnl", "PBDIT")

    # Operating Profit before interest = PBT + Interest
    if pbt is not None and interest is not None:
        _set("profitability", "Operating Profit before interest",
             pbt + interest, f"PBT({pbt}) + Int({interest})")
    op_b_int = _get("profitability", "Operating Profit before interest")

    # Gross Profit = Net Sales - Operating Expenses (direct costs)
    if net_sales is not None and op_exp is not None:
        _set("profitability", "Gross profit", net_sales - op_exp,
             f"Sales({net_sales}) - OpEx({op_exp})")
    gp = _get("profitability", "Gross profit")

    # Growth in sales (requires both years — skip, AI handles this)

    # Ratio fields
    if net_sales and net_sales != 0:
        if gp is not None:
            _set("profitability", "Gross Profit/Sales", gp / net_sales,
                 f"GP({gp}) / Sales({net_sales})")
        if pbdit is not None:
            _set("pnl", "PBDIT/Sales", pbdit / net_sales,
                 f"PBDIT({pbdit}) / Sales({net_sales})")
        if op_b_int is not None:
            _set("pnl", "Operating Profits/Sales", op_b_int / net_sales,
                 f"OpProfit({op_b_int}) / Sales({net_sales})")
        if pbt is not None:
            _set("pnl", "PBT/Sales", pbt / net_sales,
                 f"PBT({pbt}) / Sales({net_sales})")
        if pat is not None:
            _set("pnl", "PAT/Sales", pat / net_sales,
                 f"PAT({pat}) / Sales({net_sales})")
        ca2 = _get("pnl", "Cash Accruals")
        if ca2 is not None:
            _set("pnl", "Cash Accruals/Sales", ca2 / net_sales,
                 f"CashAccruals({ca2}) / Sales({net_sales})")
        if op_exp is not None:
            _set("pnl", "RM Content in sales", op_exp / net_sales,
                 f"OpEx({op_exp}) / Sales({net_sales})")
        # Domestic receivables days
        if dom_recv is not None:
            days = dom_recv / net_sales * 365
            _set("working_capital", "Domestic receivables - Days Gross Domestic Sales",
                 round(days, 1), f"({dom_recv}/{net_sales})*365")
        # Total Receivables / Gross Sales
        total_recv = _get("working_capital", "Total Receivables") or dom_recv
        if total_recv is not None:
            _set("working_capital", "Total Receivables/Gross Sales",
                 round(total_recv / net_sales * 365, 1),
                 f"({total_recv}/{net_sales})*365 days")

    # Net Worth
    if share_cap is not None and reserves is not None:
        _set("net_worth", "Net Worth", share_cap + reserves,
             f"ShareCap({share_cap}) + Reserves({reserves})")
    nw = _get("net_worth", "Net Worth") or net_worth

    # Tangible Net Worth = Net Worth - Intangibles
    intang = intangibles or 0
    if nw is not None:
        _set("ratios", "Tangible Net Worth", nw - intang,
             f"NetWorth({nw}) - Intangibles({intang})")
    tnw = _get("ratios", "Tangible Net Worth") or nw

    # Net Working Capital = CA - CL
    if curr_assets is not None and curr_liab is not None:
        nwc = curr_assets - curr_liab
        _set("ratios", "Net Working Capital", nwc,
             f"CA({curr_assets}) - CL({curr_liab})")
    nwc = _get("ratios", "Net Working Capital")

    # Current Ratio
    if curr_assets is not None and curr_liab is not None and curr_liab != 0:
        _set("ratios", "Current Ratio", round(curr_assets / curr_liab, 2),
             f"CA({curr_assets}) / CL({curr_liab})")

    # Debt/Equity
    if term_liab is not None and tnw is not None and tnw != 0:
        _set("ratios", "Debt/Equity", round(term_liab / tnw, 2),
             f"TermLiab({term_liab}) / TNW({tnw})")

    # TOL/Equity
    if tol is not None and tnw is not None and tnw != 0:
        _set("ratios", "TOL/Equity", round(tol / tnw, 2),
             f"TOL({tol}) / TNW({tnw})")

    # ROCE = PBDIT / Total Assets
    if pbdit is not None and total_assets is not None and total_assets != 0:
        _set("ratios", "ROCE", round(pbdit / total_assets * 100, 2),
             f"PBDIT({pbdit}) / TotalAssets({total_assets}) * 100")

    # NWC % to Current Assets
    if nwc is not None and curr_assets is not None and curr_assets != 0:
        _set("working_capital", "NWC % to Current Assets",
             round(nwc / curr_assets * 100, 2),
             f"NWC({nwc}) / CA({curr_assets}) * 100")

    # Working Capital gap = CA - Trade Creditors - Other CL (non-bank)
    if curr_assets is not None and trade_pay is not None:
        other_cl = _get("current_liabilities", "Other current Liabilities") or 0
        wc_gap = curr_assets - trade_pay - other_cl
        _set("working_capital", "Working Capital gap", round(wc_gap, 2),
             f"CA({curr_assets}) - TradePay({trade_pay}) - OtherCL({other_cl})")

    # Break Even Level of Sales = Fixed Costs / (Contribution/Sales)
    sales      = _get("break_even", "Sales") or net_sales
    var_cost   = _get("break_even", "Total Variable Costs")
    fixed_cost = _get("break_even", "Fixed Costs")
    dep_be     = dep or 0

    if sales and var_cost and fixed_cost and sales != 0:
        contribution = sales - var_cost
        _set("break_even", "Contribution", contribution,
             f"Sales({sales}) - VarCosts({var_cost})")
        if contribution > 0:
            bep = fixed_cost / (contribution / sales)
            _set("break_even", "Break Even Level of Sales", round(bep, 2),
                 f"FixedCosts({fixed_cost}) / (Contribution({contribution})/Sales({sales}))")
            cash_bep = (fixed_cost - dep_be) / (contribution / sales)
            _set("break_even", "Cash Break Even of Sales", round(cash_bep, 2),
                 f"(FixedCosts({fixed_cost})-Dep({dep_be})) / PVRatio")

    # Depreciation adjustments = Depreciation
    if dep is not None:
        _set("pnl", "Depreciation adjustments", dep, f"Same as Depreciation({dep})")

    # Transfer to Reserves — set to 0 if not found (common)
    if sections.get("pnl", {}).get("fields", {}).get("Transfer to Reserves", {}).get("value") is None:
        _set("pnl", "Transfer to Reserves", 0, "Not specifically mentioned — defaulting to 0")

    return sections


# ── Public API ────────────────────────────────────────────────────────────────

def extract_cma_fields(
    pages:       list[dict],
    source_file: str = "",
    doc_id:      str = "",
) -> dict:
    """
    Extract all 199 CMA fields. Results cached per doc_id.
    Includes post-extraction computed fields.
    """
    cached = _load_ai_cache(doc_id)
    if cached:
        return cached

    context      = build_context(pages)
    sections_out = {}
    found_total  = 0
    total_fields = 0

    for section_key, meta in CMA_SECTIONS.items():
        logger.info(f"Extracting: {meta['label']} ({len(meta['fields'])} fields)")
        fields_result = _extract_section(section_key, meta, context)

        found = sum(1 for v in fields_result.values() if v.get("value") is not None)
        found_total  += found
        total_fields += len(fields_result)

        sections_out[section_key] = {
            "label":  meta["label"],
            "fields": fields_result,
        }
        logger.info(f"  → {found}/{len(fields_result)} found")

    # Post-process: compute derived fields
    logger.info("Computing derived fields...")
    sections_out = _compute_derived(sections_out)

    # Recount after computation
    found_after = sum(
        1 for s in sections_out.values()
        for v in s["fields"].values()
        if v.get("value") is not None
    )

    result = {
        "meta": {
            "source_file":      source_file,
            "doc_id":           doc_id,
            "total_fields":     total_fields,
            "fields_found":     found_after,
            "fields_not_found": total_fields - found_after,
            "coverage_pct":     round(found_after / total_fields * 100, 1) if total_fields else 0,
        },
        "sections": sections_out,
    }

    _save_ai_cache(doc_id, result)
    return result