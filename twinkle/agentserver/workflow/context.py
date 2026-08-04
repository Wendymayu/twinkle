"""ContextVar bridge — lets execute_workflow tool access the WorkflowExecutor."""
from __future__ import annotations

from contextvars import ContextVar

# None = not in a workflow context; set by WorkflowContextHook
workflow_executor_ctx: ContextVar["WorkflowExecutor | None"] = ContextVar(
    "workflow_executor_ctx", default=None
)
