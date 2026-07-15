from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.llm_adapter import LMStudioLlmAdapter
from app.services.ocr_adapter import LMStudioDoclingAdapter


@pytest.mark.asyncio
async def test_ocr_adapter_calls_client() -> None:
    mock_client = AsyncMock()
    mock_client.vision_chat.return_value = "# Header\nSome content"
    adapter = LMStudioDoclingAdapter(mock_client)

    res = await adapter.convert(Path("dummy.pdf"), "application/pdf")
    assert res == "# Header\nSome content"
    mock_client.vision_chat.assert_called_once()
    call_args = mock_client.vision_chat.call_args[1]
    assert "Convert this financial document" in call_args["prompt"]
    assert call_args["file_path"] == Path("dummy.pdf")
    assert call_args["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_llm_adapter_calls_client() -> None:
    mock_client = AsyncMock()
    mock_client.chat.return_value = '{"invoice_number": "INV-1"}'
    adapter = LMStudioLlmAdapter(mock_client)

    messages = [{"role": "user", "content": "Extract fields"}]
    res = await adapter.chat(messages, temperature=0.2)
    assert res == '{"invoice_number": "INV-1"}'
    mock_client.chat.assert_called_once_with(messages, 0.2)
