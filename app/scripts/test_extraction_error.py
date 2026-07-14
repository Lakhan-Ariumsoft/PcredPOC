import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.cma_extraction_service import extract_cma_fields

async def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    
    cache_path = Path("app/outputs/uploads/.ocr_cache/9e342e892ecc2e1400ff7e490d0beb7f.json")
    if not cache_path.exists():
        print(f"Error: Cache file not found at {cache_path}")
        return
        
    print(f"Loading cached pages from {cache_path}...")
    pages = json.loads(cache_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(pages)} pages.")
    
    # We will test extract_cma_fields on these pages
    print("Starting CMA field extraction...")
    try:
        result = await extract_cma_fields(
            pages=pages,
            source_file="CLL Standalone Financials AY 23-24.pdf",
            doc_id="CLL Standalone Financials AY 23-24",
            current_fy="2023-24",
            previous_fy="2022-23"
        )
        print("Extraction completed successfully!")
    except Exception as e:
        print(f"Extraction failed with exception: {e}")

if __name__ == "__main__":
    asyncio.run(main())
