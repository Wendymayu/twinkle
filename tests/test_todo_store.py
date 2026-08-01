# tests/test_todo_store.py
import asyncio
import json

import pytest

from twinkle.agentserver.todo import TodoError, TodoStore
from twinkle.agentserver.todo.store import TodoTask


def test_task_dataclass_fields() -> None:
    t = TodoTask(id="abc", subject="hello")
    assert t.id == "abc"
    assert t.subject == "hello"
    assert t.description == ""
    assert t.status == "pending"
    assert t.result == ""
    assert t.blocked_by == []
    assert t.owner == ""
    assert t.metadata == {}
    assert t.created_at == 0.0
    assert t.updated_at == 0.0


def test_create_then_list(todo_store) -> None:
    async def run():
        await todo_store.create("s1", ["a", "b"])
        return await todo_store.list("s1")

    listed = asyncio.run(run())
    assert len(listed) == 2
    assert [t.subject for t in listed] == ["a", "b"]
    assert all(t.status == "pending" for t in listed)
    assert all(t.id for t in listed)  # non-empty UUIDs


def test_create_returns_tasks_with_ids(todo_store) -> None:
    async def run():
        return await todo_store.create("s1", ["alpha", "beta"])

    tasks = asyncio.run(run())
    assert len(tasks) == 2
    assert tasks[0].subject == "alpha"
    assert tasks[0].id  # UUID assigned
    assert tasks[0].created_at > 0


def test_create_empty_raises(todo_store) -> None:
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(todo_store.create("s1", []))


def test_create_twice_refuses_while_in_progress(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="already exists"):
        asyncio.run(todo_store.create("s1", ["b"]))


def test_sequential_sets_blocked_by(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b", "c"], sequential=True)
        return tasks

    tasks = asyncio.run(run())
    assert tasks[0].blocked_by == []
    assert tasks[1].blocked_by == [tasks[0].id]
    assert tasks[2].blocked_by == [tasks[1].id]


def test_sequential_false_no_blocked_by(todo_store) -> None:
    async def run():
        return await todo_store.create("s1", ["a", "b"])

    tasks = asyncio.run(run())
    assert all(t.blocked_by == [] for t in tasks)


def test_update_status(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, warning = await todo_store.update("s1", tasks[0].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is None


def test_update_completed(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, status="completed", result="done A")
        return task

    task = asyncio.run(run())
    assert task.status == "completed"
    assert task.result == "done A"


def test_update_owner_and_metadata(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, owner="agent-1", metadata={"key": "val"})
        return task

    task = asyncio.run(run())
    assert task.owner == "agent-1"
    assert task.metadata == {"key": "val"}


def test_update_metadata_merge_style(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        await todo_store.update("s1", tasks[0].id, metadata={"k1": "v1", "k2": "v2"})
        task, _ = await todo_store.update("s1", tasks[0].id, metadata={"k2": None, "k3": "v3"})
        return task

    task = asyncio.run(run())
    assert task.metadata == {"k1": "v1", "k3": "v3"}


def test_update_blocked_by_warning(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"], sequential=True)
        # b is blocked by a; marking b as in_progress should warn
        task, warning = await todo_store.update("s1", tasks[1].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is not None
    assert "unresolved" in warning


def test_update_no_warning_when_blocked_by_completed(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"], sequential=True)
        await todo_store.update("s1", tasks[0].id, status="completed")
        task, warning = await todo_store.update("s1", tasks[1].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is None


def test_update_unknown_task_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(todo_store.update("s1", "nonexistent-id", status="completed"))


def test_update_invalid_status_raises(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        await todo_store.update("s1", tasks[0].id, status="invalid")

    with pytest.raises(TodoError, match="Invalid status"):
        asyncio.run(run())


def test_get_found(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        return await todo_store.get("s1", tasks[0].id)

    task = asyncio.run(run())
    assert task is not None
    assert task.subject == "a"


def test_get_not_found(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    assert asyncio.run(todo_store.get("s1", "nonexistent")) is None


def test_list_with_status_filter(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b", "c"])
        await todo_store.update("s1", tasks[0].id, status="completed")
        await todo_store.update("s1", tasks[1].id, status="in_progress")
        return await todo_store.list("s1", status="pending")

    pending = asyncio.run(run())
    assert len(pending) == 1
    assert pending[0].subject == "c"


def test_sessions_isolated(todo_store) -> None:
    asyncio.run(todo_store.create("sA", ["a"]))
    asyncio.run(todo_store.create("sB", ["b"]))
    assert [t.subject for t in asyncio.run(todo_store.list("sA"))] == ["a"]
    assert [t.subject for t in asyncio.run(todo_store.list("sB"))] == ["b"]


def test_concurrent_update_no_lost_update(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"])
        await asyncio.gather(
            todo_store.update("s1", tasks[0].id, status="completed", result="A"),
            todo_store.update("s1", tasks[1].id, status="completed", result="B"),
        )
        return await todo_store.list("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}


def test_persistence_across_restart(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    tasks = asyncio.run(todo_store.list("s1"))
    asyncio.run(todo_store.update("s1", tasks[0].id, status="completed"))

    cold = TodoStore(str(todos_dir))
    listed = asyncio.run(cold.list("s1"))
    assert [t.subject for t in listed] == ["a", "b"]
    assert listed[0].status == "completed"
    assert listed[1].status == "pending"


def test_load_corrupt_json_returns_empty(todo_store, todos_dir) -> None:
    (todos_dir / "s1.json").write_text("{not valid json", encoding="utf-8")
    listed = asyncio.run(todo_store.list("s1"))
    assert listed == []
    asyncio.run(todo_store.create("s1", ["fresh"]))
    listed = asyncio.run(todo_store.list("s1"))
    assert [t.subject for t in listed] == ["fresh"]


def test_save_writes_json_with_new_fields(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    on_disk = json.loads((todos_dir / "s1.json").read_text(encoding="utf-8"))
    task = on_disk[0]
    assert "id" in task
    assert task["subject"] == "a"
    assert task["status"] == "pending"
    assert "blocked_by" in task
    assert "owner" in task
    assert "metadata" in task
    assert "created_at" in task
    assert "updated_at" in task


def test_delete_removes_file(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    p = todos_dir / "s1.json"
    assert p.is_file()
    assert asyncio.run(todo_store.delete("s1")) is True
    assert not p.exists()
    assert asyncio.run(todo_store.list("s1")) == []


def test_delete_missing_returns_false(todo_store) -> None:
    assert asyncio.run(todo_store.delete("never")) is False


def test_cancelled_status(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, status="cancelled")
        return task

    task = asyncio.run(run())
    assert task.status == "cancelled"
