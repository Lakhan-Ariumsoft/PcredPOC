FROM python:3.11-slim

WORKDIR /app

# Install Python deps first (layer caching — only rebuilt on requirements change).
# No system OCR packages needed: the pipeline OCRs via a vision LLM API call
# (see app/services/docling_service.py), not local Tesseract/Poppler — PyMuPDF
# and Pillow both ship self-contained wheels on this platform.
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. The importable package lives at app/app/ in the repo
# (after the app/app/ reorg), not app/ — copying app/ here would nest the
# package one level too deep and uvicorn's entrypoint below would 404 on import.
COPY app/app/ ./app/

# api/cma.py's /cma/inject/streamlined endpoint does a runtime
# `from scripts.streamlined_pipeline import ...` — scripts/ must be
# importable as a top-level package alongside app/, or that one endpoint
# 404s on import (the rest of the app is unaffected since it's a lazy,
# in-function import, not a module-level one).
COPY app/scripts/ ./scripts/

# OUTPUT_DIR/LOG_DIR are the settings core_config.py actually reads (this
# used to set UPLOADS_ROOT/TEMP_DIR, which nothing in the app ever read, so
# uploads silently landed in the container's ephemeral filesystem instead of
# the persistent volume). Railway mounts a persistent volume at /data; the
# app creates its own subdirectories under it on startup.
ENV OUTPUT_DIR=/data
ENV LOG_DIR=/data/logs

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
