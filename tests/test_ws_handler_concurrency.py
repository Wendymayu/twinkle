"""ws_handler 并发化:挂起的 run_stream 期间能收 approval.respond 并恢复。
用 free_port 起真 ws server + 一个会 yield e2a.ask 然后挂起的假 loop。
"""
import asyncio
import json
import uuid

from websockets.asyncio.server import serve
from websockets.asyncio.client import connect

from twinkle.agentserver.server import ws_handler
from twinkle.e2a.models import E2AEnvelope, E2AResponse


class _SuspendingLoop:
    """Yields e2a.ask, awaits the registry Future, then yields e2a.complete."""
    def __init__(self):
        self.envelopes = []
    async def run_stream(self, envelope):
        from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY
        self.envelopes.append(envelope)
        aid = str(uuid.uuid4())
        fut = APPROVAL_REGISTRY.register(aid)
        yield E2AResponse(request_id=envelope.request_id, sequence=0, is_final=False,
                          status="in_progress", response_kind="e2a.ask",
                          body={"approval_id": aid, "tool": "echo", "args": {},
                                "tool_call_id": "c1", "reason": "require-approval"})
        decision = await fut
        yield E2AResponse(request_id=envelope.request_id, sequence=1, is_final=True,
                          status="succeeded", response_kind="e2a.complete",
                          body={"result": {"content": f"approved:{decision}"}})


class _FakeStore: ...


def test_approval_respond_resumes_suspended_stream(free_port):
    async def scenario():
        loop = _SuspendingLoop()
        handler = ws_handler(loop, _FakeStore())
        srv = await serve(handler, "127.0.0.1", free_port)
        try:
            uri = f"ws://127.0.0.1:{free_port}"
            async with connect(uri) as ws:
                await ws.recv()  # connection.ack first
                # 1. send chat.send
                await ws.send(json.dumps({
                    "protocol_version": "1.0", "request_id": "R", "channel": "web",
                    "session_id": "s1", "method": "chat.send",
                    "params": {"query": "hi"}, "timestamp": 0.0}))
                # 2. expect e2a.ask (is_final=false)
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert frame["response_kind"] == "e2a.ask"
                aid = frame["body"]["approval_id"]
                # 3. send approval.respond (R2) while R is suspended
                await ws.send(json.dumps({
                    "protocol_version": "1.0", "request_id": "R2", "channel": "web",
                    "session_id": "s1", "method": "approval.respond",
                    "params": {"approval_id": aid, "decision": "allow",
                               "original_request_id": "R"}, "timestamp": 0.0}))
                # 4. expect ack (R2, e2a.result) then resumed complete (R)
                frames = []
                while len(frames) < 2:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    frames.append(json.loads(raw))
                ack = next(f for f in frames if f["request_id"] == "R2")
                comp = next(f for f in frames if f["request_id"] == "R")
                assert ack["response_kind"] == "e2a.result" and ack["body"]["accepted"] is True
                assert comp["response_kind"] == "e2a.complete"
                assert comp["body"]["result"]["content"] == "approved:allow"
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
