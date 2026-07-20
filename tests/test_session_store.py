from twinkle.agentserver.session_store import SessionStore


def test_append_and_get_round_trip() -> None:
    store = SessionStore()
    store.append("s1", {"role": "user", "content": "hi"})
    store.append("s1", {"role": "assistant", "content": "hello"})
    msgs = store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_sessions_are_isolated() -> None:
    store = SessionStore()
    store.append("s1", {"role": "user", "content": "a"})
    store.append("s2", {"role": "user", "content": "b"})
    assert [m["content"] for m in store.get_messages("s1")] == ["a"]
    assert [m["content"] for m in store.get_messages("s2")] == ["b"]


def test_unknown_session_returns_empty() -> None:
    assert SessionStore().get_messages("never") == []
