"""Task 6: mcp.reconnect.with_reconnect — streamable-http 传输层重连。

撞可重试传输错误(ConnectionError / ClosedResourceError 等)→ disconnect+connect 重试,
attempts 次耗尽抛原异常。非可重试错误(ValueError 等)直接抛,不重试。
stdio 进程崩了不重试(本装饰器只挂 http client)。
"""
import asyncio
import pytest
from twinkle.agentserver.mcp.reconnect import with_reconnect, is_retryable_transport_error


class _FakeClient:
    def __init__(self, excs, result="ok"):
        self._excs = list(excs)
        self._result = result
        self.connects = 0
        self.disconnects = 0
    async def connect(self):
        self.connects += 1
    async def disconnect(self):
        self.disconnects += 1
    async def _do(self):
        if self._excs:
            exc = self._excs.pop(0)
            if exc is not None:
                raise exc
        return self._result


def test_retryable_error_triggers_reconnect_then_succeeds() -> None:
    c = _FakeClient([ConnectionError("connection closed"), None])
    c.connects = 1  # already connected

    @with_reconnect
    async def call(client):
        return await client._do()

    assert asyncio.run(call(c, attempts=3)) == "ok"
    assert c.disconnects == 1  # reconnected once
    assert c.connects == 2


def test_attempts_exhausted_raises_original() -> None:
    c = _FakeClient([ConnectionError("connection closed"),
                     ConnectionError("connection closed"),
                     ConnectionError("connection closed")])

    @with_reconnect
    async def call(client):
        return await client._do()

    with pytest.raises(ConnectionError, match="connection closed"):
        asyncio.run(call(c, attempts=3))


def test_non_retryable_error_not_retried() -> None:
    c = _FakeClient([ValueError("not retryable")])

    @with_reconnect
    async def call(client):
        return await client._do()

    with pytest.raises(ValueError, match="not retryable"):
        asyncio.run(call(c, attempts=3))
    assert c.disconnects == 0


def test_is_retryable_matches_whitelist() -> None:
    assert is_retryable_transport_error(ConnectionError("session terminated"))
    # ClosedResourceError(anyio)——用同名 fake 类测,避免依赖 anyio 是否安装
    class ClosedResourceError(Exception):
        pass
    assert is_retryable_transport_error(ClosedResourceError("x"))
    assert not is_retryable_transport_error(ValueError("nope"))


def test_attempts_zero_makes_single_attempt_and_succeeds() -> None:
    """attempts=0 仍至少试一次——修正前 range(0) 导致 fn 不被调用、AssertionError。"""
    c = _FakeClient([None])  # 首次即成功(无异常)
    c.connects = 1  # already connected

    @with_reconnect
    async def call(client):
        return await client._do()

    assert asyncio.run(call(c, attempts=0)) == "ok"
    assert c.disconnects == 0  # 成功,无需重连


def test_attempts_zero_propagates_error_no_assertionerror() -> None:
    """attempts=0 撞可重试错误 → 抛原异常(非 AssertionError;python -O 下非 TypeError)。"""
    c = _FakeClient([ConnectionError("connection closed")])

    @with_reconnect
    async def call(client):
        return await client._do()

    with pytest.raises(ConnectionError, match="connection closed"):
        asyncio.run(call(c, attempts=0))
