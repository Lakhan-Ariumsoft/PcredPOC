from app.core_config import get_settings
from app.services.lm_studio_client import LMStudioClient
from app.services.ocr_adapter import OcrAdapter, LMStudioDoclingAdapter
from app.services.llm_adapter import LlmAdapter, LMStudioLlmAdapter


def get_ocr_adapter() -> OcrAdapter:
    settings = get_settings()
    client = LMStudioClient(
        settings.lm_studio_base_url,
        settings.docling_model,
        settings.request_timeout_seconds,
        api_key=settings.lm_studio_api_key,
    )
    return LMStudioDoclingAdapter(client)


def get_llm_adapter() -> LlmAdapter:
    settings = get_settings()
    client = LMStudioClient(
        settings.lm_studio_base_url,
        settings.extraction_model,
        settings.request_timeout_seconds,
        api_key=settings.lm_studio_api_key,
    )
    return LMStudioLlmAdapter(client)

