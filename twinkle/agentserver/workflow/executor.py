"""WorkflowExecutor — orchestration core that wires validation, sandbox, and fallback.

Validates plan_code, loads it in a sandboxed namespace, extracts the root PlanNode,
binds runtime callbacks, and executes with timeout/fallback support.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import TYPE_CHECKING, Any

from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.workflow.context import workflow_executor_ctx
from twinkle.agentserver.workflow.json_utils import extract_llm_json
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.workflow.sandbox import build_namespace
from twinkle.agentserver.workflow.validator import PlanCodeValidator
from twinkle.config.schema import WorkflowConfig

if TYPE_CHECKING:
    from twinkle.agentserver.tools.builtin.subagent.executor import SubagentExecutor

log = logging.getLogger("twinkle.workflow")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PlanCodeValidationError(Exception):
    """plan_code fails validation."""


class ExecutionTimeoutError(Exception):
    """Execution exceeds timeout."""


class FallbackLimitExceededError(Exception):
    """Fallback count exceeds limit."""


# ---------------------------------------------------------------------------
# WorkflowExecutor
# ---------------------------------------------------------------------------

class WorkflowExecutor:
    """Orchestration core: validate → load → bind callbacks → execute (with timeout)."""

    def __init__(
        self,
        llm: LLMClient | None,
        tools: ToolManager | None,
        subagent_executor: SubagentExecutor | None,
        config: WorkflowConfig,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._subagent_executor = subagent_executor
        self._config = config
        self._fallback_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_workflow(self, plan_code: str, inputs: dict) -> Any:
        """Validate → load → bind callbacks → execute with timeout."""
        root = self._prepare_root_node(plan_code)

        token = workflow_executor_ctx.set(self)
        try:
            self._fallback_count = 0
            return await asyncio.wait_for(
                root.run(inputs),
                timeout=self._config.execution_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ExecutionTimeoutError(
                f"Workflow exceeded {self._config.execution_timeout}s"
            ) from exc
        finally:
            workflow_executor_ctx.reset(token)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    def _prepare_root_node(self, plan_code: str) -> PlanNode:
        """Validate → load → extract root → deep copy → bind callbacks."""
        errors = PlanCodeValidator().validate(plan_code)
        if errors:
            raise PlanCodeValidationError(
                f"Plan code validation failed: {errors}"
            )

        namespace = self._load_plan_namespace(plan_code)
        root = self._extract_root_node(namespace)
        root = copy.deepcopy(root)
        self._bind_node_callbacks(root)
        return root

    def _load_plan_namespace(self, plan_code: str) -> dict:
        """exec(plan_code, sandboxed_namespace) and return the namespace."""
        namespace = build_namespace()
        exec(plan_code, namespace)
        return namespace

    def _extract_root_node(self, namespace: dict) -> PlanNode:
        """Extract 'root' PlanNode from namespace."""
        root = namespace.get("root")
        if root is None:
            raise PlanCodeValidationError(
                "Plan code must define a 'root' variable of type PlanNode"
            )
        if not isinstance(root, PlanNode):
            raise PlanCodeValidationError(
                f"'root' must be a PlanNode, got {type(root).__name__}"
            )
        return root

    def _bind_node_callbacks(self, root: PlanNode) -> None:
        """Inject all runtime callbacks into the root node (and sub_plans)."""
        root.set_runtime_callbacks(
            has_tool=self._has_tool_wrapper,
            call_tool=self._call_tool_wrapper,
            call_llm=self._call_llm_wrapper,
            fallback=self._fallback_wrapper,
            extract_json=self._extract_json_wrapper,
        )

    # ------------------------------------------------------------------
    # Callback wrappers
    # ------------------------------------------------------------------

    def _has_tool_wrapper(self, tool_name: str) -> bool:
        """Delegate to ToolManager.get()."""
        if self._tools is None:
            return False
        return self._tools.get(tool_name) is not None

    async def _call_tool_wrapper(self, tool_name: str, **kwargs: Any) -> Any:
        """Delegate to ToolManager.execute(), try JSON parse."""
        if self._tools is None:
            raise RuntimeError(f"ToolManager not available for tool: {tool_name}")
        result = await self._tools.execute(tool_name, kwargs)
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result

    async def _call_llm_wrapper(self, prompt: str, system_prompt: str = "") -> str:
        """LLMClient.stream() + collect TextDelta."""
        if self._llm is None:
            raise RuntimeError("LLMClient not available")
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        collected: list[str] = []
        async for event in self._llm.stream(messages, tools=[]):
            # Import locally to avoid circular at module level
            from twinkle.agentserver.llm_client import TextDelta
            if isinstance(event, TextDelta):
                collected.append(event.content)
        return "".join(collected)

    async def _fallback_wrapper(
        self, node: PlanNode, inputs: dict[str, Any], exc: Exception
    ) -> Any:
        """Delegate to SubagentExecutor, track count."""
        if not self._config.enable_fallback:
            raise exc

        self._fallback_count += 1
        if self._fallback_count > self._config.max_fallback_count:
            raise FallbackLimitExceededError(
                f"Fallback limit exceeded: {self._fallback_count} > "
                f"{self._config.max_fallback_count}"
            ) from exc

        if self._subagent_executor is None:
            raise exc

        from twinkle.agentserver.tools.builtin.subagent.models import SubagentTaskSpec

        task = SubagentTaskSpec(
            objective=node.instruction,
            prompt=f"Node '{node.plan_name}' failed: {exc}",
        )
        result = await self._subagent_executor.execute_subagent(
            task,
            parent_session_id="__workflow__",
            parent_request_id="__workflow__",
        )
        if result.success:
            return result.result
        raise RuntimeError(f"Subagent fallback failed: {result.error}") from exc

    def _extract_json_wrapper(self, raw: Any, expected_type: type = dict) -> Any:
        """Delegate to extract_llm_json."""
        return extract_llm_json(raw, expected_type)
