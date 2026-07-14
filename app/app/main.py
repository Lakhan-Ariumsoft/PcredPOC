import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.extraction import router as extraction_router
from app.api.upload import router as upload_router
from app.api.cma import router as cma_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(
    title="Financial Document OCR & Excel Extraction System",
    version="1.0.0",
    description="Local-only OCR and structured Excel extraction backend using LM Studio models.",
)

# Local-only dev API — allow any origin so Swagger UI / a local frontend
# never gets blocked by a browser-side CORS check regardless of what host
# or scheme it's served from.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload_router)
app.include_router(extraction_router)
app.include_router(cma_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
