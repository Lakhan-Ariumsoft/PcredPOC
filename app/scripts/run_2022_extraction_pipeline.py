import sys
import asyncio
import json
import logging
import shutil
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.docling_service import DoclingService
from app.services.cma_extraction_service import extract_cma_fields
from app.utils.fy_detector import get_financial_year
from app.services.merger import merge_documents
from app.services.cma_excel_injector import inject_cma_data_from_json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("CMA_2022_Pipeline")

async def run_pipeline():
    pdf_path = Path("Input_Data/CLL FY 2022-23 Standalone Financials.pdf")
    master_excel = Path("Input_Data/MASTER CARGOSOL LOGISTICS LTD CMA.xlsx")
    output_excel = Path("app/outputs/MASTER CARGOSOL LOGISTICS LTD CMA_with_2022.xlsx")
    
    if not pdf_path.exists():
        logger.error(f"PDF file not found: {pdf_path}")
        return False
    if not master_excel.exists():
        logger.error(f"Master Excel file not found: {master_excel}")
        return False
        
    logger.info(f"Step 1: Running OCR on {pdf_path.name}...")
    docling = DoclingService()
    pages = await docling.convert_to_pages(pdf_path)
    logger.info(f"OCR completed successfully. Processed {len(pages)} pages.")
    
    # Save raw markdown page texts to a log file for review
    log_dir = Path("app/outputs/logs/cma_run")
    log_dir.mkdir(parents=True, exist_ok=True)
    md_content = "\n\n".join(f"=== Page {p['page']} ===\n{p['text']}" for p in pages)
    md_log_path = log_dir / f"{pdf_path.stem}_raw_ocr.md"
    md_log_path.write_text(md_content, encoding="utf-8")
    logger.info(f"Raw markdown text saved to {md_log_path}")
    
    logger.info("Step 2: Detecting Financial Year...")
    current_fy, previous_fy = get_financial_year(pdf_path, pages)
    logger.info(f"Detected Financial Years: Current={current_fy}, Previous={previous_fy}")
    
    # Verify detected year is correct
    if current_fy != "2022-23":
        logger.warning(f"Expected current financial year 2022-23, but detected {current_fy}. Overriding.")
        current_fy = "2022-23"
        previous_fy = "2021-22"
        
    logger.info("Step 3: Extracting structured CMA fields from statement...")
    extracted_data = await extract_cma_fields(
        pages=pages,
        source_file=pdf_path.name,
        doc_id=pdf_path.stem,
        current_fy=current_fy,
        previous_fy=previous_fy
    )
    
    # Save raw extraction output to file
    extraction_json_path = log_dir / f"{pdf_path.stem}_extraction.json"
    extraction_json_path.write_text(json.dumps(extracted_data, indent=2), encoding="utf-8")
    logger.info(f"Saved extraction JSON to {extraction_json_path}")
    
    logger.info("Step 4: Merging extraction results to convert keys to years...")
    doc_results = [{
        "doc_id": pdf_path.stem,
        "filename": pdf_path.name,
        "status": "success",
        "current_fy": current_fy,
        "previous_fy": previous_fy,
        "extraction": extracted_data
    }]
    merged = merge_documents("cargosol-2022", "Cargosol Logistics Limited", doc_results)
    
    merged_json_path = log_dir / f"{pdf_path.stem}_merged.json"
    merged_json_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info(f"Saved merged year-mapped JSON to {merged_json_path}")
    
    # Extract the exact values for Gross Block (row 182) and Depreciation to Date (row 184) to use as overrides
    # Section: fixed_assets, Fields: 'Gross Block', 'Depreciation to Date'
    fixed_assets = merged.get("cma_data", {}).get("fixed_assets", {}).get("fields", {})
    
    val_182_raw = fixed_assets.get("Gross Block", {}).get("2021-22", {}).get("value")
    val_184_raw = fixed_assets.get("Depreciation to Date", {}).get("2021-22", {}).get("value")
    
    logger.info(f"Extracted raw values for 2021-22: Gross Block (182) = {val_182_raw}, Depreciation to Date (184) = {val_184_raw}")
    
    # Set overrides to make sure Gross Block and Depreciation to Date are written as static values in Column S (2022)
    # instead of formulas referencing Column R (2028 projection)
    overrides = {}
    if val_182_raw is not None:
        overrides[182] = float(val_182_raw)
    if val_184_raw is not None:
        overrides[184] = float(val_184_raw)
        
    logger.info(f"Applying overrides: {overrides}")
    
    logger.info(f"Step 5: Injecting values into output Excel workbook {output_excel}...")
    inject_cma_data_from_json(
        excel_path=output_excel,
        json_data=merged,
        target_year="2021-22", # Column for 2022 is target column
        override_vals=overrides
    )
    
    logger.info("Pipeline executed successfully and output Excel sheet has been updated.")
    return True

if __name__ == "__main__":
    asyncio.run(run_pipeline())
