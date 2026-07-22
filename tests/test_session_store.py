import asyncio
import json
from pathlib import Path

from twinkle.agentserver.session_store import SessionStore


def _run(coro):
    return asyncio.run(coro)


def test_append_and_get_round_trip(session_store):
    _run(session_store.append("s1", {"role": "user", "content": "hi"}))
    _run(session_store.append("s1", {"role": "assistant", "content": "hello"}))
    msgs = session_store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_sessions_are_isolated(session_store):
    _run(session_store.append("s1", {"role": "user", "content": "a"}))
    _run(session_store.append("s2", {"role": "user", "content": "b"}))
    assert [m["content"] for m in session_store.get_messages("s1")] == ["a"]
    assert [m["content"] for m in session_store.get_messages("s2")] == ["b"]


def test_unknown_session_returns_empty(session_store):
    assert session_store.get_messages("never") == []


def test_create_session_writes_metadata(session_store, sessions_dir):
    meta = _run(session_store.create_session("s1"))
    mpath = Path(sessions_dir) / "s1" / "metadata.json"
    assert mpath.is_file()
    on_disk = json.loads(mpath.read_text(encoding="utf-8"))
    assert on_disk["session_id"] == "s1"
    assert on_disk["title"] == ""
    assert on_disk["message_count"] == 0
    assert meta["session_id"] == "s1"


def test_create_session_is_idempotent(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    # second call must not error or reset an existing populated metadata
    _run(session_store.create_session("s1"))
    on_disk = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert on_disk["message_count"] == 0


def test_append_writes_history_line_and_updates_metadata(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hello"},
                              request_id="r1"))
    hpath = Path(sessions_dir) / "s1" / "history.json"
    lines = [json.loads(l) for l in hpath.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["role"] == "user"
    assert lines[0]["content"] == "hello"
    assert lines[0]["request_id"] == "r1"
    assert lines[0]["session_id"] == "s1"
    meta = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert meta["message_count"] == 1
    assert meta["last_message_at"] >= meta["created_at"]


def test_first_user_message_auto_titles(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    long_msg = "x" * 80
    _run(session_store.append("s1", {"role": "user", "content": long_msg},
                              request_id="r1"))
    meta = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert meta["title"].startswith("x" * 50)
    assert meta["title"].endswith("...")


def test_append_preserves_tool_calls_for_react(session_store):
    _run(session_store.create_session("s1"))
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]
    _run(session_store.append("s1", {"role": "assistant", "content": None,
                                    "tool_calls": tc}, request_id="r1"))
    _run(session_store.append("s1", {"role": "tool", "tool_call_id": "c1",
                                    "content": "tool-saw:hi"}, request_id="r1"))
    msgs = session_store.get_messages("s1")
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["tool_calls"] == tc
    assert msgs[-1]["role"] == "tool"
    assert msgs[-1]["tool_call_id"] == "c1"


def test_cold_start_hydrates_full_history(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "system", "content": "sys"}))
    _run(session_store.append("s1", {"role": "user", "content": "q"},
                              request_id="r1"))
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "echo", "arguments": '{}'}}]
    _run(session_store.append("s1", {"role": "assistant", "content": None,
                                      "tool_calls": tc}, request_id="r1"))
    _run(session_store.append("s1", {"role": "tool", "tool_call_id": "c1",
                                    "content": "res"}, request_id="r1"))

    # Brand-new store instance pointing at the SAME dir — cache is cold.
    cold = SessionStore(str(sessions_dir))
    msgs = cold.get_messages("s1")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["tool_calls"] == tc
    assert msgs[3]["tool_call_id"] == "c1"
    assert msgs[3]["content"] == "res"
