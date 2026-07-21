# tests/test_todo_store.py
import asyncio

import pytest

from twinkle.agentserver.todo_store import TodoError, TodoStore


def test_create_then_list() -> None:
    store = TodoStore()

    async def run():
        created = await store.create("s1", ["a", "b"])
        listed = await store.list_tasks("s1")
        return created, listed

    created, listed = asyncio.run(run())
    assert [t.idx for t in created] == [1, 2]
    assert [t.title for t in created] == ["a", "b"]
    assert all(t.status == "waiting" for t in created)
    assert listed == created


def test_create_empty_raises() -> None:
    store = TodoStore()
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(store.create("s1", []))


def test_create_twice_raises_already_exists() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="already exists"):
        asyncio.run(store.create("s1", ["b"]))


def test_complete_marks_status_and_result() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a", "b"]))
    tasks = asyncio.run(store.complete("s1", 1, result="done A"))
    assert tasks[0].status == "completed"
    assert tasks[0].result == "done A"
    assert tasks[1].status == "waiting"


def test_complete_unknown_idx_raises() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(store.complete("s1", 99))


def test_complete_already_completed_raises() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    asyncio.run(store.complete("s1", 1))
    with pytest.raises(TodoError, match="already completed"):
        asyncio.run(store.complete("s1", 1))


def test_sessions_isolated() -> None:
    store = TodoStore()
    asyncio.run(store.create("sA", ["a"]))
    asyncio.run(store.create("sB", ["b"]))
    assert [t.title for t in asyncio.run(store.list_tasks("sA"))] == ["a"]
    assert [t.title for t in asyncio.run(store.list_tasks("sB"))] == ["b"]


def test_concurrent_complete_no_lost_update() -> None:
    """Two coroutines completing different tasks on the same session
    must both succeed (no lost update from read-modify-write)."""
    store = TodoStore()
    asyncio.run(store.create("s1", ["a", "b"]))

    async def run():
        await asyncio.gather(
            store.complete("s1", 1, result="A"),
            store.complete("s1", 2, result="B"),
        )
        return await store.list_tasks("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}
