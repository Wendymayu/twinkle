"""Workflow tool — execute_workflow entry point + WorkflowContextHook.

The @tool function reads the WorkflowExecutor from the workflow_executor_ctx
ContextVar (set by WorkflowContextHook before each ReAct iteration). The hook
is auto-wired in build_agent_loop, mirroring SubagentContextHook/SubagentExecutor.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.workflow.context import workflow_executor_ctx

if TYPE_CHECKING:
    from twinkle.agentserver.workflow.executor import WorkflowExecutor

log = logging.getLogger("twinkle.workflow")


@tool
async def execute_workflow(workflow_name: str, inputs: str = "{}") -> str:
    """Execute a predefined workflow for structured multi-step tasks."""
    executor = workflow_executor_ctx.get()
    if executor is None:
        return "Error: WorkflowExecutor 未初始化"

    # Load plan_code from <WORKSPACE>/workflows/<workflow_name>/root.py
    from twinkle.config import settings
    workspace_dir = settings.workspace.dir
    plan_path = Path(workspace_dir) / "workflows" / workflow_name / "root.py"
    if not plan_path.is_file():
        return f"Error: workflow not found at {plan_path}"

    plan_code = plan_path.read_text(encoding="utf-8")

    # Parse inputs from JSON string
    try:
        parsed_inputs = json.loads(inputs)
    except json.JSONDecodeError as exc:
        return f"Error: invalid inputs JSON: {exc}"

    # Execute
    try:
        result = await executor.execute_workflow(plan_code, parsed_inputs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        return f"Error: {exc}"


class WorkflowContextHook(AgentHook):
    """Sets workflow_executor_ctx ContextVar before each ReAct iteration."""

    priority = 50

    def __init__(self, executor: WorkflowExecutor) -> None:
        self._executor = executor

    async def before_invoke(self, ctx: HookContext) -> None:
        workflow_executor_ctx.set(self._executor)
