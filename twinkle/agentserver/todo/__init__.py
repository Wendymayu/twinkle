"""todo 包入口 — re-exports + 进程级单例访问器。"""
from twinkle.agentserver.todo.store import TodoStore, TodoTask, TodoError
from twinkle.agentserver.todo.context import (
    PLAN_TODO_SESSION_ID, get_plan_todo_session_id,
    TODO_EVENTS, reset_todo_events, append_todo_event, flush_todo_events,
)


_TODO_STORE: TodoStore | None = None


def get_todo_store() -> TodoStore:
    """进程级单例 TodoStore(惰性构造,处处共享同一实例 + 同一套锁)。

    不像 sessions/__init__.py 的 session_store() 返 fresh 实例(DI 穿参用)——
    todo 工具是模块级 @tool 函数,不便接收 DI,故用单例访问器达到"一处构造、
    处处共享"。lazy import config 避免 import-time 副作用。
    """
    global _TODO_STORE
    if _TODO_STORE is None:
        from twinkle.config import TODOS_DIR
        _TODO_STORE = TodoStore(TODOS_DIR)
    return _TODO_STORE


def _set_todo_store(store: TodoStore | None) -> None:
    """测试钩子:替换/重置单例(配 tmp_path 盘)。生产代码不调。"""
    global _TODO_STORE
    _TODO_STORE = store


__all__ = [
    "TodoStore", "TodoTask", "TodoError",
    "PLAN_TODO_SESSION_ID", "get_plan_todo_session_id",
    "TODO_EVENTS", "reset_todo_events", "append_todo_event", "flush_todo_events",
    "get_todo_store", "_set_todo_store",
]
