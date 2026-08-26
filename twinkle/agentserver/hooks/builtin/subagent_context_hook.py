"""SubagentContextHook — 在 run_stream 入口设置 subagent 的 ContextVar 桥接。

由 create_agent 自动装配（它构造 executor 并传到此处 —
镜像 jiuwenswarm 的 adapter 把 executor 绑到其 stream rail）。
before_invoke 每次 run_stream 触发一次（与 run_stream 自身设置
PLAN_TODO_SESSION_ID 等的同一入口），把 executor + 父
session/request id 写入 ContextVar，供无参的 spawn_subagent
tool 在运行时读取。仅注册在 PARENT loop 上；子 loop 没有
spawn_subagent，故永不读取这些。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.tools.builtin.subagent.context import (
    SUBAGENT_EXECUTOR,
    SUBAGENT_PARENT_REQUEST_ID,
    SUBAGENT_PARENT_SESSION_ID,
)

if TYPE_CHECKING:
    from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor


class SubagentContextHook(AgentHook):
    priority = 50

    def __init__(self, executor: "SubagentExecutor") -> None:
        self._executor = executor

    async def before_invoke(self, ctx: HookContext) -> None:
        SUBAGENT_EXECUTOR.set(self._executor)
        SUBAGENT_PARENT_SESSION_ID.set(ctx.session_id)
        SUBAGENT_PARENT_REQUEST_ID.set(ctx.request_id)
