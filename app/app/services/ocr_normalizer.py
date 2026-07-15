import re
import json
import logging
from html.parser import HTMLParser
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

from app.utils.fy_detector import detect_fy

logger = logging.getLogger(__name__)

# Step 1: Page classifier keywords
PAGE_TYPE_KEYWORDS = {
    "Balance Sheet": [
        "balance sheet", "equity & liabilities", "equity and liabilities",
        "shareholders fund", "shareholders' funds", "non-current assets",
        "current assets", "current liabilities", "total assets",
        "property plant and equipment",
    ],
    "Statement of Profit and Loss": [
        "statement of profit and loss", "profit and loss",
        "revenue from operations", "total expense", "profit before tax",
        "profit for the year", "earnings per equity share",
        "employee benefits expense", "depreciation and amortization",
    ],
    "Cash Flow Statement": [
        "cash flow", "cashflow", "cash generated from operations",
        "investing activities", "financing activities",
        "net cash flow", "profit before taxation",
    ],
    "Notes to Accounts": [
        "note 2", "note 3", "note 4", "note 5", "note 6", "note 7",
        "note 8", "note 9", "note 10", "note 11", "note 12",
        "note 16", "note 17", "note 19", "note 20", "note 21",
        "note 22", "note 23", "note 24", "note 25", "note 26",
        "accompanying notes", "notes to financial",
        "notes forming part",
    ],
    "Fixed Asset Schedule": [
        "gross block", "net block", "depreciation block",
        "tangible assets", "accumulated depreciation",
        "additions during the year", "deletions during the year",
    ],
    "Borrowing Schedule": [
        "long-term borrowings", "long term borrowings",
        "secured loans", "unsecured loans", "term loans from bank",
        "current maturities of non-current",
    ],
    "Receivable Schedule": [
        "trade receivables", "undisputed trade receivable",
        "disputed trade receivable", "ageing schedule",
        "outstanding for following periods",
    ],
    "Payable Schedule": [
        "trade payables", "dues of micro enterprise",
        "dues of creditors other than",
        "outstanding for following periods from due date of payment",
    ],
    "Tax Schedule": [
        "deferred tax", "current tax", "tax expense",
        "income tax", "contingent liabilities",
    ],
}

# Note patterns
NOTE_PATTERNS = [
    r"Note\s*(\d{1,3})\s*[-–—:.]",       # "Note 4 -Long- Term Borrowings"
    r"Note\s*(?:No\.?\s*)?(\d{1,3})",     # "Note No. 21"
    r"Schedule\s+([IVX]+|\d{1,3})",        # "Schedule IV"
    r"note\s+(\d{1,3})\s*(?:[-–]|$)",     # "note 7" in table headers
]

# Section header pattern definitions
SECTION_HEADERS = {
    r"shareholders['']?\s*funds?": "Shareholders' Funds",
    r"non[- ]current\s+(?:liabilities|assets)": "Non-Current {0}",
    r"current\s+(?:liabilities|assets)": "Current {0}",
    r"income": "Income",
    r"expenses?": "Expenses",
    r"operating\s+activities": "Operating Activities",
    r"investing\s+activities": "Investing Activities",
    r"financing\s+activities": "Financing Activities",
}

# Skip patterns for prose line matching
PROSE_SKIP_PATTERNS = [
    r"din\s*no\s*:",
    r"udin\s*:",
    r"cin\s*:",
    r"membership\s*no",
    r"registration\s*no",
    r"date\s*:",
    r"place\s*:",
    r"page\s*\d+",
    r"director",
    r"partner",
]

REJECT_PAGE_KEYWORDS = [
    "independent auditor", "auditor's report", "auditors' report", "auditors report",
    "caro report", "caro, 2016", "caro 2020", "directors' report", "directors report",
    "board's report", "board report", "corporate governance", "management discussion",
    "notice of annual general", "notice of agm", "proxy form", "attendance slip",
    "compliance certificate", "secretarial audit", "independent practitioner's report",
    "independent accountant's review report", "annexure a", "annexure b", "companies (auditor's report)",
    "companies (auditors report)", "annexure to"
]

def is_page_rejected(text: str) -> bool:
    """Check if the page is a report, compliance, or non-financial statement narrative page to reject."""
    t = text.lower()
    has_reject_keyword = any(kw in t for kw in REJECT_PAGE_KEYWORDS)
    if not has_reject_keyword:
        return False
        
    has_financial_statement = any(
        any(kw in t for kw in PAGE_TYPE_KEYWORDS[pt])
        for pt in ["Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement"]
    )
    return not has_financial_statement

def should_reject_candidate(label: str, context_path: str, evidence_text: str, source_statement: str) -> bool:
    """Apply strict rejection rules 7 to 10 on the candidate row."""
    if source_statement not in ("Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement", "Notes to Accounts"):
        return True

    label_clean = label.strip()
    evidence_clean = evidence_text.lower()
    context_clean = context_path.lower()
    
    # Rule 7: Reject numeric-only labels
    if not any(c.isalpha() for c in label_clean):
        return True
        
    # Rule 8: Reject page headers and footers
    header_footer_patterns = [
        r"^page\s*\d+", r"^\d+\s*of\s*\d+$", r"^confidential$",
        r"^annual\s*report\s*\d{4}", r"^statements? forming part of",
        r"signature\s*to\s*note", r"referred\s*to\s*in\s*our\s*report"
    ]
    for pat in header_footer_patterns:
        if re.search(pat, label_clean.lower()) or re.search(pat, evidence_clean):
            return True
            
    # Rule 9: Reject auditor reports, CARO reports, annexures, certifications, signatures, legal/compliance
    reject_keywords = [
        "auditor", "caro", "annexure", "certification", "chartered accountant",
        "signature", "declaration", "compliance certificate", "memorandum",
        "articles of association", "directors' report", "directors report",
        "board's report", "board report", "signatories", "signed in terms of our report"
    ]
    if any(kw in label_clean.lower() or kw in context_clean or kw in evidence_clean for kw in reject_keywords):
        return True
        
    # Rule 10: Reject address/property disclosure rows unless they contain values that participate in main statements
    address_keywords = [
        "plot no", "survey no", "khasra", "village", "taluka", "district", "state", "pin code",
        "registered office", "corporate office", "office at", "situated at"
    ]
    is_main_statement = source_statement in ("Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement", "Notes to Accounts")
    if any(kw in label_clean.lower() or kw in evidence_clean for kw in address_keywords):
        if not is_main_statement:
            return True
            
    return False

def is_total_row(label: str) -> bool:
    """Check if the label contains total/subtotal patterns."""
    return bool(re.search(r'\b(total|sub\s*total|grand\s*total)\b', label, re.I))

@dataclass
class TableYearMapping:
    current_year: int
    previous_year: int
    current_col: int
    previous_col: int
    label_col: int
    note_col: int = -1

@dataclass
class SectionState:
    current_statement: str = ""
    section_stack: list[str] = field(default_factory=list)
    active_note: str = ""

class StructuredTableParser(HTMLParser):
    """Parse HTML table into 2D List[List[str]] expanding rowspan and colspan, and track inheritance."""
    def __init__(self):
        super().__init__()
        self.raw_rows = []
        self.current_row = []
        self.current_cell = []
        self.current_attrs = {}
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_cell = True
            self.current_cell = []
            self.current_attrs = dict(attrs)
        elif tag == "tr":
            self.current_row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.in_cell = False
            cell_text = "".join(self.current_cell).strip().replace("\n", " ")
            rowspan = int(self.current_attrs.get("rowspan", 1))
            colspan = int(self.current_attrs.get("colspan", 1))
            self.current_row.append((cell_text, rowspan, colspan))
        elif tag == "tr":
            if self.current_row:
                self.raw_rows.append(self.current_row)

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

    def get_grid_and_inherited(self) -> tuple[list[list[str]], list[list[bool]]]:
        grid = []
        inherited = []
        for r_idx, row in enumerate(self.raw_rows):
            while len(grid) <= r_idx:
                grid.append([])
                inherited.append([])
            c_idx = 0
            for cell_text, rowspan, colspan in row:
                while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                    c_idx += 1
                for dr in range(rowspan):
                    nr = r_idx + dr
                    while len(grid) <= nr:
                        grid.append([])
                        inherited.append([])
                    for dc in range(colspan):
                        nc = c_idx + dc
                        while len(grid[nr]) <= nc:
                            grid[nr].append(None)
                            inherited[nr].append(None)
                        grid[nr][nc] = cell_text
                        inherited[nr][nc] = (dr > 0)
                c_idx += colspan
        
        final_grid = []
        final_inherited = []
        for r, inh in zip(grid, inherited):
            final_grid.append([("" if val is None else val) for val in r])
            final_inherited.append([False if val is None else val for val in inh])
        return final_grid, final_inherited


def classify_page(text: str) -> list[str]:
    """Return list of matching page types, ordered by score (highest first)."""
    t = text.lower()
    scores = {}
    for page_type, keywords in PAGE_TYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in t:
                score += 1
        if score > 0:
            # Enforce thresholding to avoid false positives on narrative pages
            if page_type in ("Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement"):
                has_exact_name = (page_type.lower() in t) or (page_type == "Statement of Profit and Loss" and "profit & loss" in t)
                if has_exact_name or score >= 2:
                    scores[page_type] = score
            elif page_type == "Notes to Accounts":
                has_exact_phrase = any(phrase in t for phrase in ("accompanying notes", "notes to financial", "notes forming part"))
                if has_exact_phrase or score >= 2:
                    scores[page_type] = score
            else:
                scores[page_type] = score
            
    if "auditor's report" in t or "auditors report" in t or "directors' report" in t or "directors report" in t or "auditor’s report" in t or "directors’ report" in t:
        return ["Other"]
        
    sorted_types = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    return sorted_types if sorted_types else ["Other"]


def parse_fy_to_year(fy_str: str) -> Optional[int]:
    """Helper to convert FY format like '2024-25' or '2025' to integer year (2025)."""
    if not fy_str or fy_str == "unknown":
        return None
    m = re.search(r"(\d{4})-(\d{2,4})", fy_str)
    if m:
        start_yr = int(m.group(1))
        end_part = m.group(2)
        if len(end_part) == 4:
            return int(end_part)
        elif len(end_part) == 2:
            return (start_yr // 100) * 100 + int(end_part)
    m = re.search(r"\b(20\d{2})\b", fy_str)
    if m:
        return int(m.group(1))
    return None


def detect_table_column_mapping(grid: list[list[str]], doc_current: Optional[int], doc_previous: Optional[int]) -> TableYearMapping:
    """Detect which columns contain current year, previous year, and row labels."""
    label_col = 0
    current_col = 1
    previous_col = 2
    note_col = -1
    
    curr_indices = []
    prev_indices = []
    particulars_indices = []
    note_indices = []
    
    for r_idx in range(min(3, len(grid))):
        row = grid[r_idx]
        for c_idx, cell in enumerate(row):
            cell_lower = cell.lower()
            if doc_current:
                curr_pattern = rf"\b({doc_current}|{str(doc_current)[2:]})\b"
                if re.search(curr_pattern, cell_lower) or (f"31st march" in cell_lower and f"{str(doc_current)}" in cell_lower):
                    curr_indices.append(c_idx)
            if doc_previous:
                prev_pattern = rf"\b({doc_previous}|{str(doc_previous)[2:]})\b"
                if re.search(prev_pattern, cell_lower) or (f"31st march" in cell_lower and f"{str(doc_previous)}" in cell_lower):
                    prev_indices.append(c_idx)
            
            if not doc_current or not doc_previous:
                m_year = re.findall(r"\b(20\d{2})\b", cell_lower)
                for y in m_year:
                    y_val = int(y)
                    if doc_current and y_val == doc_current:
                        curr_indices.append(c_idx)
                    elif doc_previous and y_val == doc_previous:
                        prev_indices.append(c_idx)
                    else:
                        if not curr_indices:
                            curr_indices.append(c_idx)
                        elif c_idx not in curr_indices:
                            prev_indices.append(c_idx)
            
            if any(k in cell_lower for k in ("particular", "asset", "liabilit", "equity", "description")):
                particulars_indices.append(c_idx)
            if "note" in cell_lower:
                note_indices.append(c_idx)
                
    if curr_indices:
        from collections import Counter
        current_col = Counter(curr_indices).most_common(1)[0][0]
    if prev_indices:
        from collections import Counter
        previous_col = Counter(prev_indices).most_common(1)[0][0]
    if particulars_indices:
        from collections import Counter
        label_col = Counter(particulars_indices).most_common(1)[0][0]
    else:
        best_col = 0
        max_alpha = -1
        for col_idx in range(len(grid[0]) if grid else 0):
            if col_idx == current_col or col_idx == previous_col:
                continue
            alpha_count = sum(sum(c.isalpha() for c in r[col_idx]) for r in grid[3:] if col_idx < len(r))
            if alpha_count > max_alpha:
                max_alpha = alpha_count
                best_col = col_idx
        label_col = best_col
        
    if note_indices:
        from collections import Counter
        note_col = Counter(note_indices).most_common(1)[0][0]
        
    num_cols = len(grid[0]) if grid else 0
    if current_col >= num_cols:
        current_col = 1 if num_cols > 1 else 0
    if previous_col >= num_cols:
        previous_col = 2 if num_cols > 2 else -1
        
    return TableYearMapping(
        current_year=doc_current or 0,
        previous_year=doc_previous or 0,
        current_col=current_col,
        previous_col=previous_col if previous_col < num_cols else -1,
        label_col=label_col,
        note_col=note_col
    )


def split_jammed_label_cell(text: str) -> list[str]:
    """Split a jammed label cell containing multiple financial terms into clean sub-labels."""
    known_labels = [
        "Share Capital",
        "Reserves and Surplus",
        "Reserves & Surplus",
        "Long-Term Borrowings",
        "Long Term Borrowings",
        "Other Long term Liabilities",
        "Other Long-Term Liabilities",
        "Long Term Provisions",
        "Long-Term Provisions",
        "Short-Term Borrowings",
        "Short Term Borrowings",
        "Trade Payables",
        "Other Current Liabilities",
        "Short-Term Provisions",
        "Short Term Provisions",
        "Property Plant and Equipment",
        "Property, Plant and Equipment",
        "Intangible Assets",
        "Capital work-in-progress",
        "Capital work in progress",
        "Intangible Assets under development",
        "Non-Current Investments",
        "Deferred Tax Asset",
        "Deferred Tax Assets",
        "Long-Term Loans and Advances",
        "Long -Term Loans and Advances",
        "Other Non Current Assets",
        "Other Non-Current Assets",
        "Trade Receivables",
        "Cash and Cash Equivalents",
        "Bank balance other than cash and cash equivalent",
        "Bank balances other than cash",
        "Short-Term Loans and Advances",
        "Short - Term Loans and Advances",
        "Other Current Assets",
        "Revenue from Operations",
        "Other Income",
        "Total Income",
        "Cost of Materials Consumed",
        "Purchases of Stock-in-Trade",
        "Changes in inventories",
        "Employee Benefits Expense",
        "Finance Costs",
        "Depreciation and Amortization",
        "Depreciation and Amortisation",
        "Other Expense",
        "Other Expenses",
        "Total Expense",
        "Profit before tax",
        "Profit after tax",
        "Profit for the year",
        "Earnings per equity share",
        "TOTAL"
    ]
    
    matches = []
    text_lower = text.lower()
    for label in known_labels:
        start = 0
        while True:
            pos = text_lower.find(label.lower(), start)
            if pos == -1:
                break
            matches.append((pos, pos + len(label), label))
            start = pos + 1
            
    matches.sort(key=lambda x: x[0])
    
    filtered_matches = []
    for m in matches:
        overlap = False
        for f in filtered_matches:
            if not (m[1] <= f[0] or m[0] >= f[1]):
                if (m[1] - m[0]) > (f[1] - f[0]):
                    filtered_matches.remove(f)
                else:
                    overlap = True
                break
        if not overlap:
            filtered_matches.append(m)
            
    filtered_matches.sort(key=lambda x: x[0])
    labels = [m[2] for m in filtered_matches]
    
    if not labels:
        return [text.strip()]
    return labels


def extract_numbers_from_cell(text: str) -> list[str]:
    """Parse out individual numeric values from a cell string."""
    cleaned = text.strip()
    if not cleaned or cleaned in ("=", "·", "二", "。", "nil", "n.a.", "na"):
        return []
    parts = cleaned.split()
    results = []
    number_pattern = r'^[\s\(\-–—]?\d[\d,.]*\)?%?$'
    for part in parts:
        cleaned_part = part.strip().rstrip(',.;:')
        if re.match(number_pattern, cleaned_part) or cleaned_part in ("-", "—", "–"):
            results.append(cleaned_part)
    return results


def detect_notes_on_page(text: str) -> list[dict]:
    """Find note references and their positions on the page."""
    notes = []
    text_clean = re.sub(r'<[^>]+>', ' ', text)
    for pat in NOTE_PATTERNS:
        for m in re.finditer(pat, text_clean, re.IGNORECASE):
            note_num = m.group(1)
            start = max(0, m.start() - 30)
            end = min(len(text_clean), m.end() + 30)
            context = text_clean[start:end].strip()
            notes.append({
                "note_number": note_num,
                "position": m.start(),
                "context": context
            })
    notes.sort(key=lambda x: x["position"])
    return notes


def get_active_note_for_position(notes: list[dict], pos: int) -> str:
    active_note = ""
    for note in notes:
        if note["position"] <= pos:
            active_note = note["note_number"]
        else:
            break
    return active_note


def update_section_stack(text_line: str, section_stack: list[str]) -> list[str]:
    line_lower = text_line.lower().strip()
    for pattern, section_name in SECTION_HEADERS.items():
        if re.search(pattern, line_lower):
            if "{0}" in section_name:
                if "asset" in line_lower:
                    resolved_name = section_name.format("Assets")
                else:
                    resolved_name = section_name.format("Liabilities")
            else:
                resolved_name = section_name
            
            if "funds" in resolved_name.lower() or "liabilities" in resolved_name.lower() or "assets" in resolved_name.lower() or "income" in resolved_name.lower() or "expenses" in resolved_name.lower() or "activities" in resolved_name.lower():
                return [resolved_name]
            else:
                if resolved_name not in section_stack:
                    return section_stack + [resolved_name]
    return section_stack


def build_context_path(state: SectionState, label: str) -> str:
    parts = []
    if state.current_statement:
        parts.append(state.current_statement)
    parts.extend(state.section_stack)
    if label and label not in parts:
        parts.append(label)
    return " > ".join(p for p in parts if p)


def get_statement_type_from_context(page_types: list[str], text: str) -> str:
    """Determine the source statement type (Balance Sheet, P&L, etc.)."""
    for pt in page_types:
        if pt in ("Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement"):
            return pt
            
    text_lower = text.lower()
    if any(k in text_lower for k in ("profit and loss", "revenue from operations", "total expense", "depreciation")):
        return "Statement of Profit and Loss"
    if any(k in text_lower for k in ("balance sheet", "equity & liabilities", "share capital", "borrowings")):
        return "Balance Sheet"
    if any(k in text_lower for k in ("cash flow", "operating activities", "investing activities")):
        return "Cash Flow Statement"
        
    return "Balance Sheet"


def find_html_tables(text: str) -> list[str]:
    return re.findall(r'<table[^>]*>.*?</table>', text, re.IGNORECASE | re.DOTALL)


def is_header_row(row: list[str]) -> bool:
    """Check if the row represents a table header row."""
    header_keywords = ["particular", "note no", "asat 31st", "as at 31st", "year ended", "ended 31st"]
    for cell in row:
        cell_lower = cell.lower()
        if any(kw in cell_lower for kw in header_keywords):
            return True
    return False


def render_md_table_to_html(md_lines: list[str]) -> str:
    html_rows = []
    for idx, line in enumerate(md_lines):
        # Skip the separator line (e.g. |---|---|)
        if idx == 1 and all(c in " |-:" for c in line.strip()):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        tag = "th" if idx == 0 else "td"
        html_cells = "".join(f"<{tag}>{cell}</{tag}>" for cell in cells)
        html_rows.append(f"<tr>{html_cells}</tr>")
    return "<table>" + "".join(html_rows) + "</table>"


def convert_markdown_tables_to_html(text: str) -> str:
    # Split text into lines
    lines = text.split("\n")
    output_lines = []
    in_table = False
    table_lines = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                in_table = True
                table_lines = [stripped]
            else:
                table_lines.append(stripped)
        else:
            if in_table:
                # We finished a table block. Render it to HTML.
                html_table = render_md_table_to_html(table_lines)
                output_lines.append(html_table)
                in_table = False
                table_lines = []
            output_lines.append(line)
            
    if in_table:
        html_table = render_md_table_to_html(table_lines)
        output_lines.append(html_table)
        
    return "\n".join(output_lines)


def normalize_ocr_output(pages: list[dict]) -> dict:
    """
    Normalizes OCR extraction output into a structured candidate dataset.
    Exposes candidate row lists to downstream extraction services.
    """
    fy_current, fy_previous = detect_fy(pages)
    doc_current = parse_fy_to_year(fy_current)
    doc_previous = parse_fy_to_year(fy_previous)
    
    candidates = []
    section_state = SectionState()
    
    pages_rejected_count = 0
    
    for page_data in pages:
        page_num = page_data["page"]
        page_text = page_data["text"]
        
        # Pre-process markdown tables to HTML table strings
        processed_text = convert_markdown_tables_to_html(page_text)
        
        if is_page_rejected(processed_text):
            pages_rejected_count += 1
            logger.info(f"Page {page_num} rejected: contains auditor/report/legal keywords and no financial statement structure.")
            continue
        
        page_types = classify_page(processed_text)
        primary_type = page_types[0] if page_types else "Other"
        
        if primary_type in ("Balance Sheet", "Statement of Profit and Loss", "Cash Flow Statement"):
            section_state.current_statement = primary_type
            section_state.section_stack = []
        elif primary_type == "Notes to Accounts":
            section_state.current_statement = get_statement_type_from_context(page_types, processed_text)
            section_state.section_stack = []
        else:
            section_state.current_statement = "Other"
            section_state.section_stack = []
            
        notes_on_page = detect_notes_on_page(processed_text)
        tables = find_html_tables(processed_text)
        
        table_start_positions = []
        start_search = 0
        for table_html in tables:
            pos = processed_text.find(table_html, start_search)
            table_start_positions.append(pos if pos != -1 else start_search)
            start_search = pos + len(table_html) if pos != -1 else start_search + 1
            
        for t_idx, table_html in enumerate(tables):
            table_pos = table_start_positions[t_idx]
            active_note = get_active_note_for_position(notes_on_page, table_pos)
            
            parser = StructuredTableParser()
            try:
                parser.feed(table_html)
                grid, inherited = parser.get_grid_and_inherited()
            except Exception as e:
                logger.warning(f"Error parsing HTML table on page {page_num}: {e}")
                continue
                
            if not grid:
                continue
                
            year_mapping = detect_table_column_mapping(grid, doc_current, doc_previous)
            
            # Check for jammed label cell in first few columns/rows
            jammed_labels = []
            jammed_row_idx = -1
            for r in range(min(3, len(grid))):
                for c in range(min(3, len(grid[r]))):
                    cell_text = grid[r][c]
                    split_lbls = split_jammed_label_cell(cell_text)
                    if len(split_lbls) > 5:
                        jammed_labels = split_lbls
                        jammed_row_idx = r
                        break
                if jammed_labels:
                    break
            
            label_pointer = 0
            
            for r_idx, row in enumerate(grid):
                # Skip header rows
                if is_header_row(row):
                    continue
                
                # Extract uninherited value cells in this row
                curr_vals = []
                prev_vals = []
                uninherited_val_cells = []
                
                for c_idx, cell in enumerate(row):
                    if not inherited[r_idx][c_idx]:
                        # Skip note column cell and label column cell
                        if c_idx == year_mapping.note_col:
                            continue
                        if c_idx == year_mapping.label_col and not jammed_labels:
                            continue
                        
                        vals = extract_numbers_from_cell(cell)
                        is_note = re.match(r'^(?:Note\s*(?:No\.?)?\s*)?\d{1,3}$', cell.strip(), re.I)
                        if is_note:
                            continue
                        if vals:
                            uninherited_val_cells.append((c_idx, vals))
                
                if jammed_labels:
                    uninherited_val_cells.sort(key=lambda x: x[0])
                    if len(uninherited_val_cells) >= 2:
                        first_vals = uninherited_val_cells[0][1]
                        second_vals = uninherited_val_cells[1][1]
                        if year_mapping.current_col < year_mapping.previous_col or year_mapping.previous_col == -1:
                            curr_vals.extend(first_vals)
                            prev_vals.extend(second_vals)
                        else:
                            prev_vals.extend(first_vals)
                            curr_vals.extend(second_vals)
                    elif len(uninherited_val_cells) == 1:
                        c_idx, vals = uninherited_val_cells[0]
                        if c_idx == year_mapping.previous_col:
                            prev_vals.extend(vals)
                        else:
                            curr_vals.extend(vals)
                else:
                    # Map value cells to current/previous for normal table
                    for c_idx, vals in uninherited_val_cells:
                        if c_idx == year_mapping.current_col:
                            curr_vals.extend(vals)
                        elif c_idx == year_mapping.previous_col:
                            prev_vals.extend(vals)
                        else:
                            # Fallback by column order index
                            if year_mapping.current_col < year_mapping.previous_col:
                                if not curr_vals:
                                    curr_vals.extend(vals)
                                elif not prev_vals:
                                    prev_vals.extend(vals)
                            else:
                                if not prev_vals:
                                    prev_vals.extend(vals)
                                elif not curr_vals:
                                    curr_vals.extend(vals)
                
                if jammed_labels:
                    if r_idx > jammed_row_idx:
                        n_vals = max(len(curr_vals), len(prev_vals))
                        if n_vals > 0:
                            for i in range(n_vals):
                                lbl_idx = label_pointer + i
                                if lbl_idx < len(jammed_labels):
                                    label = jammed_labels[lbl_idx]
                                    curr_val = curr_vals[i] if i < len(curr_vals) else None
                                    prev_val = prev_vals[i] if i < len(prev_vals) else None
                                    
                                    context_path = build_context_path(section_state, label)
                                    if not should_reject_candidate(label, context_path, " | ".join(row), section_state.current_statement):
                                        candidates.append({
                                            "label": label,
                                            "context_path": context_path,
                                            "source_statement": section_state.current_statement,
                                            "note_number": active_note,
                                            "page_number": page_num,
                                            "current_year_value": curr_val,
                                            "previous_year_value": prev_val,
                                            "evidence_text": " | ".join(row)
                                        })
                            label_pointer += n_vals
                else:
                    # Standard row-by-row extraction
                    lbl_idx = year_mapping.label_col
                    label_cell = row[lbl_idx] if lbl_idx < len(row) else ""
                    
                    labels = split_jammed_label_cell(label_cell)
                    if not any(labels):
                        continue
                        
                    for i, label in enumerate(labels):
                        label_clean = label.strip()
                        if not label_clean or len(label_clean) < 2:
                            continue
                            
                        curr_val = curr_vals[i] if i < len(curr_vals) else None
                        prev_val = prev_vals[i] if i < len(prev_vals) else None
                        
                        context_path = build_context_path(section_state, label_clean)
                        if not should_reject_candidate(label_clean, context_path, " | ".join(row), section_state.current_statement):
                            candidates.append({
                                "label": label_clean,
                                "context_path": context_path,
                                "source_statement": section_state.current_statement,
                                "note_number": active_note,
                                "page_number": page_num,
                                "current_year_value": curr_val,
                                "previous_year_value": prev_val,
                                "evidence_text": " | ".join(row)
                            })
                        
        # Prose line-based extraction fallback
        lines = processed_text.split("\n")
        in_table = False
        for line in lines:
            if "<table" in line:
                in_table = True
            if "</table>" in line:
                in_table = False
                continue
            if in_table:
                continue
                
            line_clean = re.sub(r'<[^>]+>', '', line).strip()
            if not line_clean or len(line_clean) < 8:
                continue
                
            skip_line = False
            for pat in PROSE_SKIP_PATTERNS:
                if re.search(pat, line_clean.lower()):
                    skip_line = True
                    break
            if skip_line:
                continue
                
            section_state.section_stack = update_section_stack(line_clean, section_state.section_stack)
            
            m = re.match(
                r'^([A-Za-z][\w\s&\(\)\'"/,.-]{3,}?)\s+([\d,]+\.?\d*|\(\d[\d,]*\.?\d*\)|-)\s+([\d,]+\.?\d*|\(\d[\d,]*\.?\d*\)|-)\s*$',
                line_clean
            )
            if m:
                label = m.group(1).strip()
                val1 = m.group(2).strip()
                val2 = m.group(3).strip()
                
                active_note = ""
                if notes_on_page:
                    active_note = notes_on_page[-1]["note_number"]
                    
                context_path = build_context_path(section_state, label)
                
                if not should_reject_candidate(label, context_path, line_clean, section_state.current_statement):
                    candidates.append({
                        "label": label,
                        "context_path": context_path,
                        "source_statement": section_state.current_statement,
                        "note_number": active_note,
                        "page_number": page_num,
                        "current_year_value": val1 if val1 != "-" else None,
                        "previous_year_value": val2 if val2 != "-" else None,
                        "evidence_text": line_clean
                    })
            else:
                m2 = re.match(
                    r'^([A-Za-z][\w\s&\(\)\'"/,.-]{3,}?)\s+([\d,]+\.?\d*|\(\d[\d,]*\.?\d*\)|-)\s*$',
                    line_clean
                )
                if m2:
                    label = m2.group(1).strip()
                    val1 = m2.group(2).strip()
                    
                    active_note = ""
                    if notes_on_page:
                        active_note = notes_on_page[-1]["note_number"]
                        
                    context_path = build_context_path(section_state, label)
                    
                    if not should_reject_candidate(label, context_path, line_clean, section_state.current_statement):
                        candidates.append({
                            "label": label,
                            "context_path": context_path,
                            "source_statement": section_state.current_statement,
                            "note_number": active_note,
                            "page_number": page_num,
                            "current_year_value": val1 if val1 != "-" else None,
                            "previous_year_value": None,
                            "evidence_text": line_clean
                        })

    return {
        "document_metadata": {
            "current_year": fy_current,
            "previous_year": fy_previous,
            "pages_processed": len(pages),
            "pages_rejected": pages_rejected_count
        },
        "candidates": candidates
    }
