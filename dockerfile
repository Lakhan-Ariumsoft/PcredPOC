FROM python:3.11-slim

# System dependencies: Tesseract OCR + Poppler (pdf2image backend)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer caching — only rebuilt on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Runtime directories (Railway mounts a persistent volume at /data)
RUN mkdir -p /data/uploads /data/temp

# Tell the app where to store files (overrides the local default)
ENV UPLOADS_ROOT=/data/uploads
ENV TEMP_DIR=/data/temp

EXPOSE 8000


CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
