# twinkle/agentserver/tools/builtin/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

3 个 @tool:create / complete / list。读 plan_todo_context 拿当前 session_id,
经 get_todo_store() 取共享 TodoStore 单例操作,返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。TodoStore 的 create 返回 list[TodoTask],
update 返回 (task, warning)。工具层在 mutation 后调用 list() 取全量
再拼 markdown + snapshot。业务错误 catch 成 "Error: ..." 字符串返回。

对齐 jiuwenclaw tools/todo_toolkits.py,砍 start/insert/remove/batch
与 op-result 总线。store 单例经 todo.get_todo_store() 进程级共享(工具 + session.delete
清理同一实例,同一套锁)。
"""
from __future__ import annotations

from twinkle.agentserver.todo import (
    get_plan_todo_session_id,
    get_todo_store,
    append_todo_event,
    TodoError, TodoTask,
)
from twinkle.agentserver.tools.decorator import tool

_ICON = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}


def _format_tasks(tasks: list[TodoTask]) -> str:
    if not tasks:
        return "No todo tasks."
    lines = []
    for i, t in enumerate(tasks, 1):
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        lines.append(f"- {icon} {i}. {t.subject}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks: list[TodoTask]) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


def _snapshot(tasks: list[TodoTask]) -> dict:
    """Structured todo snapshot for the UI (publish side-channel)."""
    not_completed = sum(1 for t in tasks if t.status in ("pending", "in_progress"))
    completed = sum(1 for t in tasks if t.status == "completed")
    return {
        "tasks": [
            {"id": t.id, "subject": t.subject, "status": t.status, "result": t.result}
            for t in tasks
        ],
        "remaining": not_completed,
        "total": not_completed + completed,
    }


@tool
async def todo_create(tasks: list[str]) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task descriptions; fails if a todo list already exists for this session.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        await store.create(session_id, tasks)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        return _append_list(f"Created {len(current)} todo tasks.", current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_complete(idx: int, result: str = "") -> str:
    """Mark a todo task as completed and save a brief result. Pass the 1-based idx and an optional short result string.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        current = await store.list(session_id)
        if idx < 1 or idx > len(current):
            raise TodoError(f"Task {idx} not found.")
        task_id = current[idx - 1].id
        task, warning = await store.update(session_id, task_id, status="completed", result=result)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        msg = f"Task {idx} marked as completed."
        if warning:
            msg += f" {warning}"
        return _append_list(msg, current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list() -> str:
    """List all current todo tasks with their status. Returns 'No todo tasks.' when empty.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    tasks = await store.list(session_id)
    return _format_tasks(tasks)
