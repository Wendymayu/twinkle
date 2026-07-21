# tests/test_todo_tools.py
import asyncio

from twinkle.agentserver.plan_todo_context import PLAN_TODO_SESSION_ID
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.todo_tools import todo_complete, todo_create, todo_list


def _set_sid(sid: str) -> None:
    PLAN_TODO_SESSION_ID.set(sid)


def test_create_returns_markdown_with_tasks() -> None:
    _set_sid("tools-1")

    async def run():
        return await todo_create.invoke({"tasks": ["alpha", "beta"]})

    out = asyncio.run(run())
    assert "Created 2 todo tasks." in out
    assert "alpha" in out and "beta" in out
    assert "[ ]" in out  # waiting checkbox


def test_complete_marks_and_lists() -> None:
    _set_sid("tools-2")
    asyncio.run(todo_create.invoke({"tasks": ["x", "y"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 1, "result": "ok"}))
    assert "Task 1 marked as completed." in out
    assert "[x]" in out  # completed checkbox
    assert "ok" in out


def test_create_twice_returns_error_with_current_list() -> None:
    _set_sid("tools-3")
    asyncio.run(todo_create.invoke({"tasks": ["first"]}))
    out = asyncio.run(todo_create.invoke({"tasks": ["second"]}))
    assert "Error:" in out
    assert "already exists" in out
    assert "first" in out  # current list appended
    assert "second" not in out


def test_complete_unknown_idx_error() -> None:
    _set_sid("tools-4")
    asyncio.run(todo_create.invoke({"tasks": ["a"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 9}))
    assert "Error:" in out
    assert "not found" in out


def test_list_empty_session() -> None:
    _set_sid("tools-5-empty")

    async def run():
        return await todo_list.invoke({})

    out = asyncio.run(run())
    assert "No todo tasks." in out


def test_sessions_isolated_via_contextvar() -> None:
    _set_sid("iso-A")
    asyncio.run(todo_create.invoke({"tasks": ["A-task"]}))
    _set_sid("iso-B")
    asyncio.run(todo_create.invoke({"tasks": ["B-task"]}))
    _set_sid("iso-A")
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
