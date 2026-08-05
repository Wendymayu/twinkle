"""Subagent package — SubagentExecutor + spawn_subagent tool + models.

Lives under tools/builtin/ (sub-package, not a flat module, because subagent
spans an executor + models + a tool). Subagent is always on; create_agent
builds the executor + registers spawn_subagent + auto-wires SubagentContextHook
(which holds the executor, mirroring jiuwenswarm binding the executor onto its
stream rail). SubagentContextHook itself lives in hooks/builtin/.
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
