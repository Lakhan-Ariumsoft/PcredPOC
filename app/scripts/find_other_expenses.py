from pathlib import Path

def find_other_expenses():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    for p in pages:
        if "other expenses" in p.lower() and ("administrative" in p.lower() or "selling" in p.lower() or "audit" in p.lower()):
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:60]))
            print("================================================")

if __name__ == "__main__":
    find_other_expenses()
