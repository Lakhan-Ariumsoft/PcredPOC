"""
Complete CMA field definitions organized by section.
Each section is sent as one OpenAI API call (batching reduces 150+ calls → 15 calls).
"""

# Ordered sections with their fields
CMA_SECTIONS = {
    "operating_details": {
        "label": "Operating Details",
        "fields": ["Operating months"],
    },
    "sales": {
        "label": "Sales",
        "fields": [
            "Domestic Sale", "Export Sale", "Trade Discount",
            "Net Sales", "Growth in sales",
        ],
    },
    "cost_of_sales": {
        "label": "Cost of Sales",
        "fields": [
            "Purchases", "Services", "Stores & Spares (Imported)",
            "Stores & Spares (Indigenous)", "Repair & Maintenance",
            "Fuel Expenses", "Godown Rent", "Other Manufacturing Expenses",
            "Depreciation", "Other expenses", "Loading & unloading charges",
            "Packing and Forwarding expenses", "Transport Expenses",
            "Opening Stock in Process", "Closing Stock in Process",
            "Cost of Production", "Opening Stock of Finished Goods",
            "Closing Stock Of Finished Goods", "Total Cost of Sales",
        ],
    },
    "profitability": {
        "label": "Profitability",
        "fields": [
            "Gross profit", "Gross Profit/Sales", "Selling Expenses",
            "Administrative Expenses", "Operating Profit before interest",
        ],
    },
    "interest": {
        "label": "Interest",
        "fields": [
            "Interest on CC", "Interest on TL", "Other interests",
            "Total Interest", "Operating Profit after Interest",
        ],
    },
    "other_income_expenses": {
        "label": "Other Income / Expenses",
        "fields": [
            "Interest/Dividend/Royalties", "Creditors written back",
            "Other Income", "Other", "Other Expenses",
            "Intangibles written off",
            "Net of other non operating Income/Expenses",
        ],
    },
    "pnl": {
        "label": "Profit & Loss",
        "fields": [
            "Profit before Tax", "Provision for Taxes",
            "Net Profit/Loss (PAT)", "Cash Accruals", "Dividend paid",
            "Retained Profit", "Retained Cash Profits",
            "RM Content in sales", "PBDIT", "PBDIT/Sales",
            "Operating Profits/Sales", "PBT/Sales", "PAT/Sales",
            "Cash Accruals/Sales", "Transfer to Reserves",
            "Depreciation adjustments",
        ],
    },
    "current_liabilities": {
        "label": "Current Liabilities",
        "fields": [
            "Short Term loans from Applicant Bank",
            "Short Term loans From Other banks",
            "Short Term Borrowings from Others",
            "Sundry Creditors (Trade)", "Advance Payment from Customers",
            "Net Provision for Taxation", "Dividend Payable",
            "Other Statutory Liabilities", "Overdue Term Liabilities",
            "Installments of term Loan",
            "Other Current Liabilities & Provisions",
            "Creditors for Capital expenses", "Other current Liabilities",
            "Provision for Others",
            "Other debt due within one year-Unsecured Loans",
            "Total Current Liabilities",
        ],
    },
    "term_liabilities": {
        "label": "Term Liabilities",
        "fields": [
            "Debentures", "Preference Shares", "Term Loan from Bank",
            "Term Loan from Other Banks/Institutions",
            "Deferred Payments Credits", "Term deposits",
            "Other term Liabilities", "Long Term provisions",
            "Unsecured Loans", "Other debts - Unsecured Loans",
            "Total Term Liabilities", "Total Outside Liabilities",
        ],
    },
    "net_worth": {
        "label": "Net Worth",
        "fields": [
            "Capital", "General Reserve", "Revaluation Reserve",
            "Adjustments for previous Year costs", "Other reserves",
            "Unsecured loan as Quasi Capital",
            "Share application money pending allotment",
            "Surplus or deficit in Profit & Loss account",
            "Net Worth", "Total Liabilities",
        ],
    },
    "current_assets": {
        "label": "Current Assets",
        "fields": [
            "Cash & Bank Balances", "Government securities",
            "Fixed Deposits with Banks", "Domestic Receivables",
            "Export Receivables", "Deferred receivables",
            "Imported Raw Material", "Indigenous Rawmaterial",
            "Stock in Process", "Finished Goods",
            "Imported Consumables", "Indigenous consumables",
            "Packing Material", "Advances to Suppliers",
            "Net Advance Payment of Taxes", "Other Current Assets",
            "Current Investments & Loans and Advances",
            "Rent Receivable", "Other Investments",
            "Investments in Group Companies", "Total Current Assets",
        ],
    },
    "fixed_assets": {
        "label": "Fixed / Non-Current Assets",
        "fields": [
            "Gross Block", "Capital expenditure in work-in-process",
            "Depreciation to Date", "Net Block",
            "Investments in Subsidiaries", "Investment in Others",
            "Advance to suppliers of Capital goods & Contractors",
            "Deferred Receivables", "Other Non-current investments",
            "Fixed Deposits", "Long outstanding dues",
            "Deferred Tax Asset", "Deposits for Godown & Office",
            "Total Other Non Current Assets",
        ],
    },
    "intangibles": {
        "label": "Intangibles",
        "fields": [
            "Intangible Assets", "Preliminary Expenses",
            "Deferred Revenue expenditures", "Other Intangibles",
            "Total Intangible Assets", "Total Assets",
        ],
    },
    "ratios": {
        "label": "Ratios / Analysis",
        "fields": [
            "Tangible Net Worth", "Net Working Capital", "Opening TNW",
            "Plough back of profit", "Increase in capital/reserves",
            "Closing TNW", "Current Ratio", "Debt/Equity", "TOL/Equity",
            "Current Assets/Tangible Assets", "ROCE",
            "Inventory+Receivables as days of Net Sales",
        ],
    },
    "additional_info": {
        "label": "Additional Info",
        "fields": [
            "Arrears of Depreciation", "Contingent Liabilities",
            "Arrears of Cumulative Dividends", "Gratuity Liability",
            "Disputed Tax Liabilities",
            "Other Liabilities not provided for",
        ],
    },
    "working_capital": {
        "label": "Working Capital Assessment",
        "fields": [
            "Stock of Imported RM - Days Consumption",
            "Stock of Indigenous RM - Days Consumption",
            "Imported Consumables - Days Consumption",
            "Indigenous Consumables - Days Consumption",
            "Stock in process - Days of Cost of Production",
            "Finished Goods - Days Cost of Sales",
            "Total Inventory", "Total Inventory/Sales",
            "Domestic receivables - Days Gross Domestic Sales",
            "Export Receivables - Days Exports",
            "Total Receivables", "Total Receivables/Gross Sales",
            "Creditors - Days Consumption", "Bank Finance",
            "Working Capital gap", "NWC % to Current Assets",
        ],
    },
    "fund_flow": {
        "label": "Fund Flow",
        "fields": [
            "Increase in Term Liability", "Decrease in Fixed Assets",
            "Decrease in Other non current assets", "Net Loss",
            "Increase in Intangibles", "Decrease in Capital and Reserves",
            "Decrease in Term Liabilities", "Increase in Fixed Assets",
            "Increase in non-Current Assets", "Increase in Bank Borrowings",
            "Increase in other Current Liabilities",
            "Decrease in Inventory", "Decrease in Receivables",
            "Decrease in Cash/Deposits", "Increase in Inventory",
            "Increase in Receivables", "Increase in Cash/Deposits",
            "Decrease in Other Current Liabilities",
            "Decrease in Bank Borrowings",
        ],
    },
    "break_even": {
        "label": "Break Even",
        "fields": [
            "Sales", "Variable Cost", "Direct Labour", "Power & Fuel",
            "Total Variable Costs", "Fixed Costs",
            "Break Even Level of Sales", "Cash Break Even of Sales",
            "Contribution",
        ],
    },
}

# Flat list for reference
CMA_FIELDS_FLAT = [
    field
    for section in CMA_SECTIONS.values()
    for field in section["fields"]
]

# Financial year detection patterns
FY_PATTERNS = [
    r"march\s+31[,\s]+(\d{4})",
    r"31\s*(?:st|nd|rd|th)?\s+march[,\s]+(\d{4})",
    r"year\s+ended\s+march[,\s]+(\d{4})",
    r"f\.?y\.?\s*(\d{4})[–\-](\d{2,4})",
    r"(\d{4})[–\-](\d{2})\b",
]