import asyncio

from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.gateway.message_handler import MessageHandler
from twinkle.schema.message import EventType, Message


class _FakeAgentClient:
    """Yields a scripted list of E2AResponse frames for one request."""

    def __init__(self, frames):
        self._frames = frames

    async def send_request_stream(self, envelope: E2AEnvelope):
        for f in self._frames:
            yield f


def test_todo_update_frame_becomes_todo_event() -> None:
    todo_body = {
        "tasks": [{"idx": 1, "title": "a", "status": "waiting", "result": ""}],
        "remaining": 1,
        "total": 1,
    }
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=False,
            status="in_progress", response_kind="e2a.todo_update", body=todo_body,
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="chat.send", params={"query": "q"})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.TODO_UPDATE
    assert out.payload == todo_body
    assert out.content == ""


def test_chunk_frame_still_becomes_chat_delta() -> None:
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=False,
            status="in_progress", response_kind="e2a.chunk",
            body={"result": {"content": "hi"}},
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="chat.send", params={"query": "q"})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.CHAT_DELTA
    assert out.content == "hi"


def test_result_frame_becomes_result_event() -> None:
    body = {"type": "session.list", "sessions": [{"session_id": "s1", "title": "t"}]}
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=True,
            status="succeeded", response_kind="e2a.result", body=body,
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="session.list", params={})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.RESULT
    assert out.payload == body
    assert out.content == ""
