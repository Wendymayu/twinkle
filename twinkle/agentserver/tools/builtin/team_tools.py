"""delegate_to_member — thin wrapper that reads the Team from ContextVar.

The tool signature is intentionally minimal: persona + objective + optional prompt.
LLM invents persona dynamically; all members share the same tool whitelist.
"""

from __future__ import annotations

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.tools.decorator import tool


@tool
async def delegate_to_member(persona: str, objective: str,
                             prompt: str = "") -> str:
    """委派任务给团队的一个成员。成员是独立 agent，看不到你的对话历史。

    persona: 成员角色描述。如 "金融分析师，专长美股财报分析"
    objective: 任务目标。自包含——成员所需一切应在此描述。
    prompt: 可选，额外上下文或具体指令。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    return await team.delegate(persona, objective, prompt)
