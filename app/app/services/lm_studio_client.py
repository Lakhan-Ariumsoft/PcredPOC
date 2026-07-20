import base64
from pathlib import Path
from typing import Any

import fitz
import httpx

from app.exceptions import LLMQuotaExceededError, LLMTimeoutError

# Substrings that show up across providers' out-of-credit/quota responses.
# OpenAI's is the canonical shape ({"error": {"code": "insufficient_quota", ...}}
# at HTTP 429), but this also catches text-only variants from other
# OpenAI-compatible endpoints (HF Inference Endpoints, etc.) that don't
# follow the exact same JSON structure.
_QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "you have run out of credits",
    "credit balance is too low",
    "billing_hard_limit_reached",
)


def _is_quota_exhausted_error(status_code: int, body_text: str) -> bool:
    if status_code not in (402, 429):
        return False
    body_lower = body_text.lower()
    return any(marker in body_lower for marker in _QUOTA_ERROR_MARKERS)


class LMStudioClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: float, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _resolve_model_name(self) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers)
                if resp.status_code == 200:
                    models = [m["id"] for m in resp.json().get("data", [])]
                    if self.model in models:
                        return self.model
                    
                    # Exact or substring match first
                    for m in models:
                        if self.model.lower() in m.lower() or m.lower() in self.model.lower():
                            return m
                            
                    # Keyword prefix/suffix match (e.g., matching "gemma", "qwen", or "granite")
                    keywords = ["gemma", "qwen", "granite", "docling", "llama", "deepseek"]
                    for kw in keywords:
                        if kw in self.model.lower():
                            for m in models:
                                if kw in m.lower():
                                    return m
                                    
                    if models:
                        return models[0]
        except Exception:
            pass
        return self.model

    async def chat(self, messages: list[dict[str, Any]], temperature: float = 0.0) -> str:
        model_to_use = await self._resolve_model_name()
        payload = {
            "model": model_to_use,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LM Studio request timed out for model {self.model}") from exc
        except httpx.HTTPStatusError as exc:
            import sys
            sys.stderr.write(f"LM Studio error response: {exc.response.text}\n")
            sys.stderr.flush()
            if _is_quota_exhausted_error(exc.response.status_code, exc.response.text):
                raise LLMQuotaExceededError(
                    f"The {self.model} provider rejected the request for billing/quota reasons "
                    f"(HTTP {exc.response.status_code}) — no credits/quota remaining. "
                    "Add billing credits or switch LLM_PROVIDER, then retry."
                ) from exc
            raise exc
        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def vision_chat(self, prompt: str, file_path: Path, mime_type: str) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_payload in self._image_payloads(file_path, mime_type):
            content.append(image_payload)
        messages = [
            {
                "role": "user",
                "content": content,
            }
        ]
        return await self.chat(messages)

    def _image_payloads(self, file_path: Path, mime_type: str) -> list[dict[str, Any]]:
        if file_path.suffix.lower() == ".pdf":
            return self._pdf_payloads(file_path)
        
        from PIL import Image
        import io
        try:
            with Image.open(file_path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=90)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                return [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}]
        except Exception:
            encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
            return [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}]

    def _pdf_payloads(self, file_path: Path) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        with fitz.open(file_path) as document:
            for page in document:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                payloads.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        return payloads
