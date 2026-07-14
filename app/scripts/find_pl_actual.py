from pathlib import Path

def print_pl_actual():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    # Let's search for Note 21 or Note 22 or "employee benefit" in the first 20 pages
    for i, p in enumerate(pages[:20]):
        if "employee benefit" in p.lower() and "finance costs" in p.lower():
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:70]))
            print("================================================")

if __name__ == "__main__":
    print_pl_actual()
