from pathlib import Path

def find_borrowings():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    for p in pages:
        if "short-term borrowings" in p.lower() or "short term borrowings" in p.lower() or "note 10" in p.lower():
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            if "borrowings" in p.lower() and "cash credit" in p.lower():
                print(f"\n================ Page {page_num} ================")
                print("\n".join(lines[1:50]))
                print("================================================")

if __name__ == "__main__":
    find_borrowings()
