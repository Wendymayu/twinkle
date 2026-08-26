import contextvars

from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    flush_todo_events,
    get_plan_todo_session_id,
    append_todo_event,
    reset_todo_events,
)


def test_default_is_default_string() -> None:
    # 此全新 context 未设置 token -> "default"。
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
    # flush 清空了缓冲区
    assert flush_todo_events() == []


def test_append_without_reset_is_noop() -> None:
    # 在真正全新（空）的 context 中，TODO_EVENTS 是其默认值（None）；
    # append 不得抛异常，也不得改动任何共享状态。
    # NOTE：contextvars.Context()（空）—— 而非 copy_context()，后者会把绑定
    # 指向同一个可变 list 对象，从而泄漏修改。
    def body():
        append_todo_event({"tasks": [], "remaining": 0, "total": 0})

    contextvars.Context().run(body)


def test_flush_without_reset_returns_empty() -> None:
    def body():
        assert flush_todo_events() == []

    contextvars.Context().run(body)
