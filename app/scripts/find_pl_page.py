from pathlib import Path

def find_pl():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    print("Searching for Profit & Loss page:")
    for p in pages:
        if "revenue from operations" in p.lower() or "profit before tax" in p.lower():
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:50]))
            print("================================================")

if __name__ == "__main__":
    find_pl()
