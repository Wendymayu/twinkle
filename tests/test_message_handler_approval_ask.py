# tests/test_message_handler_approval_ask.py
import asyncio

from twinkle.gateway.message_handler import MessageHandler
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType, Message


class _FakeAgentClient:
    def __init__(self, frames):
        self._frames = frames
    async def send_request_stream(self, envelope):
        for f in self._frames:
            yield f


def test_e2a_ask_mapped_to_approval_ask_event():
    ac = _FakeAgentClient([
        E2AResponse(request_id="R", sequence=0, is_final=False, status="in_progress",
                    response_kind="e2a.ask",
                    body={"approval_id": "a1", "tool": "echo", "args": {},
                          "tool_call_id": "c1", "reason": "x"}),
        E2AResponse(request_id="R", sequence=1, is_final=True, status="succeeded",
                    response_kind="e2a.complete", body={"result": {"content": "done"}}),
    ])
    mh = MessageHandler(ac)
    msg = Message(id="R", type="req", channel_id="web", session_id="s1",
                  method="chat.send", params={"query": "hi"})
    envelope = E2AEnvelope(request_id="R", channel="web", session_id="s1",
                           method="chat.send", params={"query": "hi"})

    async def go():
        # drive _process_stream directly — handle_message is fire-and-forget create_task,
        # which asyncio.run cancels before it processes frames
        await mh._process_stream(envelope, msg)
        out = []
        for _ in range(2):
            out.append(await mh.dequeue_outbound())
        return out

    out = asyncio.run(go())
    assert out[0].event_type == EventType.APPROVAL_ASK
    assert out[0].payload["approval_id"] == "a1"
    assert out[1].event_type == EventType.CHAT_FINAL
