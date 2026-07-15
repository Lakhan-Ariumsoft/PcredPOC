import json
from pathlib import Path
from typing import Any

from app.core_config import get_settings


class ProcessingLogger:
    def __init__(self) -> None:
        self.settings = get_settings()

    def write_text(self, job_id: str, name: str, content: str) -> Path:
        path = self.settings.log_dir / job_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, job_id: str, name: str, content: Any) -> Path:
        path = self.settings.log_dir / job_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")
        return path
