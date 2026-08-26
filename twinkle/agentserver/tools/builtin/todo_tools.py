# twinkle/agentserver/tools/builtin/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

4 个 @tool: create / update / list / get。读 plan_todo_context 拿当前 session_id,
经 get_todo_store() 取共享 TodoStore 单例操作, 返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。mutation 后调用 append_todo_event() 发布 snapshot。
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
    for t in tasks:
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        deps = f" (blocked by: {', '.join(t.blocked_by[:6])}{'...' if len(t.blocked_by) > 6 else ''})" if t.blocked_by else ""
        owner = f" [@{t.owner}]" if t.owner else ""
        lines.append(f"- {icon} {t.subject}{deps}{owner}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks: list[TodoTask]) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


def _snapshot(tasks: list[TodoTask]) -> dict:
    """给 UI 用的结构化 todo 快照(发布 side-channel)。"""
    pending_running = sum(1 for t in tasks if t.status in ("pending", "in_progress"))
    completed = sum(1 for t in tasks if t.status == "completed")
    return {
        "tasks": [
            {
                "id": t.id, "subject": t.subject, "description": t.description,
                "status": t.status, "result": t.result,
                "blocked_by": t.blocked_by, "owner": t.owner,
                "metadata": t.metadata,
                "created_at": t.created_at, "updated_at": t.updated_at,
            }
            for t in tasks
        ],
        "remaining": pending_running,
        "total": pending_running + completed,
    }


@tool
async def todo_create(subjects: list[str], sequential: bool = False) -> str:
    """创建一组 todo task 来规划并跟踪多步工作。不要用于单步简单请求。传入任务主题列表;若本 session 已存在 todo 列表则失败。任务必须按顺序执行时用 sequential=True。
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        tasks = await store.create(session_id, subjects, sequential=sequential)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        seq_note = " (sequential)" if sequential else ""
        return _append_list(f"Created {len(tasks)} todo tasks{seq_note}.", current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_update(task_id: str, status: str = "", result: str = "", owner: str = "", metadata: dict | None = None) -> str:
    """更新一个 todo task 的 status、result、owner 或 metadata。用 status="completed" 标记完成,status="in_progress" 开始处理,status="cancelled" 取消。metadata 会合并:把某 key 设为 null 即删除该 key。
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        kwargs = {}
        if status:
            kwargs["status"] = status
        if result:
            kwargs["result"] = result
        if owner:
            kwargs["owner"] = owner
        if metadata is not None:
            kwargs["metadata"] = metadata
        task, warning = await store.update(session_id, task_id, **kwargs)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        msg = f"Updated task {task_id}."
        if warning:
            msg += f"\n{warning}"
        return _append_list(msg, current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list(status: str = "") -> str:
    """列出所有当前 todo task 及其 status。可选按 status 过滤(pending/in_progress/completed/cancelled)。为空时返回 'No todo tasks.'。
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    status_filter = status if status else None
    tasks = await store.list(session_id, status=status_filter)
    return _format_tasks(tasks)


@tool
async def todo_get(task_id: str) -> str:
    """按 ID 获取单个 todo task 的详情。返回 task 信息,未找到则返回错误。
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    task = await store.get(session_id, task_id)
    if task is None:
        return f"Task {task_id} not found."
    lines = [
        f"ID: {task.id}",
        f"Subject: {task.subject}",
        f"Status: {task.status}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.result:
        lines.append(f"Result: {task.result}")
    if task.blocked_by:
        lines.append(f"Blocked by: {', '.join(task.blocked_by)}")
    if task.owner:
        lines.append(f"Owner: {task.owner}")
    if task.metadata:
        lines.append(f"Metadata: {task.metadata}")
    return "\n".join(lines)
