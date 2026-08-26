"""spawn_subagent —— 把一个隔离子任务委派给一个全新子 agent(黑盒)。

从 subagent_context 的 ContextVars 读取 executor + 父 session/request id
(由父 loop 上的 SubagentContextHook 设置)。运行子 agent 到收敛,
把其最终答案(+ 停止提示)作为 tool_result 字符串返回。
"""
from __future__ import annotations

from .context import (
    get_subagent_executor,
    get_subagent_parent_request_id,
    get_subagent_parent_session_id,
)
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.errors import ToolError
from twinkle.agentserver.tools.builtin.subagent.models import (
    SubagentResult,
    SubagentTaskSpec,
)

_SUBAGENT_STOP_HINT = (
    "\n\n[SYSTEM] The delegated task is complete. "
    "Summarize the result to the user and finish your turn. "
    "Do NOT call spawn_subagent again for this task."
)


def _wrap(result: SubagentResult) -> str:
    if result.success:
        return (result.result or "") + _SUBAGENT_STOP_HINT
    return (result.error or "subagent failed") + _SUBAGENT_STOP_HINT


@tool
async def spawn_subagent(objective: str, prompt: str = "") -> str:
    """把一个隔离子任务委派给一个全新子 agent,后者在隔离 session 中
    跑自己的 ReAct loop,只返回最终答案。

    何时委派:
    - 子任务复杂 / 多步,值得聚焦的 ReAct。
    - 你想要隔离(全新 context,不会污染本次对话)。
    - 不同子任务相互独立(每个各调一次 spawn_subagent)。

    何时不要委派:
    - 一次 tool 调用或直接回答就够 —— 自己做。
    - 子任务需要本次对话历史 —— 在 `objective` 里显式传给子 agent
      (子 agent 看不到本 agent 的历史)。

    `objective` 须自包含:目标 + 约束 + 子 agent 所需的一切上下文
    (它别的都看不到)。`prompt` 可携带额外指令(如输出格式)。
    子 agent 不能问用户;它必须收敛或返回失败说明。其最终答案
    (过大则截断)成为你的 tool_result —— 把它总结给用户;
    不要对同一任务重复委派。
    """
    executor = get_subagent_executor()
    parent_session_id = get_subagent_parent_session_id()
    if executor is None or parent_session_id is None:
        raise ToolError("subagent executor not initialized on this loop", kind="unavailable")
    parent_request_id = get_subagent_parent_request_id() or parent_session_id
    task = SubagentTaskSpec(objective=objective, prompt=prompt)
    result = await executor.execute_subagent(
        task, parent_session_id=parent_session_id, parent_request_id=parent_request_id
    )
    return _wrap(result)
