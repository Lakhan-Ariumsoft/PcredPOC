from __future__ import annotations

from pathlib import Path
from time import perf_counter

from app.core_config import get_settings
from app.exceptions import DocumentProcessingError
from app.models.job import JobStatus, ProcessingJob
from app.services.classifier import DocumentClassifier
from app.services.docling_service import DoclingService
from app.services.excel_service import ExcelService
from app.services.job_store import job_store
from app.services.llm_service import FinancialExtractionService
from app.services.logging_service import ProcessingLogger
from app.services.validation_service import ValidationService


class PipelineService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.docling = DoclingService()
        self.classifier = DocumentClassifier()
        self.extractor = FinancialExtractionService()
        self.validator = ValidationService()
        self.excel = ExcelService()
        self.logger = ProcessingLogger()

    async def process(self, job: ProcessingJob) -> ProcessingJob:
        started = perf_counter()
        job = job_store.update(job, status=JobStatus.processing)
        try:
            markdown = await self.docling.convert_to_markdown(job.source_path)
            markdown_path = self.logger.write_text(job.job_id, "raw_docling_output.md", markdown)

            classification = await self.classifier.classify(markdown)
            extracted = await self.extractor.extract(markdown, classification.document_type)
            self.logger.write_json(job.job_id, "raw_llm_output.json", extracted)

            validated = self.validator.validate(extracted)
            validated_path = self.logger.write_json(job.job_id, "validated_json.json", validated.model_dump())

            excel_path = self.settings.output_dir / f"{job.job_id}.xlsx"
            self.excel.generate(validated, excel_path)
            self.logger.write_json(
                job.job_id,
                "processing_log.json",
                {
                    "document_type": classification.document_type.value,
                    "classification_confidence": classification.confidence,
                    "classification_source": classification.source,
                    "excel_path": str(excel_path),
                    "processing_time": perf_counter() - started,
                },
            )

            return job_store.update(
                job,
                status=JobStatus.completed,
                document_type=classification.document_type.value,
                markdown_path=markdown_path,
                validated_json_path=validated_path,
                excel_path=excel_path,
                processing_time_seconds=perf_counter() - started,
            )
        except DocumentProcessingError as exc:
            return job_store.mark_failed(job, exc.code, str(exc))
        except Exception as exc:
            return job_store.mark_failed(job, "unexpected_error", str(exc))


def get_download_path(job_id: str) -> Path | None:
    job = job_store.get(job_id)
    if not job or not job.excel_path:
        return None
    return job.excel_path
