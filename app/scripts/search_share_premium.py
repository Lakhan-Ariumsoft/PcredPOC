from pathlib import Path

def search_share_premium():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    for p in pages:
        if "388.6" in p or "securities premium" in p.lower() or "share premium" in p.lower():
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:50]))
            print("================================================")

if __name__ == "__main__":
    search_share_premium()
