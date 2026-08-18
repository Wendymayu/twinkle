# twinkle/agentserver/hooks/builtin/runtime_env_hook.py
"""RuntimeEnvHook — before_model_call 把易变 env(today/os)放 ctx.extra['environment_context']。

env 不进 system prompt(用 ctx.extra 不用 ctx.builder)——system 前缀字节稳定,
provider 端 prefix cache 不被每步/每日变动的 today 破坏。loop 端 pop() 拼尾部
<environment_context> UserMessage。UserMessage 不 SystemMessage:多数 provider 把额外
SystemMessage 合并进 system 参数破坏前缀 cache 稳定性(jiuwenswarm 明示理由)。
"""
from __future__ import annotations

import datetime
import sys

from twinkle.agentserver.hooks.base import AgentHook, HookContext


class RuntimeEnvHook(AgentHook):
    priority = 99  # before_model_call 最先跑(高于 ContextCompression 95 / Skill 90 / Memory 80)

    async def before_model_call(self, ctx: HookContext) -> None:
        content = (
            f"当前平台：`{sys.platform}`\n"
            f"当前日期：`{datetime.date.today().isoformat()}`"
        )
        ctx.extra.setdefault("environment_context", []).append(
            {"content": content, "source": "runtime_env"})
