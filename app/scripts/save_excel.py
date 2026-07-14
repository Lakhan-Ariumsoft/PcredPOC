import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.cma_reporter_service import generate_cma_excel

def main():
    json_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_validated.json")
    if not json_path.exists():
        print(f"Error: validated JSON not found at {json_path}")
        sys.exit(1)
        
    print(f"Loading validated extraction from {json_path}...")
    validated = json.loads(json_path.read_text(encoding="utf-8"))
    
    print("Generating Excel report...")
    excel_bytes = generate_cma_excel(validated)
    
    # Try writing to the standard name first
    target_path = Path("app/outputs/CLL Standalone Financials AY 23-24_CMA.xlsx")
    try:
        target_path.write_bytes(excel_bytes)
        print(f"Success! Re-generated Excel sheet saved to {target_path}")
    except PermissionError:
        # If open in Excel, save to a fallback version
        fallback_path = Path("app/outputs/CLL Standalone Financials AY 23-24_CMA_reconciled.xlsx")
        fallback_path.write_bytes(excel_bytes)
        print(f"Permission denied on primary file (likely open in Excel). Saved instead to: {fallback_path}")

if __name__ == "__main__":
    main()
