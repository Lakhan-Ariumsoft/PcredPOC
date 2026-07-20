class DocumentProcessingError(Exception):
    code = "document_processing_error"


class UnsupportedFormatError(DocumentProcessingError):
    code = "unsupported_format"


class EmptyDocumentError(DocumentProcessingError):
    code = "empty_document"


class OcrFailureError(DocumentProcessingError):
    code = "ocr_failure"


class LLMTimeoutError(DocumentProcessingError):
    code = "llm_timeout"


class LLMQuotaExceededError(DocumentProcessingError):
    """Raised when the LLM provider rejects a request for billing/quota
    reasons (e.g. OpenAI's insufficient_quota) — retrying won't help, so
    callers should fail fast and surface this distinctly rather than
    burning through retries and silently returning nulls."""
    code = "llm_quota_exceeded"


class JsonParseFailureError(DocumentProcessingError):
    code = "json_parse_failure"


class ExcelGenerationFailureError(DocumentProcessingError):
    code = "excel_generation_failure"
