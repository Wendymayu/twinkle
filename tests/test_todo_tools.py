# tests/test_todo_tools.py
import asyncio

import pytest
from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    _set_todo_store,
    flush_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.todo.store import TodoStore
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.todo_tools import todo_complete, todo_create, todo_list


@pytest.fixture(autouse=True)
def _isolated_todo_store(tmp_path):
    """Each test gets a tmp-backed todo singleton so the tools' get_todo_store()
    never writes to the real ~/.twinkle."""
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    yield
    _set_todo_store(None)


def _set_session_id(session_id: str) -> None:
    PLAN_TODO_SESSION_ID.set(session_id)


def test_create_returns_markdown_with_tasks() -> None:
    _set_session_id("tools-1")

    async def run():
        return await todo_create.invoke({"tasks": ["alpha", "beta"]})

    out = asyncio.run(run())
    assert "Created 2 todo tasks." in out
    assert "alpha" in out and "beta" in out
    assert "[ ]" in out  # waiting checkbox


def test_complete_marks_and_lists() -> None:
    _set_session_id("tools-2")
    asyncio.run(todo_create.invoke({"tasks": ["x", "y"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 1, "result": "ok"}))
    assert "Task 1 marked as completed." in out
    assert "[x]" in out  # completed checkbox
    assert "ok" in out


def test_create_twice_returns_error_with_current_list() -> None:
    _set_session_id("tools-3")
    asyncio.run(todo_create.invoke({"tasks": ["first"]}))
    out = asyncio.run(todo_create.invoke({"tasks": ["second"]}))
    assert "Error:" in out
    assert "in progress" in out
    assert "first" in out  # current list appended
    assert "second" not in out


def test_complete_unknown_idx_error() -> None:
    _set_session_id("tools-4")
    asyncio.run(todo_create.invoke({"tasks": ["a"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 9}))
    assert "Error:" in out
    assert "not found" in out


def test_list_empty_session() -> None:
    _set_session_id("tools-5-empty")

    async def run():
        return await todo_list.invoke({})

    out = asyncio.run(run())
    assert "No todo tasks." in out


def test_sessions_isolated_via_contextvar() -> None:
    _set_session_id("iso-A")
    asyncio.run(todo_create.invoke({"tasks": ["A-task"]}))
    _set_session_id("iso-B")
    asyncio.run(todo_create.invoke({"tasks": ["B-task"]}))
    _set_session_id("iso-A")
    out = asyncio.run(todo_list.invoke({}))
    assert "A-task" in out
    assert "B-task" not in out


def test_schemas_registered_in_tool_manager() -> None:
    m = tool_manager()
    names = {t.card.name for t in m.list()}
    assert {"todo_create", "todo_complete", "todo_list"} <= names
    schemas = {s["function"]["name"]: s for s in m.schemas()}
    assert schemas["todo_create"]["function"]["parameters"]["properties"]["tasks"][
        "type"
    ] == "array"
    assert "tasks" in schemas["todo_create"]["function"]["parameters"]["required"]
    # idx required for complete; result optional (has default)
    complete_req = schemas["todo_complete"]["function"]["parameters"]["required"]
    assert "idx" in complete_req
    assert "result" not in complete_req


def test_create_publishes_snapshot() -> None:
    _set_session_id("pub-1")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["a", "b"]}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 2
    assert [t["idx"] for t in snap["tasks"]] == [1, 2]
    assert all(t["status"] == "waiting" for t in snap["tasks"])
    assert snap["tasks"][0]["title"] == "a"


def test_complete_publishes_snapshot() -> None:
    _set_session_id("pub-2")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["x", "y"]}))
    flush_todo_events()  # clear create's snapshot
    asyncio.run(todo_complete.invoke({"idx": 1, "result": "ok"}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 1
    assert snap["tasks"][0]["status"] == "completed"
    assert snap["tasks"][0]["result"] == "ok"


def test_list_does_not_publish() -> None:
    _set_session_id("pub-3")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["a"]}))
    flush_todo_events()
    asyncio.run(todo_list.invoke({}))
    assert flush_todo_events() == []


def test_error_path_does_not_publish() -> None:
    _set_session_id("pub-4")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["first"]}))
    flush_todo_events()
    # second create fails (already exists) — must NOT publish
    asyncio.run(todo_create.invoke({"tasks": ["second"]}))
    assert flush_todo_events() == []
