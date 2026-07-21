import contextvars

from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    get_plan_todo_session_id,
    publish_todo_update,
    reset_todo_events,
)


def test_default_is_default_string() -> None:
    # No token set in this fresh context -> "default".
    PLAN_TODO_SESSION_ID.set(None)
    assert get_plan_todo_session_id() == "default"


def test_returns_set_session_id() -> None:
    PLAN_TODO_SESSION_ID.set("sess-abc")
    assert get_plan_todo_session_id() == "sess-abc"


def test_publish_then_drain() -> None:
    reset_todo_events()
    publish_todo_update({"tasks": [], "remaining": 0, "total": 0})
    publish_todo_update({"tasks": [{"idx": 1}], "remaining": 1, "total": 1})
    drained = drain_todo_events()
    assert len(drained) == 2
    assert drained[0]["total"] == 0
    assert drained[1]["remaining"] == 1
    # drain cleared the bus
    assert drain_todo_events() == []


def test_publish_without_reset_is_noop() -> None:
    # In a truly fresh (empty) context, TODO_EVENTS is its default (None);
    # publish must not raise and must not mutate any shared state.
    # NOTE: contextvars.Context() (empty) — NOT copy_context(), which copies
    # the binding to the same mutable list object and would leak mutations.
    def body():
        publish_todo_update({"tasks": [], "remaining": 0, "total": 0})

    contextvars.Context().run(body)


def test_drain_without_reset_returns_empty() -> None:
    def body():
        assert drain_todo_events() == []

    contextvars.Context().run(body)
