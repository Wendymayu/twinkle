"""team tools — thin wrappers that read the Team from ContextVar.

Each tool: reads CURRENT_TEAM → calls a Team/TeamTaskStore method → formats.
delegate_to_member delegates to a member agent (independent, no shared history);
the 7 task/message tools wrap TeamTaskStore + member inbox for shared-queue
coordination. member_name for claim/complete auto-derives from
CURRENT_MEMBER_NAME (set in _drive_member, Task 7); before that, callers pass
it explicitly.
"""

from __future__ import annotations

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.todo import TodoError
from twinkle.agentserver.tools.decorator import tool


@tool
async def delegate_to_member(member_name: str, persona: str, objective: str,
                             prompt: str = "") -> str:
    """委派任务给团队的一个成员。成员是独立 agent，看不到你的对话历史。
    第一次委派某 member_name 会创建该成员。

    member_name: 成员名（简短英文标识，如 researcher）。稳定可读，用于 task owner/消息寻址。
    persona: 成员角色描述。如 "金融分析师，专长美股财报分析"。
    objective: 任务目标。自包含——成员所需一切应在此描述。
              主路径用"认领并执行 queue 中你能做的 task"。
    prompt: 可选，额外上下文或具体指令。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    return await team.delegate(member_name, persona, objective, prompt)


@tool
async def create_task(subject: str, blocked_by: list[str] | None = None) -> str:
    """创建一个 team 共享任务入队。blocked_by 指定前置依赖（它们的 id）。

    subject: 任务主题/目标。
    blocked_by: 可选，前置 task id 列表；这些 task completed 后本 task 才能被认领。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        t = await team.task_store.create_task(subject, blocked_by=blocked_by)
        return f"Created task {t.id}: {t.subject}" + (
            f" (blocked_by {t.blocked_by})" if t.blocked_by else "")
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def claim_task(task_id: str, member_name: str = "") -> str:
    """认领一个 team task（独占）。需 pending 且无 owner 且前置全完成。

    task_id: 要认领的 task id。
    member_name: 你的成员名（作 owner）。可省，自动取当前成员名。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    name = member_name or _current_member_name()
    try:
        t = await team.task_store.claim_task(task_id, name)
        return f"Claimed task {t.id}: {t.subject} (owner={t.owner})"
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def complete_task(task_id: str, result: str = "",
                        help_reason: str = "",
                        member_name: str = "") -> str:
    """完成你认领的 task（写结果），或在遇困难时请求 leader 帮助（标 help_reason）。

    task_id: 你的 task id。
    result: 任务结果/产出（完成时）。
    help_reason: 遇困难求助时写明原因。非空时标 metadata.help_reason + 不标 completed；
                 member run 结束后 task 回 pending，leader 通过 list_tasks 看 help_reason
                 决定 steer/重派（spec §1.4 求助，不混 blocked）。
    member_name: 你的成员名（可省，自动取）。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    name = member_name or _current_member_name()
    try:
        if help_reason:
            t = await team.task_store.request_help(task_id, help_reason, name)
            return f"Help requested on task {t.id}: {help_reason}"
        t = await team.task_store.complete_task(task_id, result, name)
        return f"Completed task {t.id}."
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def cancel_task(task_id: str) -> str:
    """取消一个 team 任务（leader 用）。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        t = await team.task_store.cancel_task(task_id)
        return f"Cancelled task {t.id}."
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def list_tasks(status: str = "") -> str:
    """列出所有 team task。可按 status 过滤（pending/in_progress/completed/cancelled）。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    tasks = await team.task_store.list_tasks(status=status or None)
    return _format_team_tasks(tasks)


@tool
async def get_task(task_id: str) -> str:
    """查看单个 team task 详情（含 result/owner/blocked_by/help_reason）。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    t = await team.task_store.get_task(task_id)
    if t is None:
        return f"Task {task_id} not found."
    return _format_team_tasks([t])


@tool
async def send_member(member_name: str, message: str) -> str:
    """向指定成员发送异步消息（steer，非阻塞）。消息进入成员信箱，成员下次运行时读取。
    只在 member 跑时有效调整方向；idle 时滞留。

    member_name: 目标成员名。
    message: 消息内容（运行中调整方向用，不派发任务——任务走 create_task）。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        return await team.send_member(member_name, message)
    except KeyError:
        return f"Error: unknown member '{member_name}'"


def _current_member_name() -> str:
    """member 工具调用时取自己的 member_name。

    member run 时 `_drive_member` 的 `_run()` set `CURRENT_MEMBER_NAME` ContextVar
    （Task 7 实现），member 调工具时自动取，无需 LLM 显式传参。Task 7 落地前
    返回空——此时 `claim_task(task_id, member_name)` 走显式传参 fallback。
    """
    from twinkle.agentserver.team.context import CURRENT_MEMBER_NAME
    return CURRENT_MEMBER_NAME.get() or ""


def _format_team_tasks(tasks) -> str:
    if not tasks:
        return "No team tasks."
    icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]",
            "cancelled": "[-]"}
    lines = []
    for t in tasks:
        ic = icon.get(t.status, "[ ]")
        deps = (f" (blocked by: {', '.join(t.blocked_by)})" if t.blocked_by else "")
        owner = f" [@{t.owner}]" if t.owner else ""
        res = f" | {t.result}" if t.result else ""
        help_r = t.metadata.get("help_reason")
        help_line = f" ⚠help: {help_r}" if help_r else ""
        lines.append(f"- {ic} {t.id}: {t.subject}{deps}{owner}{res}{help_line}")
    return "\n".join(lines)
