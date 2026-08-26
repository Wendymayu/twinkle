"""Subagent 包 —— SubagentExecutor + spawn_subagent tool + models。

归在 tools/builtin/ 下(子包而非扁平模块,因为 subagent 跨一个
executor + models + 一个 tool)。Subagent 始终开启;create_agent
构建 executor + 注册 spawn_subagent + 自动接线 SubagentContextHook
(它持有 executor,对照 jiuwenswarm 把 executor 绑到其 stream rail)。
SubagentContextHook 本身在 hooks/builtin/ 中。
"""
from twinkle.agentserver.tools.builtin.subagent.executor import (
    SubagentExecutor,
    create_subagent_executor,
)
from twinkle.agentserver.tools.builtin.subagent.models import (
    EXCLUDED_TOOLS,
    SoftTimeoutError,
    SubagentResult,
    SubagentTaskSpec,
)
from twinkle.agentserver.tools.builtin.subagent.tools import spawn_subagent

__all__ = [
    "SubagentExecutor",
    "create_subagent_executor",
    "spawn_subagent",
    "SubagentTaskSpec",
    "SubagentResult",
    "EXCLUDED_TOOLS",
    "SoftTimeoutError",
]
