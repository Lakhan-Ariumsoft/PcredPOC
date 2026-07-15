# Financial Document OCR & Excel Extraction System

A Python-based FastAPI backend and CLI pipeline for converting financial PDFs/images (like financial statements, invoices, and receipts) into validated JSON and formatted, formula-preserving Excel workbooks.

## Pipeline Flow

```text
PDF/Image 
  └─> Docling OCR (via LM Studio) ──> Raw Markdown Tables
        └─> LLM Extraction (via LM Studio) ──> Structured JSON
              └─> Pydantic Validation ──> Excel Workbook (preserving formulas/formats)
```

---

## 1. Prerequisites & Setup

### A. Setup Local LLM Server (LM Studio)
The system runs entirely locally. You must set up LM Studio or a compatible server:
1. Download and open [LM Studio](https://lmstudio.ai/).
2. Download the recommended local models:
   - OCR/Parser model: `granite-docling-258m` (or any equivalent visual/text document model).
   - Extraction model: `gemma-4-2b-it` (or other instruction-tuned models).
3. Start the **Local Server** (OpenAI-compatible) on port `1234`.
4. Load the models inside LM Studio.

### B. Environment Installation
Clone the repository, initialize a Python virtual environment, and install dependencies:

**For Windows (PowerShell):**
```powershell
# Clone the repository and navigate inside
git clone <your-repo-url>
cd OCR

# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install required packages
pip install -r requirements.txt

# Create your .env file
copy .env.example .env
```

**For macOS/Linux:**
```bash
# Clone the repository and navigate inside
git clone <your-repo-url>
cd OCR

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install required packages
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
```

### C. Configuration (`.env`)
Verify the `.env` settings match your LM Studio setup:
```env
LM_STUDIO_BASE_URL=http://localhost:1234/v1
DOCLING_MODEL=granite-docling-258m
EXTRACTION_MODEL=gemma-4-2b-it
OUTPUT_DIR=app/outputs
LOG_DIR=app/outputs/logs
MAX_RETRIES=3
REQUEST_TIMEOUT_SECONDS=120
```

---

## 2. CLI Run Commands

### A. Run the Streamlined End-to-End CMA Pipeline
This generic, config-driven script handles OCR, financial statement parsing, year detection, and injecting the values into the master Excel sheet.
```powershell
python scripts/streamlined_pipeline.py --pdf "Input_Data/Financials 24-25(standalone).pdf" --excel "Input_Data/CARGOSOL LOGISTICS LTD CMA.xlsx" --year "2024-25"
```
* **Options:**
  - `--pdf`: Path to the input PDF financial statement.
  - `--excel`: Path to the master CMA Excel workbook.
  - `--year`: Target financial year (e.g. `2024-25`).
  - `--no-overrides`: Disables custom row overrides and writes JSON values directly.

### B. Run the 2022 Custom Extraction & Injection Pipeline
Run the dedicated extraction pipeline specifically configured for the 2022 financial year PDF:
```powershell
python scripts/run_2022_extraction_pipeline.py
```
This script saves raw markdown OCR logs, performs the extraction, maps values to columns, applies manual overrides for custom cells, and outputs `app/outputs/MASTER CARGOSOL LOGISTICS LTD CMA_with_2022.xlsx`.

### C. Run Verification and Balance Checks
After injecting data, verify that the Excel sheets balance and that all underlying Excel formulas compute successfully:

* **Verify the 2022 Injected File (Formula Evaluation & Balance check):**
  ```powershell
  python scripts/verify_2022_injected.py
  ```
* **Verify 3-Year Master Excel Sheet (FY23, FY24, and FY25 check):**
  ```powershell
  python scripts/verify_injected_master.py
  ```

### D. Run the Data Accuracy & Hallucination Audit
To confirm the integrity of the data and verify that no values were hallucinated, run the audit script. It compares every injected value in Column S (Excel) against the raw OCR text and logs the exact match contexts:
```powershell
python scripts/audit_accuracy.py
```

### E. Run Generic Document Processing (Invoices / Receipts)
For processing general financial images or smaller documents:
```powershell
python scripts/process_file.py .\sample-invoice.jpg
```

---

## 3. Running the FastAPI Web API Server

You can also run the system as a local web service to integrate with frontends or process requests via API:

```powershell
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`. 
- Open interactive API documentation (Swagger UI) at `http://127.0.0.1:8000/docs`.

### Key Endpoints:
- `POST /cma/inject/streamlined`: Upload a PDF, specify `target_year` (e.g., `2024-25`) and `apply_overrides` (boolean) to run the streamlined pipeline and download the resulting Excel file.
- `POST /upload`: Upload any invoice/financial document.
- `POST /extract`: Extract fields from the uploaded document.
- `POST /generate-excel`: Generate a standard formatted workbook from extracted JSON.

---

## 4. Running Automated Tests

To verify code correctness, run the unit test suite using `pytest`:
```powershell
pytest
```
