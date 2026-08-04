"""PlanNode ABC — recursive execution node with fallback and HookInterrupt.

PlanNode contract (v1):

1. Each node must inherit PlanNode and implement async _execute(inputs: dict) -> Any.
2. Subclasses must not override run(); run() is the template method with fallback.
3. Node init must provide plan_name (str), instruction (str), sub_plans (list[PlanNode]).
4. Node input is dict[str, Any]; output recommended as dict with at least node/status/result.
5. Composite nodes dispatch children via self.sub_plans and await child.run(ctx).
6. External capabilities accessed only via self.has_tool / self.call_tool / self.call_llm / self.extract_json.
7. On failure, raise the exception; the framework triggers fallback automatically.
8. Each skill_code must expose root: PlanNode.
9. plan_name should be unique within a skill for logging, trace, and fallback targeting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Union

from twinkle.agentserver.hooks.base import HookInterrupt

__all__ = ["PlanNode"]


class PlanNode(ABC):
    """Recursive execution node — subclass implements _execute, run has fallback."""

    def __init__(
        self,
        plan_name: str,
        instruction: str,
        sub_plans: list[PlanNode] | None = None,
        depth: int = 0,
    ):
        self.plan_name = plan_name
        self.instruction = instruction
        self.depth = depth
        self.sub_plans = sub_plans or []

        self._update_subplans_depth()

        # Callbacks — injected by Executor via set_runtime_callbacks
        self._has_tool_callback: Callable[[str], bool] | None = None
        self._call_tool_callback: Callable[..., Awaitable[Any]] | None = None
        self._call_llm_callback: Callable[..., Awaitable[str]] | None = None
        self._fallback_callback: (
            Callable[[PlanNode, dict[str, Any], Exception], Awaitable[Any]] | None
        ) = None
        self._extract_json_callback: Callable[..., Any] | None = None
        self._before_subplan_execute: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[None]] | None
        ) = None
        self._after_subplan_execute: (
            Callable[[PlanNode, dict[str, Any], Any], Awaitable[None]] | None
        ) = None

    def _update_subplans_depth(self) -> None:
        """Recursively update depth for all descendant nodes.

        Uses iterative traversal over public attributes depth/sub_plans,
        avoiding calling protected methods on other instances.
        """
        pending = [(sub, self.depth + 1) for sub in self.sub_plans]
        while pending:
            node, node_depth = pending.pop()
            node.depth = node_depth
            pending.extend((child, node_depth + 1) for child in node.sub_plans)

    def set_runtime_callbacks(
        self,
        *,
        has_tool: Callable[[str], bool] | None = None,
        call_tool: Callable[..., Awaitable[Any]] | None = None,
        call_llm: Callable[..., Awaitable[str]] | None = None,
        fallback: Callable[[PlanNode, dict[str, Any], Exception], Awaitable[Any]] | None = None,
        extract_json: Callable[..., Any] | None = None,
        before_subplan_execute: Callable[[PlanNode, dict[str, Any]], Awaitable[None]] | None = None,
        after_subplan_execute: Callable[[PlanNode, dict[str, Any], Any], Awaitable[None]] | None = None,
    ) -> None:
        """Inject runtime callbacks and propagate to all sub_plans."""
        if has_tool is not None:
            self._has_tool_callback = has_tool
        if call_tool is not None:
            self._call_tool_callback = call_tool
        if call_llm is not None:
            self._call_llm_callback = call_llm
        if fallback is not None:
            self._fallback_callback = fallback
        if extract_json is not None:
            self._extract_json_callback = extract_json
        if before_subplan_execute is not None:
            self._before_subplan_execute = before_subplan_execute
        if after_subplan_execute is not None:
            self._after_subplan_execute = after_subplan_execute

        for node in self.sub_plans:
            node.set_runtime_callbacks(
                has_tool=has_tool,
                call_tool=call_tool,
                call_llm=call_llm,
                fallback=fallback,
                extract_json=extract_json,
                before_subplan_execute=before_subplan_execute,
                after_subplan_execute=after_subplan_execute,
            )

    # --- Capability methods (delegate to callbacks) ---

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is available. Raises RuntimeError if callback not set."""
        if self._has_tool_callback is None:
            raise RuntimeError("PlanNode has_tool callback not initialized")
        return self._has_tool_callback(tool_name)

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """Call a tool by name. Raises RuntimeError if callback not set."""
        if self._call_tool_callback is None:
            raise RuntimeError("PlanNode call_tool callback not initialized")
        return await self._call_tool_callback(tool_name, **kwargs)

    async def call_llm(self, prompt: str, system_prompt: str = "") -> str:
        """Call LLM. Raises RuntimeError if callback not set."""
        if self._call_llm_callback is None:
            raise RuntimeError("PlanNode call_llm callback not initialized")
        return await self._call_llm_callback(prompt, system_prompt=system_prompt)

    def extract_json(self, raw: Union[str, dict, list], expected_type: type = dict) -> Any:
        """Extract JSON from LLM output. Raises RuntimeError if callback not set."""
        if self._extract_json_callback is None:
            raise RuntimeError("PlanNode extract_json callback not initialized")
        return self._extract_json_callback(raw, expected_type)

    # --- Abstract execution ---

    @abstractmethod
    async def _execute(self, inputs: dict[str, Any]) -> Any:
        """Subclass must implement this with the node's core logic."""
        ...

    # --- Template method ---

    async def run(self, inputs: dict[str, Any]) -> Any:
        """Execute with fallback. HookInterrupt is never caught by fallback."""
        try:
            return await self._execute(inputs)
        except HookInterrupt:
            raise
        except Exception as exc:
            if self._fallback_callback is None:
                raise
            return await self._fallback_callback(self, inputs, exc)

    # --- Sub-plan execution ---

    async def execute_subplan(self, subplan: PlanNode, inputs: dict[str, Any]) -> Any:
        """Execute a child node with before/after callbacks."""
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        try:
            result = await subplan.run(inputs)

            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, result)

            return result
        except HookInterrupt:
            # HITL interrupt: do not call after_subplan_execute
            raise
        except Exception as exc:
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, exc)
            raise

    # --- Repr ---

    def __repr__(self) -> str:
        return f"PlanNode(name={self.plan_name!r}, sub_plans={len(self.sub_plans)})"
