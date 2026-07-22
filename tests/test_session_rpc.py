import asyncio

import pytest

from twinkle.agentserver.session_rpc import dispatch_session_rpc
from twinkle.e2a.models import E2AEnvelope


def _env(method, rid="r1", session_id="s1", params=None):
    return E2AEnvelope(
        request_id=rid, session_id=session_id,
        method=method, params=params or {},
    )


def _run(coro):
    return asyncio.run(coro)


async def _frames(envelope, store):
    return [f async for f in dispatch_session_rpc(envelope, store)]


def test_session_list_returns_result_frame(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hello"},
                              request_id="r1"))
    frames = _run(_frames(_env("session.list"), session_store))
    assert len(frames) == 1
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.is_final is True
    assert f.status == "succeeded"
    assert f.request_id == "r1"
    assert f.body["type"] == "session.list"
    assert [s["session_id"] for s in f.body["sessions"]] == ["s1"]
    assert f.body["sessions"][0]["title"] == "hello"


def test_session_create_returns_result_frame(session_store, sessions_dir):
    frames = _run(_frames(
        _env("session.create", session_id="s-new", params={"session_id": "s-new"}),
        session_store,
    ))
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.body["type"] == "session.create"
    assert f.body["session_id"] == "s-new"
    assert (sessions_dir / "s-new" / "metadata.json").is_file()


def test_history_get_returns_messages(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"},
                              request_id="r1"))
    _run(session_store.append("s1", {"role": "assistant", "content": "yo"},
                              request_id="r1"))
    frames = _run(_frames(
        _env("history.get", session_id="s1"), session_store,
    ))
    f = frames[0]
    assert f.body["type"] == "history.get"
    roles = [m["role"] for m in f.body["messages"]]
    assert roles == ["user", "assistant"]
    assert f.body["messages"][0]["content"] == "hi"


def test_session_delete_removes_and_returns_result(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    frames = _run(_frames(
        _env("session.delete", session_id="s1"), session_store,
    ))
    f = frames[0]
    assert f.body["type"] == "session.delete"
    assert f.body["session_id"] == "s1"
    assert not (sessions_dir / "s1").exists()


def test_unknown_session_method_returns_no_frames(session_store):
    # dispatch_session_rpc only handles session.*/history.get; an unknown
    # method yields nothing (the caller — server.py — falls through to the
    # AgentLoop for chat.send). We assert it yields no frames for a method
    # it does not own.
    frames = _run(_frames(_env("chat.send"), session_store))
    assert frames == []


def test_history_get_unknown_session_returns_empty(session_store):
    frames = _run(_frames(_env("history.get", session_id="nope"), session_store))
    f = frames[0]
    assert f.body["messages"] == []
