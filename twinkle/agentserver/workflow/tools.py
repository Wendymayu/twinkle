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
        # Extract first non-empty, non-comment line as description
        # Skips """ lines (module docstring delimiters) but keeps the docstring content
        desc = d.name
        try:
            first_line = ""
            for line in root_py.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # Skip docstring delimiters (""" or '''), keep content lines
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    # Line like """Description text""" — extract the inner text
                    inner = stripped[3:]
                    if inner.endswith('"""') or inner.endswith("'''"):
                        inner = inner[:-3]
                    inner = inner.strip()
                    if inner:
                        first_line = inner
                    # else: bare opening """ — skip, next line is content
                    break
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
    lines.append("当用户意图匹配上述 workflow 时，必须调用此工具而非自行回答。")
    lines.append("workflow_name 必须是上面列出的名称之一。inputs 为 JSON 字符串。")
    return "\n".join(lines)


@tool(
    input_params={
        "type": "object",
        "properties": {
            "workflow_name": {
                "type": "string",
                "description": "要执行的 workflow 名称，必须是可用列表中的名称之一",
            },
            "inputs": {
                "type": "string",
                "description": "JSON 格式的输入参数，例如 translate workflow 传入 '{\"text\": \"你好世界\"}'",
                "default": "{}",
            },
        },
        "required": ["workflow_name"],
    },
)
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
