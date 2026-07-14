from pathlib import Path
import re

def search_borrowings():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    print("Searching for borrowings or loan numbers:")
    for p in pages:
        if not p.strip():
            continue
        lines = p.split("\n")
        page_num = lines[0].split(" ===")[0]
        
        # Check if page contains keywords or numbers
        p_lower = p.lower()
        if "1,126.84" in p or "1,340.66" in p or "1340.66" in p or "1126.84" in p or "cash credit" in p_lower or "working capital" in p_lower:
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:60]))
            print("================================================")

if __name__ == "__main__":
    search_borrowings()
