"""Tests for AgentClient fail-fast on AgentServer disconnect.

When the recv loop ends (AgentServer crashed / ws closed), pending
send_request_stream calls must raise ConnectionError instead of hanging forever
on an empty queue.
"""
from __future__ import annotations

import asyncio

import pytest

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.agent_client import AgentClient


class _NoopWS:
    """Fake ws whose send() is a no-op (lets _send succeed for the test)."""
    async def send(self, data):
        pass


def test_fail_pending_pushes_disconnect_error_to_all_queues():
    """_fail_pending pushes a ConnectionError into every pending request queue."""
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
    """When the recv loop pushes a ConnectionError into the pending queue,
    send_request_stream raises it (does NOT hang, does NOT feed it to
    E2AResponse.model_validate)."""
    async def run():
        client = AgentClient("ws://ignored")
        client._ws = _NoopWS()  # bypass connect(); _send becomes a no-op

        env = E2AEnvelope(request_id="r1", method="chat.send", params={})

        async def _consume():
            async for _ in client.send_request_stream(env):
                pass

        task = asyncio.create_task(_consume())
        # Let the consumer register its queue and reach `await q.get()`.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Simulate recv loop exit pushing a disconnect error into the queue.
        client._queues["r1"].put_nowait(
            ConnectionError("agent server disconnected"))
        with pytest.raises(ConnectionError):
            await task

    asyncio.run(run())
