from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    get_plan_todo_session_id,
)


def test_default_is_default_string() -> None:
    # No token set in this fresh context -> "default".
    PLAN_TODO_SESSION_ID.set(None)
    assert get_plan_todo_session_id() == "default"


def test_returns_set_session_id() -> None:
    PLAN_TODO_SESSION_ID.set("sess-abc")
    assert get_plan_todo_session_id() == "sess-abc"
