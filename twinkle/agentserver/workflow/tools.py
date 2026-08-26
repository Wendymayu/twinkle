"""Workflow tool — execute_workflow 入口 + WorkflowContextHook。

@tool 函数从 workflow_executor_ctx ContextVar 读取 WorkflowExecutor
（由 WorkflowContextHook 在每次 ReAct 迭代前设置）。该 hook 在
build_agent_loop 中自动接入，对照 SubagentContextHook/SubagentExecutor。

tool 描述在注册时动态生成，列出可用 workflow，
以便 LLM 知道可以调用哪些。
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
    """扫描 <WORKSPACE>/workflows/*/root.py 获取可用 workflow。

    返回 {workflow_name: description_line}，用于 tool 描述。
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
        # 提取第一个非空、非注释行作为描述
        # 跳过 """ 行（模块 docstring 定界符），但保留 docstring 内容
        desc = d.name
        try:
            first_line = ""
            for line in root_py.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # 跳过 docstring 定界符（""" 或 '''），保留内容行
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    # 形如 """描述文本""" 的行 — 提取内部文本
                    inner = stripped[3:]
                    if inner.endswith('"""') or inner.endswith("'''"):
                        inner = inner[:-3]
                    inner = inner.strip()
                    if inner:
                        first_line = inner
                    # 否则：裸 """ 开头 — 跳过，下一行是内容
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
    """构建动态 tool 描述，列出可用 workflow。"""
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
    """动态替换 — 见 _build_tool_description()。"""
    executor = workflow_executor_ctx.get()
    if executor is None:
        return "Error: WorkflowExecutor 未初始化"

    # 校验 workflow_name — 防止路径穿越
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_name):
        return f"Error: invalid workflow name: {workflow_name}"

    # 从 <WORKSPACE>/workflows/<workflow_name>/root.py 加载 plan_code
    from twinkle.config import settings
    workspace_dir = settings.workspace.dir
    workflows_root = (Path(workspace_dir) / "workflows").resolve()
    plan_path = (workflows_root / workflow_name / "root.py").resolve()
    if not str(plan_path).startswith(str(workflows_root)):
        return f"Error: invalid workflow path"
    if not plan_path.is_file():
        return f"Error: workflow not found: {workflow_name}"

    plan_code = plan_path.read_text(encoding="utf-8")

    # 从 JSON 字符串解析 inputs
    try:
        parsed_inputs = json.loads(inputs)
    except json.JSONDecodeError as exc:
        return f"Error: invalid inputs JSON: {exc}"

    # 执行
    try:
        result = await executor.execute_workflow(plan_code, parsed_inputs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        return f"Error: {exc}"


class WorkflowContextHook(AgentHook):
    """在每次 ReAct 迭代前设置 workflow_executor_ctx ContextVar。"""

    priority = 50

    def __init__(self, executor: WorkflowExecutor) -> None:
        self._executor = executor

    async def before_invoke(self, ctx: HookContext) -> None:
        workflow_executor_ctx.set(self._executor)
