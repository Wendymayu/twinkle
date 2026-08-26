"""AgentClient 在 AgentServer 断连时 fail-fast 的测试.

当 recv loop 结束(AgentServer 崩溃 / ws 关闭)时,挂起的
send_request_stream 调用必须抛出 ConnectionError,而不是在空 queue 上永久挂起。
"""
from __future__ import annotations

import asyncio

import pytest

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.agent_client import AgentClient


class _NoopWS:
    """假的 ws,send() 是空操作(让 _send 在测试中成功)。"""
    async def send(self, data):
        pass


def test_fail_pending_pushes_disconnect_error_to_all_queues():
    """_fail_pending 往每个 pending request queue 推一个 ConnectionError。"""
    async def run():
        client = AgentClient("ws://ignored")
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        client._queues = {"r1": q1, "r2": q2}
        client._fail_pending("agent server disconnected")
        e1, e2 = q1.get_nowait(), q2.get_nowait()
        assert isinstance(e1, ConnectionError) and "disconnected" in str(e1)
        assert isinstance(e2, ConnectionError)

    asyncio.run(run())


def test_send_request_stream_raises_when_recv_loop_pushes_disconnect_error():
    """当 recv loop 往 pending queue 推一个 ConnectionError 时,
    send_request_stream 抛出它(不挂起,也不把它喂给
    E2AResponse.model_validate)。"""
    async def run():
        client = AgentClient("ws://ignored")
        client._ws = _NoopWS()  # bypass connect(); _send becomes a no-op

        env = E2AEnvelope(request_id="r1", method="chat.send", params={})

        async def _consume():
            async for _ in client.send_request_stream(env):
                pass

        task = asyncio.create_task(_consume())
        # 让消费者注册 queue 并走到 `await q.get()`.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 模拟 recv loop 退出,往 queue 推一个 disconnect 错误。
        client._queues["r1"].put_nowait(
            ConnectionError("agent server disconnected"))
        with pytest.raises(ConnectionError):
            await task

    asyncio.run(run())
