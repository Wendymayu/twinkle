# tests/test_todo_accessor.py
from twinkle.agentserver.todo import get_todo_store, _set_todo_store
from twinkle.agentserver.todo.store import TodoStore


def test_get_todo_store_returns_singleton(tmp_path):
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    try:
        a = get_todo_store()
        b = get_todo_store()
        assert a is b
    finally:
        _set_todo_store(None)


def test_set_todo_store_swaps(tmp_path):
    custom = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(custom)
    try:
        assert get_todo_store() is custom
    finally:
        _set_todo_store(None)
