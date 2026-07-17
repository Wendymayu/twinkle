"""Headless echo-loop verification for Phase 0.

Mirrors jiuwenclaw/gateway/agent_client.py:802-846 self-verification: spins up
the AgentServer in-process, connects an AgentClient, and asserts both a
streaming echo (N chunks + final) and a unary echo.

Run: `pytest tests/test_echo.py`
"""
import asyncio

import pytest
from websockets.asyncio.server import serve

from twinkle.agentserver.server import handler
from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.agent_client import AgentClient


def _free_port() -> int:
    """Grab an OS-assigned free TCP port (no pytest-asyncio fixture needed)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_echo_stream_and_unary() -> None:
    port = _free_port()
    async def run() -> None:
        server = await serve(handler, "127.0.0.1", port)
        try:
            client = AgentClient(f"ws://127.0.0.1:{port}")
            await client.connect()
            assert client.ready

            # --- streaming echo ---
            env = E2AEnvelope(
                request_id="r1",
                method="chat.send",
                params={"query": "hi"},
                is_stream=True,
            )
            chunks = [c async for c in client.send_request_stream(env)]
            streamed = "".join(
                (c.body.get("result", {}).get("content", ""))
                for c in chunks
                if not c.is_final
            )
            assert streamed == "Echo: hi"
            final = chunks[-1]
            assert final.is_final
            assert final.response_kind == "e2a.complete"
            assert final.status == "succeeded"

            # --- unary echo ---
            env2 = E2AEnvelope(
                request_id="r2",
                method="chat.send",
                params={"query": "yo"},
                is_stream=False,
            )
            resp = await client.send_request(env2)
            assert resp.body["result"]["content"] == "Echo: yo"
            assert resp.is_final

            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_malformed_envelope_returns_error(free_port: int) -> None:
    port = free_port
    async def run() -> None:
        import json

        server = await serve(handler, "127.0.0.1", port)
        try:
            from websockets.asyncio.client import connect

            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # consume connection.ack
                await ws.send("not-json-at-all")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
