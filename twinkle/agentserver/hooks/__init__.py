"""Twinkle Hook 机制 — 公共 API。

镜像 jiuwen 的 Rail 系统，采用 Hook 命名。
"""
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    HookInputs,
    InvokeInputs,
    ModelCallInputs,
    RetryRequest,
    ForceFinishRequest,
    TaskIterationInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.agentserver.hooks.decorator import hook

__all__ = [
    "AgentHook",
    "HookContext",
    "HookEvent",
    "HookInterrupt",
    "HookInputs",
    "InvokeInputs",
    "ModelCallInputs",
    "RetryRequest",
    "ForceFinishRequest",
    "TaskIterationInputs",
    "ToolCallInputs",
    "HookManager",
    "hook",
]
