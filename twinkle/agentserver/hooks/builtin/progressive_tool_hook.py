"""ProgressiveToolHook — eager/deferred 工具可见性。

对齐 jiuwenswarm JiuWenProgressiveToolRail,精简为两个事件:
- before_invoke: 注 deferred 工具导航 frozen_section(跨步稳定 + cache 友好,对齐 SkillHook)
- before_model_call: 过滤 ctx.inputs.tools 为 eager 名单

eager 名单构造时传入(来自 builder);tm 经 ctx.agent._tool_manager 取(对齐
HookManager.register_hook 注释:init 收 None,需 agent 的 hook 走 ctx.agent)。
"""
from __future__ import annotations

from typing import Iterable

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.tools.builtin.progressive_tools import (
    META_NAMES, deferred_schemas,
)

_NAV_PRIORITY = 70
_DESC_LIMIT = 160


class ProgressiveToolHook(AgentHook):
    """eager/deferred 工具渐进可见。enabled 由 builder 控制(关闭则不注册本 hook)。"""

    priority = 70

    def __init__(self, eager_names: Iterable[str]) -> None:
        self.eager_names = set(eager_names) | META_NAMES

    async def before_invoke(self, ctx: HookContext) -> None:
        deferred = deferred_schemas(ctx.agent._tool_manager, self.eager_names)
        if not deferred:
            return  # 无 deferred → no-op(对齐 SkillHook 无 skill 时 no-op)
        entries = []
        for s in sorted(deferred, key=lambda x: x["function"]["name"]):
            name = s["function"]["name"]
            desc = s["function"].get("description", "") or ""
            brief = desc[:_DESC_LIMIT]
            entries.append(f"- {name}: {brief}")
        header = (
            "## 按需可见工具导航(不可直接调用)\n\n"
            "**重要提示:以下工具不在当前 tools 列表中,无法直接调用。**\n\n"
            "使用方法:\n"
            "1. **必须先**调用 `tools_search`,传入与导航列表一致的 `tool_name`,"
            "获取完整参数 schema。\n"
            "2. **然后**调用 `invoke_tool`,传入精确 `tool_name` 和根据 schema 构造的 `arguments`。\n\n"
            "**切勿直接调用以下工具——直接调用会失败。**\n\n"
        )
        content = header + "\n".join(entries)
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("tool_navigation", content, priority=_NAV_PRIORITY))

    async def before_model_call(self, ctx: HookContext) -> None:
        ctx.inputs.tools = [
            t for t in ctx.inputs.tools
            if t.get("function", {}).get("name", "") in self.eager_names
        ]
