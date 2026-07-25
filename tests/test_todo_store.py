# tests/test_todo_store.py
import asyncio
import json

import pytest

from twinkle.agentserver.todo import TodoError, TodoStore


def test_create_then_list(todo_store) -> None:
    async def run():
        await todo_store.create("s1", ["a", "b"])
        return await todo_store.list_tasks("s1")

    listed = asyncio.run(run())
    assert [t.idx for t in listed] == [1, 2]
    assert [t.title for t in listed] == ["a", "b"]
    assert all(t.status == "waiting" for t in listed)


def test_create_empty_raises(todo_store) -> None:
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(todo_store.create("s1", []))


def test_create_twice_refuses_while_in_progress(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="in progress"):
        asyncio.run(todo_store.create("s1", ["b"]))


def test_create_replaces_when_all_completed(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1))
    asyncio.run(todo_store.complete("s1", 2))
    # all completed -> create allowed, replaces the old list
    asyncio.run(todo_store.create("s1", ["c"]))
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert [t.title for t in listed] == ["c"]
    assert all(t.status == "waiting" for t in listed)


def test_complete_marks_status_and_result(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1, result="done A"))
    tasks = asyncio.run(todo_store.list_tasks("s1"))
    assert tasks[0].status == "completed"
    assert tasks[0].result == "done A"
    assert tasks[1].status == "waiting"


def test_complete_unknown_idx_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(todo_store.complete("s1", 99))


def test_complete_already_completed_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    asyncio.run(todo_store.complete("s1", 1))
    with pytest.raises(TodoError, match="already completed"):
        asyncio.run(todo_store.complete("s1", 1))


def test_sessions_isolated(todo_store) -> None:
    asyncio.run(todo_store.create("sA", ["a"]))
    asyncio.run(todo_store.create("sB", ["b"]))
    assert [t.title for t in asyncio.run(todo_store.list_tasks("sA"))] == ["a"]
    assert [t.title for t in asyncio.run(todo_store.list_tasks("sB"))] == ["b"]


def test_concurrent_complete_no_lost_update(todo_store) -> None:
    """Two coroutines completing different tasks on the same session
    must both succeed (no lost update from read-modify-write)."""
    asyncio.run(todo_store.create("s1", ["a", "b"]))

    async def run():
        await asyncio.gather(
            todo_store.complete("s1", 1, result="A"),
            todo_store.complete("s1", 2, result="B"),
        )
        return await todo_store.list_tasks("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}


def test_persistence_across_restart(todo_store, todos_dir) -> None:
    """A brand-new TodoStore pointing at the same dir sees the persisted list
    (no in-memory carryover across instances)."""
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1))

    cold = TodoStore(str(todos_dir))  # fresh instance, cold "cache"
    listed = asyncio.run(cold.list_tasks("s1"))
    assert [t.title for t in listed] == ["a", "b"]
    assert listed[0].status == "completed"
    assert listed[1].status == "waiting"


def test_load_corrupt_json_returns_empty(todo_store, todos_dir) -> None:
    (todos_dir / "s1.json").write_text("{not valid json", encoding="utf-8")
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert listed == []
    # create treats corrupt/missing as no list -> succeeds
    asyncio.run(todo_store.create("s1", ["fresh"]))
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert [t.title for t in listed] == ["fresh"]


def test_save_writes_json_with_full_fields(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    on_disk = json.loads((todos_dir / "s1.json").read_text(encoding="utf-8"))
    assert on_disk == [{"idx": 1, "title": "a", "status": "waiting", "result": ""}]


def test_delete_removes_file(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    p = todos_dir / "s1.json"
    assert p.is_file()
    assert asyncio.run(todo_store.delete("s1")) is True
    assert not p.exists()
    assert asyncio.run(todo_store.list_tasks("s1")) == []


def test_delete_missing_returns_false(todo_store) -> None:
    assert asyncio.run(todo_store.delete("never")) is False
