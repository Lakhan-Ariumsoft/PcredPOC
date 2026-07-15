from __future__ import annotations

from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    uploaded = "uploaded"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ProcessingJob(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.uploaded
    source_path: Path
    company_slug: Optional[str] = None
    doc_id: Optional[str] = None
    document_type: Optional[str] = None
    markdown_path: Optional[Path] = None
    validated_json_path: Optional[Path] = None
    excel_path: Optional[Path] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at_perf: float = Field(default_factory=perf_counter)
    processing_time_seconds: Optional[float] = None
