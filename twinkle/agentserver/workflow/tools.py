"""Workflow tool — execute_workflow entry point + WorkflowContextHook.

The @tool function reads the WorkflowExecutor from the workflow_executor_ctx
ContextVar (set by WorkflowContextHook before each ReAct iteration). The hook
is auto-wired in build_agent_loop, mirroring SubagentContextHook/SubagentExecutor.

The tool description is dynamically generated at registration time to list
available workflows, so the LLM knows which ones it can call.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.workflow.context import workflow_executor_ctx

if TYPE_CHECKING:
    from twinkle.agentserver.workflow.executor import WorkflowExecutor


def _scan_workflows() -> dict[str, str]:
    """Scan <WORKSPACE>/workflows/*/root.py for available workflows.

    Returns: {workflow_name: description_line} for tool description.
    """
    try:
        from twinkle.config import settings
        workspace_dir = settings.workspace.dir
    except Exception:
        return {}
    workflows_root = Path(workspace_dir) / "workflows"
    if not workflows_root.is_dir():
        return {}

    result: dict[str, str] = {}
    for d in sorted(workflows_root.iterdir()):
        if not d.is_dir():
            continue
        root_py = d / "root.py"
        if not root_py.is_file():
            continue
        # Extract first non-empty docstring line as description
        desc = d.name
        try:
            first_line = ""
            for line in root_py.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith('"""') and not stripped.startswith("#"):
                    first_line = stripped
                    break
            if first_line:
                desc = first_line
        except Exception:
            pass
        result[d.name] = desc
    return result


def _build_tool_description() -> str:
    """Build dynamic tool description listing available workflows."""
    workflows = _scan_workflows()
    if not workflows:
        return "执行预定义的 Workflow，用于结构化多步骤任务。（当前无可用 workflow）"
    lines = [
        "执行预定义的 Workflow，用于结构化多步骤任务。",
        "",
        "可用 workflow：",
    ]
    for name, desc in workflows.items():
        lines.append(f"  - {name}: {desc}")
    lines.append("")
    lines.append("当用户意图匹配上述 workflow 时，优先调用此工具。")
    return "\n".join(lines)


@tool
async def execute_workflow(workflow_name: str, inputs: str = "{}") -> str:
    """Dynamically replaced — see _build_tool_description()."""
    executor = workflow_executor_ctx.get()
    if executor is None:
        return "Error: WorkflowExecutor 未初始化"

    # Validate workflow_name — prevent path traversal
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_name):
        return f"Error: invalid workflow name: {workflow_name}"

    # Load plan_code from <WORKSPACE>/workflows/<workflow_name>/root.py
    from twinkle.config import settings
    workspace_dir = settings.workspace.dir
    workflows_root = (Path(workspace_dir) / "workflows").resolve()
    plan_path = (workflows_root / workflow_name / "root.py").resolve()
    if not str(plan_path).startswith(str(workflows_root)):
        return f"Error: invalid workflow path"
    if not plan_path.is_file():
        return f"Error: workflow not found: {workflow_name}"

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
