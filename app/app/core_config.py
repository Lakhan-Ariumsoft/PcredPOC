from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Toggle: "openai" or "qwen" — selects which credential set below is active.
    llm_provider: str = Field(default="qwen", alias="LLM_PROVIDER")

    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_docling_model: str = Field(default="gpt-4o-mini", alias="OPENAI_DOCLING_MODEL")
    openai_extraction_model: str = Field(default="gpt-4o-mini", alias="OPENAI_EXTRACTION_MODEL")

    qwen_base_url: str = Field(default="http://localhost:1234/v1", alias="QWEN_BASE_URL")
    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_docling_model: str = Field(default="Qwen/Qwen2.5-VL-7B-Instruct", alias="QWEN_DOCLING_MODEL")
    qwen_extraction_model: str = Field(default="Qwen/Qwen2.5-VL-7B-Instruct", alias="QWEN_EXTRACTION_MODEL")

    output_dir: Path = Field(default=Path("app/outputs"), alias="OUTPUT_DIR")
    log_dir: Path = Field(default=Path("app/outputs/logs"), alias="LOG_DIR")
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    request_timeout_seconds: float = Field(default=120, alias="REQUEST_TIMEOUT_SECONDS")
    ocr_start_page: int = Field(default=1, ge=1, alias="OCR_START_PAGE")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", populate_by_name=True)

    @property
    def lm_studio_base_url(self) -> str:
        return self.openai_base_url if self.llm_provider == "openai" else self.qwen_base_url

    @property
    def lm_studio_api_key(self) -> str:
        return self.openai_api_key if self.llm_provider == "openai" else self.qwen_api_key

    @property
    def docling_model(self) -> str:
        return self.openai_docling_model if self.llm_provider == "openai" else self.qwen_docling_model

    @property
    def extraction_model(self) -> str:
        return self.openai_extraction_model if self.llm_provider == "openai" else self.qwen_extraction_model


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    project_root = Path(__file__).resolve().parent.parent
    if not settings.output_dir.is_absolute():
        settings.output_dir = (project_root / settings.output_dir).resolve()
    if not settings.log_dir.is_absolute():
        settings.log_dir = (project_root / settings.log_dir).resolve()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings
