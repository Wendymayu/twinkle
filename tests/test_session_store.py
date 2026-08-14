import asyncio
import json
from pathlib import Path

from twinkle.agentserver.sessions import SessionStore


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


def test_list_sessions_sorted_desc(session_store, sessions_dir):
    _run(session_store.create_session("old"))
    _run(session_store.append("old", {"role": "user", "content": "a"},
                              request_id="r1"))
    # tiny sleep-free ordering: old was created first -> lower last_message_at
    _run(session_store.create_session("new"))
    _run(session_store.append("new", {"role": "user", "content": "b"},
                              request_id="r2"))
    rows = session_store.list_sessions()
    assert [r["session_id"] for r in rows] == ["new", "old"]


def test_list_sessions_falls_back_on_corrupt_metadata(session_store, sessions_dir):
    sdir = Path(sessions_dir) / "broken"
    sdir.mkdir()
    (sdir / "metadata.json").write_text("{not valid json", encoding="utf-8")
    rows = session_store.list_sessions()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "broken"
    assert rows[0]["title"] == "(无标题)"


def test_delete_session_removes_dir_and_evicts_cache(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}))
    assert _run(session_store.delete_session("s1")) is True
    assert not (Path(sessions_dir) / "s1").exists()
    # cache evicted -> cold read returns empty
    assert session_store.get_messages("s1") == []
    # deleting again -> False (absent)
    assert _run(session_store.delete_session("s1")) is False


def test_get_history_skips_corrupt_lines(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    hpath = Path(sessions_dir) / "s1" / "history.json"
    hpath.write_text(
        json.dumps({"role": "user", "content": "good"}) + "\n"
        + "{bad line\n"
        + json.dumps({"role": "assistant", "content": "ok"}) + "\n",
        encoding="utf-8",
    )
    rows = session_store.get_history("s1")
    assert [r["content"] for r in rows] == ["good", "ok"]


def test_list_files_lists_session_files(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}, request_id="r1"))
    files = session_store.list_files("s1")
    names = {f["name"] for f in files}
    assert "metadata.json" in names
    assert "history.json" in names
    for f in files:
        assert f["is_dir"] is False
        assert f["size"] >= 0


def test_list_files_unknown_session_returns_empty(session_store):
    assert session_store.list_files("never") == []


def test_read_file_returns_metadata_json(session_store):
    _run(session_store.create_session("s1"))
    import json as _json
    content = session_store.read_file("s1", "metadata.json")
    meta = _json.loads(content)
    assert meta["session_id"] == "s1"
    assert meta["message_count"] == 0


def test_read_file_returns_history_jsonl(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}, request_id="r1"))
    content = session_store.read_file("s1", "history.json")
    import json as _json
    lines = [_json.loads(l) for l in content.splitlines() if l.strip()]
    assert lines[0]["role"] == "user"
    assert lines[0]["content"] == "hi"


def test_read_file_rejects_path_traversal(session_store):
    _run(session_store.create_session("s1"))
    for bad in ["../etc/passwd", "a/b", "..", ".", "a\\b", ""]:
        try:
            session_store.read_file("s1", bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_read_file_missing_raises_filenotfound(session_store):
    _run(session_store.create_session("s1"))
    try:
        session_store.read_file("s1", "nope.json")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_message_count_excludes_system_role(session_store, sessions_dir):
    """Reproduces issue #5: the base system prompt inflates message_count.

    A plain user->assistant turn (no tools) writes 3 history records
    (system + user + assistant), but only the user/assistant pair is a
    user-visible conversation message — the frontend filters `system` out
    in fromHistory. message_count must match that visible count, so the
    system-role prompt must NOT be counted.
    """
    _run(session_store.append("s1", {"role": "system", "content": "sys"}))
    _run(session_store.append("s1", {"role": "user", "content": "你好"},
                              request_id="r1"))
    _run(session_store.append("s1", {"role": "assistant", "content": "你好!有什么我可以帮你的吗?"},
                              request_id="r1"))
    meta = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert meta["message_count"] == 2  # not 3 — system prompt excluded


def test_list_sessions_recomputes_count_excluding_system(session_store, sessions_dir):
    """Legacy sessions on disk carry an inflated message_count (the system
    prompt used to be counted). list_sessions must derive the visible count
    from history.json so existing sessions display correctly instead of the
    stale stored value."""
    _run(session_store.append("s1", {"role": "system", "content": "sys"}))
    _run(session_store.append("s1", {"role": "user", "content": "q"}, request_id="r1"))
    _run(session_store.append("s1", {"role": "assistant", "content": "a"}, request_id="r1"))
    # corrupt the stored count to a clearly-wrong legacy value
    mpath = Path(sessions_dir) / "s1" / "metadata.json"
    meta = json.loads(mpath.read_text(encoding="utf-8"))
    meta["message_count"] = 99
    mpath.write_text(json.dumps(meta), encoding="utf-8")
    rows = session_store.list_sessions()
    row = next(r for r in rows if r["session_id"] == "s1")
    assert row["message_count"] == 2  # recomputed from history, system excluded


def test_list_sessions_count_falls_back_when_history_missing(session_store, sessions_dir):
    """A session with metadata but no history.json (e.g. just created, no
    messages yet) must not error — list_sessions falls back to the stored
    count."""
    _run(session_store.create_session("s1"))
    rows = session_store.list_sessions()
    row = next(r for r in rows if r["session_id"] == "s1")
    assert row["message_count"] == 0


def test_list_sessions_hides_subagent_sessions(session_store):
    """Child sessions (<parent>__sub_<id>) are hidden by default; visible with include_subagents."""
    _run(session_store.create_session("real1"))
    _run(session_store.append("real1", {"role": "user", "content": "a"}, request_id="r1"))
    _run(session_store.create_session("p1__sub_abc12345"))
    _run(session_store.append("p1__sub_abc12345", {"role": "user", "content": "child"},
                              request_id="r2"))
    default_rows = session_store.list_sessions()
    default_ids = {r["session_id"] for r in default_rows}
    assert "real1" in default_ids
    assert "p1__sub_abc12345" not in default_ids
    all_rows = session_store.list_sessions(include_subagents=True)
    all_ids = {r["session_id"] for r in all_rows}
    assert "p1__sub_abc12345" in all_ids


def test_append_preserves_reasoning_but_does_not_feed_back(session_store, sessions_dir):
    """reasoning is persisted on the assistant history record (for display /
    evolution / debugging) but ``_record_to_openai`` drops it so it is never
    fed back to the model — thinking is regenerated each turn, never replayed
    (OpenAI reasoning convention)."""
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "q"}, request_id="r1"))
    _run(session_store.append("s1", {"role": "assistant", "content": "a",
                                    "reasoning": "我的思考"}, request_id="r1"))
    # persisted on the history record
    hpath = Path(sessions_dir) / "s1" / "history.json"
    recs = [json.loads(l) for l in hpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    asst = [r for r in recs if r["role"] == "assistant"][0]
    assert asst.get("reasoning") == "我的思考"
    # NOT fed back to the model — get_messages reconstructs OpenAI messages only
    msgs = session_store.get_messages("s1")
    assert "reasoning" not in msgs[-1]
    assert msgs[-1] == {"role": "assistant", "content": "a"}
