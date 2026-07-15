from pathlib import Path

from app.services.job_store import job_store
from app.utils.files import SUPPORTED_EXTENSIONS


class BatchService:
    def create_jobs_from_folder(self, folder: Path) -> list:
        jobs = []
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS and path.suffix.lower() != ".zip":
                jobs.append(job_store.create(path))
        return jobs
