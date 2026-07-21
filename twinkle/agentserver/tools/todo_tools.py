# twinkle/agentserver/tools/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

3 个 @tool:create / complete / list。读 plan_todo_context 拿当前
session_id,操作模块级 TodoStore 单例,返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。业务错误 catch 成 "Error: ..." 字符串
返回(虽然 ToolManager.execute 也兜底,但工具层自转更可读)。

对齐 jiuwenclaw tools/todo_toolkits.py,砍 start/insert/remove/batch
与 op-result 总线。
"""
from __future__ import annotations

from twinkle.agentserver.plan_todo_context import get_plan_todo_session_id
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.todo_store import TodoError, TodoStore, TodoTask

_store = TodoStore()  # 模块级单例;session 隔离靠 ContextVar 路由

_ICON = {"waiting": "[ ]", "running": "[>]", "completed": "[x]"}


def _format_tasks(tasks: list[TodoTask]) -> str:
    if not tasks:
        return "No todo tasks."
    lines = []
    for t in tasks:
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        lines.append(f"- {icon} {t.idx}. {t.title}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks: list[TodoTask]) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


@tool
async def todo_create(tasks: list[str]) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task descriptions; fails if a todo list already exists for this session.
    """
    sid = get_plan_todo_session_id()
    try:
        created = await _store.create(sid, tasks)
        return _append_list(f"Created {len(created)} todo tasks.", created)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_complete(idx: int, result: str = "") -> str:
    """Mark a todo task as completed and save a brief result. Pass the 1-based idx and an optional short result string.
    """
    sid = get_plan_todo_session_id()
    try:
        tasks = await _store.complete(sid, idx, result)
        return _append_list(f"Task {idx} marked as completed.", tasks)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list() -> str:
    """List all current todo tasks with their status. Returns 'No todo tasks.' when empty.
    """
    sid = get_plan_todo_session_id()
    tasks = await _store.list_tasks(sid)
    return _format_tasks(tasks)
