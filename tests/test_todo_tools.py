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
from twinkle.agentserver.tools.builtin.todo_tools import todo_create, todo_update, todo_list, todo_get


@pytest.fixture(autouse=True)
def _isolated_todo_store(tmp_path):
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    yield
    _set_todo_store(None)


def _set_session_id(session_id: str) -> None:
    PLAN_TODO_SESSION_ID.set(session_id)


def test_create_returns_markdown_with_tasks() -> None:
    _set_session_id("tools-1")

    async def run():
        return await todo_create.invoke({"subjects": ["alpha", "beta"]})

    out = asyncio.run(run())
    assert "Created 2 todo tasks." in out
    assert "alpha" in out and "beta" in out
    assert "[ ]" in out


def test_create_sequential() -> None:
    _set_session_id("tools-seq")

    async def run():
        return await todo_create.invoke({"subjects": ["step1", "step2"], "sequential": True})

    out = asyncio.run(run())
    assert "Created 2 todo tasks (sequential)." in out
    assert "blocked by" in out


def test_update_marks_completed() -> None:
    _set_session_id("tools-2")
    tasks = asyncio.run(todo_create.invoke({"subjects": ["x", "y"]}))
    # Extract a task id from the store
    store = asyncio.run(todo_list.invoke({}))
    # Get the actual task id via the store
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-2"))
    task_id = all_tasks[0].id
    out = asyncio.run(todo_update.invoke({"task_id": task_id, "status": "completed", "result": "ok"}))
    assert "Updated task" in out
    assert "[x]" in out


def test_update_with_warning() -> None:
    _set_session_id("tools-warn")
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"], "sequential": True}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-warn"))
    blocked_id = all_tasks[1].id
    out = asyncio.run(todo_update.invoke({"task_id": blocked_id, "status": "in_progress"}))
    assert "Warning" in out


def test_create_twice_returns_error() -> None:
    _set_session_id("tools-3")
    asyncio.run(todo_create.invoke({"subjects": ["first"]}))
    out = asyncio.run(todo_create.invoke({"subjects": ["second"]}))
    assert "Error:" in out
    assert "already exists" in out


def test_update_unknown_task_error() -> None:
    _set_session_id("tools-4")
    asyncio.run(todo_create.invoke({"subjects": ["a"]}))
    out = asyncio.run(todo_update.invoke({"task_id": "nonexistent", "status": "completed"}))
    assert "Error:" in out
    assert "not found" in out


def test_list_empty_session() -> None:
    _set_session_id("tools-5-empty")

    async def run():
        return await todo_list.invoke({})

    out = asyncio.run(run())
    assert "No todo tasks." in out


def test_list_with_status_filter() -> None:
    _set_session_id("tools-filter")
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"]}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-filter"))
    asyncio.run(todo_update.invoke({"task_id": all_tasks[0].id, "status": "completed"}))
    out = asyncio.run(todo_list.invoke({"status": "completed"}))
    assert "a" in out
    assert "b" not in out


def test_get_found() -> None:
    _set_session_id("tools-get")
    asyncio.run(todo_create.invoke({"subjects": ["findme"]}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-get"))
    out = asyncio.run(todo_get.invoke({"task_id": all_tasks[0].id}))
    assert "findme" in out


def test_get_not_found() -> None:
    _set_session_id("tools-getnf")
    out = asyncio.run(todo_get.invoke({"task_id": "nonexistent"}))
    assert "not found" in out


def test_sessions_isolated_via_contextvar() -> None:
    _set_session_id("iso-A")
    asyncio.run(todo_create.invoke({"subjects": ["A-task"]}))
    _set_session_id("iso-B")
    asyncio.run(todo_create.invoke({"subjects": ["B-task"]}))
    _set_session_id("iso-A")
    out = asyncio.run(todo_list.invoke({}))
    assert "A-task" in out
    assert "B-task" not in out


def test_schemas_registered_in_tool_manager() -> None:
    m = tool_manager()
    names = {t.card.name for t in m.list()}
    assert {"todo_create", "todo_update", "todo_list", "todo_get"} <= names
    schemas = {s["function"]["name"]: s for s in m.schemas()}
    assert "subjects" in schemas["todo_create"]["function"]["parameters"]["properties"]
    assert "sequential" in schemas["todo_create"]["function"]["parameters"]["properties"]
    assert "task_id" in schemas["todo_update"]["function"]["parameters"]["required"]


def test_create_publishes_snapshot() -> None:
    _set_session_id("pub-1")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"]}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 2
    assert all(t["status"] == "pending" for t in snap["tasks"])
    assert snap["tasks"][0]["subject"] == "a"
    assert "id" in snap["tasks"][0]
    assert "blocked_by" in snap["tasks"][0]


def test_update_publishes_snapshot() -> None:
    _set_session_id("pub-2")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["x", "y"]}))
    flush_todo_events()
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("pub-2"))
    asyncio.run(todo_update.invoke({"task_id": all_tasks[0].id, "status": "completed", "result": "ok"}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 1
    assert snap["tasks"][0]["status"] == "completed"


def test_list_does_not_publish() -> None:
    _set_session_id("pub-3")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["a"]}))
    flush_todo_events()
    asyncio.run(todo_list.invoke({}))
    assert flush_todo_events() == []


def test_error_path_does_not_publish() -> None:
    _set_session_id("pub-4")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["first"]}))
    flush_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["second"]}))
    assert flush_todo_events() == []
