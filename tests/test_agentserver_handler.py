"""Handler-level tests: malformed envelope still errors; a valid envelope
reaches the injected loop. Replaces tests/test_echo.py (echo removed)."""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.server import make_handler
from twinkle.e2a.models import E2AEnvelope, E2AResponse


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RecordingLoop:
    """Records the env it received and streams back one canned frame."""
    def __init__(self):
        self.seen = None

    async def run_stream(self, env):
        self.seen = env
        yield E2AResponse(
            request_id=env.request_id,
            sequence=0,
            is_final=True,
            status="succeeded",
            response_kind="e2a.complete",
            body={"result": {"content": "ok"}},
        )


def test_malformed_envelope_returns_error() -> None:
    port = _free_port()
    loop_obj = _RecordingLoop()

    async def run() -> None:
        server = await serve(make_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                await ws.send("not-json-at-all")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_valid_envelope_dispatches_to_loop() -> None:
    port = _free_port()
    loop_obj = _RecordingLoop()

    async def run() -> None:
        server = await serve(make_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                env = E2AEnvelope(
                    request_id="r1", session_id="s1", method="chat.send",
                    params={"query": "hi"},
                )
                await ws.send(env.model_dump_json())
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.complete"
                assert data["body"]["result"]["content"] == "ok"
            assert loop_obj.seen is not None
            assert loop_obj.seen.session_id == "s1"
            assert loop_obj.seen.params["query"] == "hi"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
