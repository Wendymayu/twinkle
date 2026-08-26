"""ProgressiveToolHook — eager/deferred 工具可见性。

两个事件:
- before_invoke: 重算 eager(把非 allow 档工具拉回 eager,防 invoke_tool 绕过审批门)
  + 注 deferred 工具导航 frozen_section(跨步稳定 + cache 友好)
- before_model_call: 过滤 ctx.inputs.tools 为 eager 名单

eager 名单构造时由 builder 传入,before_invoke 每请求重算。tm 经
ctx.agent._tool_manager 取(init 收 None,需 agent 的 hook 走 ctx.agent)。
"""
from __future__ import annotations

from typing import Any, Iterable

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

    def __init__(self, eager_names: Iterable[str], permissions: Any) -> None:
        # 存引用(builder 传同一 eager set 给本 hook + 两 meta-tool):before_invoke 重算改共享
        # set,三方都见,invoke_tool 的 deferred 判断随之同步(非 allow 工具不漏进 deferred)。
        # 非 set 入参转 set(_force_protected_eager 需 .add)。
        self.eager_names = eager_names if isinstance(eager_names, set) else set(eager_names)
        self.eager_names |= META_NAMES   # before_model_call 过滤需含 meta 名
        self._permissions = permissions

    async def before_invoke(self, ctx: HookContext) -> None:
        # 每请求重算 eager:把 tm 中非 allow 档工具拉回 eager(#1 权限守卫——invoke_tool 直接
        # tm.execute 不经 before_tool_call,非 allow 档不许 defer,须留 eager 走审批门)。
        # 幂等(已在 eager 的跳过)。局部 import 避循环(progressive.py 模块级 import 本类)。
        from twinkle.agentserver.tools.progressive import _force_protected_eager
        _force_protected_eager(ctx.agent._tool_manager, self.eager_names, self._permissions)
        deferred = deferred_schemas(ctx.agent._tool_manager, self.eager_names)
        if not deferred:
            return  # 无 deferred 工具 → 不注导航(no-op)
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
