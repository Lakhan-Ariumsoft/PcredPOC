import json

import httpx
import pytest

from app.exceptions import LLMQuotaExceededError
from app.services.cma_extraction_service import _openai_call
from app.services.lm_studio_client import LMStudioClient, _is_quota_exhausted_error


# ── _is_quota_exhausted_error ────────────────────────────────────────────────

def test_detects_openai_insufficient_quota_shape():
    body = json.dumps({
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing details.",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    })
    assert _is_quota_exhausted_error(429, body) is True


def test_detects_text_only_quota_variant():
    assert _is_quota_exhausted_error(429, "Error: you have run out of credits") is True


def test_does_not_flag_ordinary_rate_limit():
    body = json.dumps({"error": {"type": "rate_limit_exceeded", "message": "Too many requests, slow down"}})
    assert _is_quota_exhausted_error(429, body) is False


def test_does_not_flag_unrelated_5xx():
    assert _is_quota_exhausted_error(500, "internal server error") is False


def test_does_not_flag_ordinary_401():
    # Wrong/expired API key is a different, separate failure mode — not quota.
    assert _is_quota_exhausted_error(401, "Invalid Authentication") is False


# ── LMStudioClient.chat raises LLMQuotaExceededError distinctly ────────────────

@pytest.mark.asyncio
async def test_chat_raises_quota_error_on_insufficient_quota_response(monkeypatch):
    client = LMStudioClient("http://fake/v1", "gpt-4o-mini", 30, api_key="sk-test")

    async def fake_resolve_model_name():
        return "gpt-4o-mini"
    monkeypatch.setattr(client, "_resolve_model_name", fake_resolve_model_name)

    quota_body = json.dumps({"error": {"code": "insufficient_quota", "type": "insufficient_quota"}})
    request = httpx.Request("POST", "http://fake/v1/chat/completions")
    response = httpx.Response(429, text=quota_body, request=request)

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: FakeAsyncClient())

    with pytest.raises(LLMQuotaExceededError):
        await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_raises_plain_http_error_for_non_quota_4xx(monkeypatch):
    client = LMStudioClient("http://fake/v1", "gpt-4o-mini", 30, api_key="sk-test")

    async def fake_resolve_model_name():
        return "gpt-4o-mini"
    monkeypatch.setattr(client, "_resolve_model_name", fake_resolve_model_name)

    request = httpx.Request("POST", "http://fake/v1/chat/completions")
    response = httpx.Response(401, text="Invalid Authentication", request=request)

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: FakeAsyncClient())

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat([{"role": "user", "content": "hi"}])


# ── _openai_call fails fast on quota errors instead of retrying 3x ────────────

@pytest.mark.asyncio
async def test_openai_call_does_not_retry_on_quota_error(monkeypatch):
    call_count = 0

    class FakeAdapter:
        async def chat(self, messages, temperature=0.0):
            nonlocal call_count
            call_count += 1
            raise LLMQuotaExceededError("no credits remaining")

    monkeypatch.setattr(
        "app.services.cma_extraction_service.get_llm_adapter", lambda: FakeAdapter()
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-not-mock")

    with pytest.raises(LLMQuotaExceededError):
        await _openai_call("Sales", ["Net Sales"], "some context", "2023-24", "2022-23", "Lakhs")

    assert call_count == 1, "quota errors must not be retried — retrying a zero-balance account can't succeed"
