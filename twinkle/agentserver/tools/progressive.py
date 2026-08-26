"""apply_progressive_tools — 渐进可见叠加层 builder。

接入 server.py create_agent(在 mcp.register_into 之后):
  hook = apply_progressive_tools(tools, settings.progressive_tool, settings.permissions)
  if hook: all_hooks.append(hook)

enabled=False → 返回 None(no-op,行为等同现状)。
enabled=True  → 注册两 meta-tool + 返回 ProgressiveToolHook。
默认 eager = tool_manager() 的纯内置名 + meta(MCP 工具名不在 → 自动 deferred)。

权限守卫(#1):invoke_tool 直接 tm.execute 不经 _tool_call 的 before_tool_call,
PermissionHook 只看到 'invoke_tool' 而非内层 deferred 工具 → 审批门被绕过。故凡
非 allow 档(require-approval/deny)的工具强制留 eager,不许 defer,保证审批级
工具仍走 _tool_call → before_tool_call → 审批门 intact。
"""
from __future__ import annotations

from typing import Any

from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.tools.builtin.progressive_tools import (
    META_NAMES, ToolsSearchTool, InvokeToolTool,
)


def apply_progressive_tools(tm: Any, config: Any, permissions: Any):
    """启用渐进可见:注册 meta-tool + 返回 hook;否则返回 None。

    permissions: PermissionsConfig,用于权限守卫(读 permissions.tools[name] 或
    global_default 判定静态档位;非 allow 档的工具强制 eager)。
    """
    if not getattr(config, "enabled", False):
        return None
    from twinkle.agentserver.tools import tool_manager
    builtin_names = {t.card.name for t in tool_manager().list()}   # 纯 33 内置
    eager = set(config.eager_tools) if config.eager_tools else builtin_names
    eager |= META_NAMES                                     # 强制 meta 进 eager
    _force_protected_eager(tm, eager, permissions)         # #1 权限守卫
    tm.register(ToolsSearchTool(tm, eager))
    tm.register(InvokeToolTool(tm, eager))
    return ProgressiveToolHook(eager, permissions)


def _force_protected_eager(tm: Any, eager: set, permissions: Any) -> None:
    """强制非 allow 档工具留 eager(防 invoke_tool 绕过 PermissionHook 审批门)。

    invoke_tool 调 tm.execute 不经 _tool_call 的 before_tool_call 钩子,故 deferred
    工具的审批门被绕过。本函数把所有静态档位非 allow(require-approval/deny)的工具
    拉回 eager,使其仍走 _tool_call → before_tool_call → 审批门 intact。
    静态档位 = permissions.tools.get(name, permissions.global_default)。
    """
    global_default = getattr(permissions, "global_default", "allow")
    tiers = getattr(permissions, "tools", {}) or {}
    for t in tm.list():
        name = t.card.name
        if name in eager or name in META_NAMES:
            continue
        if tiers.get(name, global_default) != "allow":
            eager.add(name)
