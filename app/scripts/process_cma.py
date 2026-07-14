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
    parser = argparse.ArgumentParser(description="Process a PDF/image into CMA Excel sheet.")
    parser.add_argument("file", type=Path, help="CLL Standalone Financials AY 23-24.pdf or other file")
    args = parser.parse_args()

    source = args.file.resolve()
    print(f"Starting CMA processing for {source.name}...")
    
    # 1. OCR all pages to markdown
    docling = DoclingService()
    print("Step 1: Running page-by-page OCR on the complete file...")
    pages = await docling.convert_to_pages(source)
    print(f"OCR complete. Processed {len(pages)} pages.")
    
    # Save raw markdown page texts to a log file for review
    log_dir = Path("app/outputs/logs/cma_run")
    log_dir.mkdir(parents=True, exist_ok=True)
    md_content = "\n\n".join(f"=== Page {p['page']} ===\n{p['text']}" for p in pages)
    md_log_path = log_dir / f"{source.stem}_raw_ocr.md"
    md_log_path.write_text(md_content, encoding="utf-8")
    print(f"Saved complete raw markdown text to {md_log_path}")
    
    # 2. Financial Year Detection
    current_fy, previous_fy = get_financial_year(source, pages)
    print(f"Step 2: Detected Financial Years: Current={current_fy}, Previous={previous_fy}")
    
    # 3. CMA Extraction
    print("Step 3: Extracting 199 CMA fields (multi-chunk routing)...")
    extraction = await extract_cma_fields(
        pages=pages,
        source_file=source.name,
        doc_id=source.stem,
        current_fy=current_fy,
        previous_fy=previous_fy
    )
    
    # 4. Merge
    print("Step 4: Merging document extraction results...")
    doc_results = [{
        "doc_id": source.stem,
        "filename": source.name,
        "status": "success",
        "current_fy": current_fy,
        "previous_fy": previous_fy,
        "extraction": extraction
    }]
    merged = merge_documents(source.stem.lower().replace(" ", "-"), "Cargosol Logistics Limited", doc_results)
    
    # 5. Validation
    print("Step 5: Running accounting sanity checks...")
    validated = validate_extraction(merged)
    
    val_log_path = log_dir / f"{source.stem}_validated.json"
    val_log_path.write_text(json.dumps(validated, indent=2), encoding="utf-8")
    print(f"Saved validated CMA extraction JSON to {val_log_path}")
    
    # 6. Excel Report
    print("Step 6: Generating XLCMA Excel spreadsheet...")
    excel_bytes = generate_cma_excel(validated)
    excel_out_path = Path("app/outputs") / f"{source.stem}_CMA.xlsx"
    excel_out_path.write_bytes(excel_bytes)
    print(f"CMA Excel spreadsheet successfully generated and saved to {excel_out_path}")

if __name__ == "__main__":
    asyncio.run(main())
