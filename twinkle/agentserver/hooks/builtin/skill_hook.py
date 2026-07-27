"""SkillHook — before_model_call 注入 skill 清单/提示。

all 模式:把全部 skill name+desc 拼成 system msg 注入(模型每步看见清单)。
auto_list 模式:只注入一句"调 list_skill"提示(模型要时自己拉清单)。
无 skills → no-op。注入用赋新 list(不 in-place mutate,避免污染 store 内部 list)。
mode 传 None 时从 config 读 SKILL_MODE(生产用),测试可直传 mode。
"""
from __future__ import annotations

from twinkle.agentserver.hooks.base import AgentHook, HookContext


class SkillHook(AgentHook):
    priority = 90  # 功能层(50-99);before_model_call,与 PermissionHook(before_tool_call)不同事件

    def __init__(self, mode: str | None = None) -> None:
        self._mode = mode  # None → 调用时从 config 读

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return  # 无 skill → no-op
        mode = self._mode or _resolve_mode()
        if mode == "auto_list":
            self._prepend(ctx, "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。")
        else:  # "all"(默认)
            lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
            self._prepend(ctx, "\n".join(lines))

    def _prepend(self, ctx: HookContext, content: str) -> None:
        # 赋新 list(不原地 insert——msgs 可能是 store 的内部 list,in-place 会污染历史)
        ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages


def _resolve_mode() -> str:
    from twinkle.config import SKILL_MODE
    return SKILL_MODE
