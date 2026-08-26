"""WorkflowExecutor — 编排核心，串联校验、sandbox 和 fallback。

校验 plan_code、在沙箱 namespace 中加载它、提取 root PlanNode、
绑定运行时回调，并带超时/fallback 支持执行。
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.workflow.json_utils import extract_llm_json
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.workflow.sandbox import build_namespace
from twinkle.agentserver.workflow.validator import PlanCodeValidator
from twinkle.config.schema import WorkflowConfig

if TYPE_CHECKING:
    from twinkle.agentserver.tools.builtin.subagent.executor import SubagentExecutor


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class PlanCodeValidationError(Exception):
    """plan_code 校验失败。"""


class ExecutionTimeoutError(Exception):
    """执行超过超时。"""


class FallbackLimitExceededError(Exception):
    """fallback 次数超过上限。"""


# ---------------------------------------------------------------------------
# 基础设施错误检测
# ---------------------------------------------------------------------------

# 基础设施错误（LLM API 宕机 / 鉴权 / 限流 / 超时）无法被 subagent fallback
# 挽救 — subagent 调用同一 LLM API，会以同样方式失败，白白浪费 token。
# 按异常的类层级名匹配（而非 message），以避免对仅在文本中提到 "connection"
# 的节点逻辑错误产生误报。
_INFRA_KEYWORDS: tuple[str, ...] = (
    "Connection",
    "Auth",
    "Unauthorized",
    "RateLimit",
    "Timeout",
    "Transport",
)


def _is_infrastructure_error(exc: BaseException) -> bool:
    """若 ``exc`` 是基础设施类错误（网络/鉴权/限流/超时）则返回 True。

    遍历 ``type(exc).__mro__``，使 openai/httpx/asyncio 错误的子类
    （如 ``openai.APIConnectionError``）无需 import 那些 SDK 即可识别 —
    保持 workflow 层与任何 LLM SDK 解耦。
    """
    for cls in type(exc).__mro__:
        if any(keyword in cls.__name__ for keyword in _INFRA_KEYWORDS):
            return True
    return False


# ---------------------------------------------------------------------------
# WorkflowExecutor
# ---------------------------------------------------------------------------

class WorkflowExecutor:
    """编排核心：校验 → 加载 → 绑定回调 → 执行（带超时）。"""

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
    # 公共 API
    # ------------------------------------------------------------------

    async def execute_workflow(self, plan_code: str, inputs: dict) -> Any:
        """校验 → 加载 → 绑定回调 → 带超时执行。"""
        root = self._prepare_root_node(plan_code)

        self._fallback_count = 0
        try:
            return await asyncio.wait_for(
                root.run(inputs),
                timeout=self._config.execution_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ExecutionTimeoutError(
                f"Workflow exceeded {self._config.execution_timeout}s"
            ) from exc

    # ------------------------------------------------------------------
    # 内部流水线
    # ------------------------------------------------------------------

    def _prepare_root_node(self, plan_code: str) -> PlanNode:
        """校验 → 加载 → 提取 root → 深拷贝 → 绑定回调。"""
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
        """exec(plan_code, sandboxed_namespace) 并返回该 namespace。"""
        namespace = build_namespace()
        exec(plan_code, namespace)
        return namespace

    def _extract_root_node(self, namespace: dict) -> PlanNode:
        """从 namespace 中提取 'root' PlanNode。"""
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
        """把所有运行时回调注入 root 节点（及 sub_plans）。"""
        root.set_runtime_callbacks(
            has_tool=self._has_tool_wrapper,
            call_tool=self._call_tool_wrapper,
            call_llm=self._call_llm_wrapper,
            fallback=self._fallback_wrapper,
            extract_json=self._extract_json_wrapper,
        )

    # ------------------------------------------------------------------
    # 回调 wrapper
    # ------------------------------------------------------------------

    def _has_tool_wrapper(self, tool_name: str) -> bool:
        """委托给 ToolManager.get()。"""
        if self._tools is None:
            return False
        return self._tools.get(tool_name) is not None

    async def _call_tool_wrapper(self, tool_name: str, **kwargs: Any) -> Any:
        """委托给 ToolManager.execute()，尝试 JSON 解析。"""
        if self._tools is None:
            raise RuntimeError(f"ToolManager not available for tool: {tool_name}")
        result = await self._tools.execute(tool_name, kwargs)
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result

    async def _call_llm_wrapper(self, prompt: str, system_prompt: str = "") -> str:
        """LLMClient.stream() + 收集 TextDelta。"""
        if self._llm is None:
            raise RuntimeError("LLMClient not available")
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        collected: list[str] = []
        async for event in self._llm.stream(messages, tools=[]):
            # 局部 import 以避免模块级循环引用
            from twinkle.agentserver.llm_client import TextDelta
            if isinstance(event, TextDelta):
                collected.append(event.content)
        return "".join(collected)

    async def _fallback_wrapper(
        self, node: PlanNode, inputs: dict[str, Any], exc: Exception
    ) -> Any:
        """委托给 SubagentExecutor，跟踪计数。"""
        if not self._config.enable_fallback:
            raise exc

        # 基础设施错误（LLM API 连接/鉴权/限流/超时）无法被 subagent 挽救 —
        # 它调用同一 API 会以同样方式失败。重新抛出，使错误到达主 agent 循环
        # （ReAct），后者可在基础设施恢复后重试整个 workflow。
        if _is_infrastructure_error(exc):
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
        """委托给 extract_llm_json。"""
        return extract_llm_json(raw, expected_type)
