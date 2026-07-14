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


class JsonParseFailureError(DocumentProcessingError):
    code = "json_parse_failure"


class ExcelGenerationFailureError(DocumentProcessingError):
    code = "excel_generation_failure"
