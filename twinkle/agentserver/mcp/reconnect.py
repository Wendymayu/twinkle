"""传输层重连——只挂 streamable-http。stdio 进程崩了直接抛(对齐 jiuwenswarm)。"""
from __future__ import annotations

import functools
from typing import Awaitable, Callable

RETRYABLE_TRANSPORT_MARKERS = (
    "session terminated", "closedresourceerror", "broken pipe",
    "connection closed", "connection reset", "incomplete streamed",
)


def is_retryable_transport_error(exc: BaseException) -> bool:
    """可重试传输层错误白名单(对齐 jiuwenswarm is_retryable_transport_error)。"""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in RETRYABLE_TRANSPORT_MARKERS)


def with_reconnect(fn: Callable[..., Awaitable[str]]):
    """装饰 list_tools/call_tool:撞可重试传输错误 → disconnect+connect 重试,attempts 次耗尽抛原异常。

    被 wraps 的函数签名须为 (client, *args, attempts=3, **kwargs),client 需有 connect/disconnect。
    attempts<=0 视为 1(至少试一次)。
    """
    @functools.wraps(fn)
    async def wrapper(client, *args, attempts: int = 3, **kwargs):
        last_exc: BaseException | None = None
        for _ in range(max(1, attempts)):
            try:
                return await fn(client, *args, **kwargs)
            except Exception as exc:
                if not is_retryable_transport_error(exc):
                    raise
                last_exc = exc
                await client.disconnect()
                await client.connect()
        assert last_exc is not None
        raise last_exc
    return wrapper
