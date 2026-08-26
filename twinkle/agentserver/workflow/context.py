"""ContextVar 桥接 — 让 execute_workflow 工具能访问 WorkflowExecutor。"""
from __future__ import annotations

from contextvars import ContextVar

# None = 不在 workflow 上下文中；由 WorkflowContextHook 设置
workflow_executor_ctx: ContextVar["WorkflowExecutor | None"] = ContextVar(
    "workflow_executor_ctx", default=None
)
