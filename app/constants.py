"""
CMA field definitions, section groupings, and all known text aliases.
Add new aliases here when new PDF wordings are discovered.
"""

CMA_FIELDS = {

    # ── Shareholders' Funds ─────────────────────────────────────────────────
    "share_capital": {
        "label": "Share Capital",
        "section": "shareholders_funds",
        "aliases": [
            "share capital",
            "equity share capital",
            "share cap",
        ],
    },
    "reserves_and_surplus": {
        "label": "Reserves & Surplus",
        "section": "shareholders_funds",
        "aliases": [
            "reserves and surplus",
            "reserves & surplus",
            "reserve and surplus",
            "reserve & surplus",
            "reserves surplus",
        ],
    },
    "money_received_against_share_warrants": {
        "label": "Money Received Against Share Warrants",
        "section": "shareholders_funds",
        "aliases": [
            "money received against share warrants",
            "money received share warrants",
            "share warrants money received",
        ],
    },
    "share_application_money_pending_allotment": {
        "label": "Share Application Money Pending Allotment",
        "section": "shareholders_funds",
        "aliases": [
            "share application money pending allotment",
            "share application money",
            "application money pending allotment",
        ],
    },

    # ── Non-Current Liabilities ─────────────────────────────────────────────
    "long_term_borrowings": {
        "label": "Long Term Borrowings",
        "section": "non_current_liabilities",
        "aliases": [
            "long term borrowings",
            "long - term borrowings",
            "long-term borrowings",
            "long term borrowing",
        ],
    },
    "deferred_tax_liabilities_net": {
        "label": "Deferred Tax Liabilities (Net)",
        "section": "non_current_liabilities",
        "aliases": [
            "deferred tax liabilities",
            "deferred tax liabilities (net)",
            "deferred tax liability",
            "deferred tax liabilities net",
        ],
    },
    "other_long_term_liabilities": {
        "label": "Other Long Term Liabilities",
        "section": "non_current_liabilities",
        "aliases": [
            "other long term liabilities",
            "other long-term liabilities",
            "other long term liability",
        ],
    },
    "long_term_provisions": {
        "label": "Long Term Provisions",
        "section": "non_current_liabilities",
        "aliases": [
            "long term provisions",
            "long - term provisions",
            "long-term provisions",
            "long term provision",
        ],
    },

    # ── Current Liabilities ─────────────────────────────────────────────────
    "short_term_borrowings": {
        "label": "Short Term Borrowings",
        "section": "current_liabilities",
        "aliases": [
            "short term borrowings",
            "short - term borrowings",
            "short-term borrowings",
            "short term borrowing",
        ],
    },
    "trade_payables": {
        "label": "Trade Payables",
        "section": "current_liabilities",
        "aliases": [
            "trade payables",
            "trade payable",
            "sundry creditors",
        ],
    },
    "other_current_liabilities": {
        "label": "Other Current Liabilities",
        "section": "current_liabilities",
        "aliases": [
            "other current liabilities",
            "other current liability",
        ],
    },
    "short_term_provisions": {
        "label": "Short Term Provisions",
        "section": "current_liabilities",
        "aliases": [
            "short term provisions",
            "short - term provisions",
            "short-term provisions",
            "short term provision",
        ],
    },
}

SECTION_LABELS = {
    "shareholders_funds":      "Shareholders' Funds",
    "non_current_liabilities": "Non-Current Liabilities",
    "current_liabilities":     "Current Liabilities",
}

# Keywords that identify a balance sheet page
BALANCE_SHEET_KEYWORDS = [
    "balance sheet",
    "equity and liabilities",
    "shareholders",
    "non current liabilities",
    "current liabilities",
]

# Trade payable MSME sub-row detection
MSME_KEYWORDS = ["micro enterprises", "small enterprises", "msme", "micro and small"]

# Trade payable Others sub-row detection
OTHER_CREDITORS_KEYWORDS = ["other than micro", "creditors other than", "other creditors"]

# Financial years: index 0 = current year column, index 1 = previous year
FINANCIAL_YEARS = ["2022-23", "2021-22"]

# Minimum rapidfuzz score (0–100) to accept a match
MATCH_THRESHOLD = 75