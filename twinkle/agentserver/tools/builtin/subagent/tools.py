"""spawn_subagent — delegate an isolated subtask to a fresh child agent (black-box).

Reads the executor + parent session/request id from the subagent_context
ContextVars (set by SubagentContextHook on the parent loop). Runs the child to
convergence, returns its final answer (+ stop hint) as a tool_result string.
"""
from __future__ import annotations

from .context import (
    get_subagent_executor,
    get_subagent_parent_request_id,
    get_subagent_parent_session_id,
)
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.errors import ToolError
from twinkle.agentserver.tools.builtin.subagent.models import (
    SubagentResult,
    SubagentTaskSpec,
)

_SUBAGENT_STOP_HINT = (
    "\n\n[SYSTEM] The delegated task is complete. "
    "Summarize the result to the user and finish your turn. "
    "Do NOT call spawn_subagent again for this task."
)


def _wrap(result: SubagentResult) -> str:
    if result.success:
        return (result.result or "") + _SUBAGENT_STOP_HINT
    return (result.error or "subagent failed") + _SUBAGENT_STOP_HINT


@tool
async def spawn_subagent(objective: str, prompt: str = "") -> str:
    """Delegate an isolated subtask to a fresh sub-agent that runs its own ReAct
    loop in an isolated session and returns only its final answer.

    WHEN to delegate:
    - The subtask is complex / multi-step and benefits from focused ReAct.
    - You want it isolated (fresh context, can't pollute this conversation).
    - Different subtasks are independent (call spawn_subagent once each).

    WHEN NOT to delegate:
    - One tool call or a direct answer suffices — do it yourself.
    - The subtask needs this conversation's history — pass it explicitly in
      `objective` instead (the sub-agent CANNOT see this agent's history).

    `objective` must be self-contained: goal + constraints + all context the
    sub-agent needs (it sees nothing else). `prompt` may carry extra instructions
    (e.g. output format). The sub-agent cannot ask the user; it must converge or
    return a failure note. Its final answer (truncated if huge) becomes your
    tool_result — summarize it to the user; do not re-delegate the same task.
    """
    executor = get_subagent_executor()
    parent_session_id = get_subagent_parent_session_id()
    if executor is None or parent_session_id is None:
        raise ToolError("subagent executor not initialized on this loop", kind="unavailable")
    parent_request_id = get_subagent_parent_request_id() or parent_session_id
    task = SubagentTaskSpec(objective=objective, prompt=prompt)
    result = await executor.execute_subagent(
        task, parent_session_id=parent_session_id, parent_request_id=parent_request_id
    )
    return _wrap(result)
