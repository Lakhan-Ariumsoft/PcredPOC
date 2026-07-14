from pathlib import Path

def print_pl_main():
    md_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_raw_ocr.md")
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
        
    content = md_path.read_text(encoding="utf-8")
    pages = content.split("=== Page ")
    
    # Statement of Profit and Loss usually contains "statement of profit and loss" or "profit & loss"
    for p in pages:
        if "statement of profit and loss" in p.lower() or "profit and loss statement" in p.lower() or ("revenue from operations" in p.lower() and "employee benefit expenses" in p.lower() and "finance costs" in p.lower()):
            lines = p.split("\n")
            page_num = lines[0].split(" ===")[0]
            print(f"\n================ Page {page_num} ================")
            print("\n".join(lines[1:70]))
            print("================================================")
            break

if __name__ == "__main__":
    print_pl_main()
