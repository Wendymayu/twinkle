"""SkillHook — before_invoke 注入 skill 清单/提示到 ctx.extra["frozen_sections"]。

all 模式:把全部 skill name+desc 拼成 section stash(每步由 loop 套用到 builder,跨步稳定)。
auto_list 模式:只 stash 一句"调 list_skill"提示(模型要时自己拉清单)。
无 skills → no-op。注入走 ctx.extra["frozen_sections"](loop 每步 add_section),
hook 不碰 messages/builder(before_invoke 时 builder 尚不存在)。
mode 传 None 时从 config 读 SKILL_MODE(生产用),测试可直传 mode。
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

log = logging.getLogger("twinkle.hooks.skill")


class SkillHook(AgentHook):
    priority = 90  # 功能层(50-99);before_invoke,与 PermissionHook(before_tool_call)不同事件

    def __init__(self, mode: str | None = None) -> None:
        self._mode = mode  # None → 调用时从 config 读

    async def before_invoke(self, ctx: HookContext) -> None:
        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return  # 无 skill → no-op(不创建 frozen_sections key)
        mode = self._mode or _get_skill_mode()
        if mode == "auto_list":
            content = "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。"
        else:  # "all"(默认);未知 mode 也落到 all 并告警,避免静默误配置
            if mode != "all":
                log.warning("unknown SKILL_MODE %r, falling back to 'all'", mode)
            lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
            content = "\n".join(lines)
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("skills", content, priority=90))


def _get_skill_mode() -> str:
    from twinkle.config import SKILL_MODE
    return SKILL_MODE
