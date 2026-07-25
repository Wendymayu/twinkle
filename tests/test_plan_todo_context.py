import contextvars

from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    flush_todo_events,
    get_plan_todo_session_id,
    append_todo_event,
    reset_todo_events,
)


def test_default_is_default_string() -> None:
    # No token set in this fresh context -> "default".
    PLAN_TODO_SESSION_ID.set(None)
    assert get_plan_todo_session_id() == "default"


def test_returns_set_session_id() -> None:
    PLAN_TODO_SESSION_ID.set("sess-abc")
    assert get_plan_todo_session_id() == "sess-abc"


def test_append_then_flush() -> None:
    reset_todo_events()
    append_todo_event({"tasks": [], "remaining": 0, "total": 0})
    append_todo_event({"tasks": [{"idx": 1}], "remaining": 1, "total": 1})
    flushed = flush_todo_events()
    assert len(flushed) == 2
    assert flushed[0]["total"] == 0
    assert flushed[1]["remaining"] == 1
    # flush cleared the buffer
    assert flush_todo_events() == []


def test_append_without_reset_is_noop() -> None:
    # In a truly fresh (empty) context, TODO_EVENTS is its default (None);
    # append must not raise and must not mutate any shared state.
    # NOTE: contextvars.Context() (empty) — NOT copy_context(), which copies
    # the binding to the same mutable list object and would leak mutations.
    def body():
        append_todo_event({"tasks": [], "remaining": 0, "total": 0})

    contextvars.Context().run(body)


def test_flush_without_reset_returns_empty() -> None:
    def body():
        assert flush_todo_events() == []

    contextvars.Context().run(body)
