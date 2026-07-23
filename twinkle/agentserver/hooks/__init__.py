"""Twinkle Hook mechanism — public API.

Mirrors jiuwen's Rail system with Hook naming.
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
