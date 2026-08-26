"""PlanNode ABC — 带 fallback 和 HookInterrupt 的递归执行节点。

PlanNode 契约（v1）：

1. 每个节点必须继承 PlanNode 并实现 async _execute(inputs: dict) -> Any。
2. 子类不得覆盖 run()；run() 是带 fallback 的模板方法。
3. 节点初始化必须提供 plan_name (str)、instruction (str)、sub_plans (list[PlanNode])。
4. 节点输入是 dict[str, Any]；输出建议为 dict，至少含 node/status/result。
5. 组合节点通过 self.sub_plans 分发子节点并 await child.run(ctx)。
6. 外部能力只能通过 self.has_tool / self.call_tool / self.call_llm / self.extract_json 访问。
7. 失败时抛出异常；框架自动触发 fallback。
8. 每个 skill_code 必须暴露 root: PlanNode。
9. plan_name 在一个 skill 内应唯一，用于日志、trace 和 fallback 定位。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Union

from twinkle.agentserver.hooks.base import HookInterrupt

__all__ = ["PlanNode"]


class PlanNode(ABC):
    """递归执行节点 — 子类实现 _execute，run 带 fallback。"""

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

        # 回调 — 由 Executor 通过 set_runtime_callbacks 注入
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
        """递归更新所有后代节点的 depth。

        通过对公开属性 depth/sub_plans 做迭代遍历，
        避免调用其他实例的受保护方法。
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
        """注入运行时回调并传播到所有 sub_plans。"""
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

    # --- 能力方法（委托给回调）---

    def has_tool(self, tool_name: str) -> bool:
        """检查某 tool 是否可用。回调未设置时抛 RuntimeError。"""
        if self._has_tool_callback is None:
            raise RuntimeError("PlanNode has_tool callback not initialized")
        return self._has_tool_callback(tool_name)

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """按名字调用 tool。回调未设置时抛 RuntimeError。"""
        if self._call_tool_callback is None:
            raise RuntimeError("PlanNode call_tool callback not initialized")
        return await self._call_tool_callback(tool_name, **kwargs)

    async def call_llm(self, prompt: str, system_prompt: str = "") -> str:
        """调用 LLM。回调未设置时抛 RuntimeError。"""
        if self._call_llm_callback is None:
            raise RuntimeError("PlanNode call_llm callback not initialized")
        return await self._call_llm_callback(prompt, system_prompt=system_prompt)

    def extract_json(self, raw: Union[str, dict, list], expected_type: type = dict) -> Any:
        """从 LLM 输出中提取 JSON。回调未设置时抛 RuntimeError。"""
        if self._extract_json_callback is None:
            raise RuntimeError("PlanNode extract_json callback not initialized")
        return self._extract_json_callback(raw, expected_type)

    # --- 抽象执行 ---

    @abstractmethod
    async def _execute(self, inputs: dict[str, Any]) -> Any:
        """子类必须用节点的核心逻辑实现它。"""
        ...

    # --- 模板方法 ---

    async def run(self, inputs: dict[str, Any]) -> Any:
        """带 fallback 执行。HookInterrupt 永远不会被 fallback 捕获。"""
        try:
            return await self._execute(inputs)
        except HookInterrupt:
            raise
        except Exception as exc:
            if self._fallback_callback is None:
                raise
            return await self._fallback_callback(self, inputs, exc)

    # --- 子计划执行 ---

    async def execute_subplan(self, subplan: PlanNode, inputs: dict[str, Any]) -> Any:
        """带 before/after 回调执行子节点。"""
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        try:
            result = await subplan.run(inputs)

            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, result)

            return result
        except HookInterrupt:
            # HITL 中断：不调用 after_subplan_execute
            raise
        except Exception as exc:
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, exc)
            raise

    # --- Repr ---

    def __repr__(self) -> str:
        return f"PlanNode(name={self.plan_name!r}, sub_plans={len(self.sub_plans)})"
