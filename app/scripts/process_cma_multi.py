import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path to allow execution from any directory
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.docling_service import DoclingService
from app.services.cma_extraction_service import extract_cma_fields
from app.services.merger import merge_documents
from app.services.cma_validator import validate_extraction
from app.services.cma_reporter_service import generate_cma_excel
from app.utils.fy_detector import get_financial_year

async def main() -> None:
    # Configure logging to output INFO logs to stdout to track progress in real-time
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )
    # 1. Source Paths
    doc1_json_path = Path("app/outputs/logs/cma_run/CLL Standalone Financials AY 23-24_validated.json")
    doc2_pdf_path = Path("Input_Data/Financials 24-25(standalone).pdf")
    
    if not doc1_json_path.exists():
        print(f"Error: Previous extraction JSON not found at {doc1_json_path}")
        sys.exit(1)
    if not doc2_pdf_path.exists():
        print(f"Error: PDF for FY25 not found at {doc2_pdf_path}")
        sys.exit(1)
        
    print(f"Loading Doc 1 (FY23/FY24) from {doc1_json_path}...")
    doc1_data = json.loads(doc1_json_path.read_text(encoding="utf-8"))
    
    # Reconstruct original extraction structure for Doc 1
    doc1_extraction = {
        "meta": {
            "fields_found": doc1_data["documents"][0]["fields_found"] if doc1_data["documents"] else 0
        },
        "sections": {}
    }
    
    for sk, section_data in doc1_data.get("cma_data", {}).items():
        doc1_extraction["sections"][sk] = {"fields": {}}
        for field_name, year_map in section_data.get("fields", {}).items():
            doc1_extraction["sections"][sk]["fields"][field_name] = {
                "current": year_map.get("2023-24", {}),
                "previous": year_map.get("2022-23", {})
            }
            
    print("Doc 1 extraction successfully reconstructed.")

    # 2. OCR FY25 Standalone PDF
    print(f"\nStep 1: Running page-by-page OCR on {doc2_pdf_path.name}...")
    docling = DoclingService()
    pages = await docling.convert_to_pages(doc2_pdf_path)
    print(f"OCR complete. Processed {len(pages)} pages.")
    
    # Save raw markdown page texts to a log file for review
    log_dir = Path("app/outputs/logs/cma_run")
    log_dir.mkdir(parents=True, exist_ok=True)
    md_content = "\n\n".join(f"=== Page {p['page']} ===\n{p['text']}" for p in pages)
    md_log_path = log_dir / f"{doc2_pdf_path.stem}_raw_ocr.md"
    md_log_path.write_text(md_content, encoding="utf-8")
    print(f"Saved complete raw markdown text to {md_log_path}")
    
    # 3. Financial Year Detection for FY25 Standalone PDF
    current_fy, previous_fy = get_financial_year(doc2_pdf_path, pages)
    print(f"\nStep 2: Detected Financial Years: Current={current_fy}, Previous={previous_fy}")
    if current_fy != "2024-25":
        print(f"Warning: Expected current financial year 2024-25, but detected {current_fy}.")
        print("Overriding to Current=2024-25, Previous=2023-24.")
        current_fy = "2024-25"
        previous_fy = "2023-24"
    
    # 4. Extract CMA Fields for FY25 Standalone PDF
    print("\nStep 3: Extracting 199 CMA fields (multi-chunk routing) for FY25 PDF...")
    doc2_extraction = await extract_cma_fields(
        pages=pages,
        source_file=doc2_pdf_path.name,
        doc_id=doc2_pdf_path.stem,
        current_fy=current_fy,
        previous_fy=previous_fy
    )
    
    # 5. Merge the results of Doc 1 and Doc 2
    print("\nStep 4: Merging Doc 1 (FY23/FY24) and Doc 2 (FY24/FY25) extraction results...")
    doc_results = [
        {
            "doc_id": "CLL Standalone Financials AY 23-24",
            "filename": "CLL Standalone Financials AY 23-24.pdf",
            "status": "success",
            "current_fy": "2023-24",
            "previous_fy": "2022-23",
            "extraction": doc1_extraction
        },
        {
            "doc_id": doc2_pdf_path.stem,
            "filename": doc2_pdf_path.name,
            "status": "success",
            "current_fy": current_fy,
            "previous_fy": previous_fy,
            "extraction": doc2_extraction
        }
    ]
    
    merged = merge_documents("cargosol-3years", "Cargosol Logistics Limited", doc_results)
    print(f"Merged successfully. Years available: {merged.get('financial_years')}")
    
    # 6. Validate the merged extraction
    print("\nStep 5: Running accounting sanity checks on merged 3-year data...")
    validated = validate_extraction(merged)
    
    val_log_path = log_dir / "Cargosol_Logistics_LTD_CMA_3years_validated.json"
    val_log_path.write_text(json.dumps(validated, indent=2), encoding="utf-8")
    print(f"Saved validated 3-year CMA extraction JSON to {val_log_path}")
    
    # 7. Generate Excel Report
    print("\nStep 6: Generating XLCMA 3-year Excel spreadsheet...")
    excel_bytes = generate_cma_excel(validated)
    
    excel_out_path = Path("app/outputs") / "Cargosol_Logistics_LTD_CMA_3years.xlsx"
    try:
        excel_out_path.write_bytes(excel_bytes)
        print(f"CMA 3-year Excel spreadsheet successfully generated and saved to {excel_out_path}")
    except PermissionError:
        fallback_path = Path("app/outputs") / "Cargosol_Logistics_LTD_CMA_3years_reconciled.xlsx"
        fallback_path.write_bytes(excel_bytes)
        print(f"Permission denied on {excel_out_path} (likely open in Excel). Saved instead to: {fallback_path}")

if __name__ == "__main__":
    asyncio.run(main())
