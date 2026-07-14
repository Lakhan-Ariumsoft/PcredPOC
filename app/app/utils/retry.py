from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(operation: Callable[[], Awaitable[T]], attempts: int, delay_seconds: float = 0.5) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(delay_seconds * (attempt + 1))
    assert last_error is not None
    raise last_error
