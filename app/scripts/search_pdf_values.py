import re
from pathlib import Path

def search_values():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    
    # We will search for several key values from the yellow rows
    values_to_search = [
        "17012.85", "11354.87", # Domestic Sale
        "14349.97", "9980.89",   # Freight & Handling
        "124.95",   "64",        # Vehicle Running
        "102.53",   "82.42",     # Selling Expenses
        "1530.51",  "1093.57",   # Administrative
        "6.61",     "37.05",     # Other Interest
        "145.39",   "141.24",    # Interest/Dividend/Royalties
        "15.63",    "41.89",     # Other Income
        "1126.84",  "1340.66",   # ST Bank Loan
        "50.15",    "83.09",     # ST Other Banks
        "63.17",                 # Other Statutory
        "388.6",                 # Share Premium
        "17.79",    "96.16",     # Cash
        "62.02",    "52.96",     # FD
        "293.93",                # Deferred Receivables
        "84.46",    "148.23",    # Advances to Suppliers
        "1394.25",  "3343.02",   # Gross Block
        "1954",     "98.34",     # CWIP
        "38.97",                 # Investment in Others
        "255.88",   "272.63",    # Deferred Receivables LT
        "22.78",    "35.58",     # Security Deposits
        "43.29",    "166.76",    # DTA
        "43.31",    "137.56",    # Advance Tax/TDS
    ]
    
    # Split content by pages
    pages = content.split("=== Page ")
    
    print("Searching for values in OCR Markdown:")
    for val in values_to_search:
        # Create a flexible regex pattern to match numbers with or without commas
        # E.g. 17012.85 or 17,012.85
        # If val is just digits, match boundary
        val_escaped = re.escape(val)
        if "." in val:
            parts = val.split(".")
            pattern = re.compile(rf"\b{parts[0][:3]}[\s,]*{parts[0][3:]}\.{parts[1]}\b|\b{val_escaped}\b")
        else:
            pattern = re.compile(rf"\b{val_escaped}\b")
            
        found = False
        print(f"\n--- Searching for: {val} ---")
        for p in pages:
            if not p.strip():
                continue
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            
            for i, line in enumerate(lines[1:]):
                if pattern.search(line):
                    found = True
                    start = max(0, i - 2)
                    end = min(len(lines), i + 4)
                    context = "\n".join(lines[1+start:1+end])
                    print(f"Page {page_num} (Line {i+1}):")
                    print(context)
                    print("-" * 40)
        if not found:
            print("NOT FOUND in raw OCR.")

if __name__ == "__main__":
    search_values()
