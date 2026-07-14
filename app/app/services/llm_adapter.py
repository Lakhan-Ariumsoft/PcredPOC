import abc
from typing import Any

from app.services.lm_studio_client import LMStudioClient


class LlmAdapter(abc.ABC):
    """Abstract base class for LLM text/chat generation adapters."""

    @abc.abstractmethod
    async def chat(self, messages: list[dict[str, Any]], temperature: float = 0.0) -> str:
        """Send chat messages to the LLM and return the generated content.

        Args:
            messages: List of chat messages (OpenAI format).
            temperature: LLM temperature parameter.

        Returns:
            The text response from the model.
        """
        pass


class LMStudioLlmAdapter(LlmAdapter):
    """Adapter for LLM text generation models hosted on LM Studio."""

    def __init__(self, client: LMStudioClient) -> None:
        self.client = client

    async def chat(self, messages: list[dict[str, Any]], temperature: float = 0.0) -> str:
        return await self.client.chat(messages, temperature)
