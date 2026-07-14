from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from app.models.job import JobStatus, ProcessingJob


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, ProcessingJob] = {}

    def create(self, source_path: Path, company_slug: Optional[str] = None, doc_id: Optional[str] = None) -> ProcessingJob:
        job = ProcessingJob(
            job_id=uuid4().hex,
            source_path=source_path,
            company_slug=company_slug,
            doc_id=doc_id,
        )
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[ProcessingJob]:
        return self._jobs.get(job_id)

    def list_by_company(self, company_slug: str) -> list:
        return [j for j in self._jobs.values() if j.company_slug == company_slug]

    def update(self, job: ProcessingJob, **changes: object) -> ProcessingJob:
        updated = job.model_copy(update=changes)
        self._jobs[updated.job_id] = updated
        return updated

    def mark_failed(self, job: ProcessingJob, code: str, message: str) -> ProcessingJob:
        return self.update(job, status=JobStatus.failed, error_code=code, error_message=message)


job_store = InMemoryJobStore()
