"""当前请求的 channel 上下文(照抄 plan_todo_context.py 的形态)。

由 AgentLoop._inner_run_stream 入口设定,使无参的 PermissionHook 回调能定位
当前通道(engine.check 需要 channel 做通道门判定)。
"""
from __future__ import annotations

import contextvars

APPROVAL_CHANNEL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "twinkle_approval_channel", default="default")


def get_permission_channel() -> str:
    return APPROVAL_CHANNEL.get() or "default"


def set_permission_channel(channel: str) -> contextvars.Token:
    return APPROVAL_CHANNEL.set(channel or "default")
