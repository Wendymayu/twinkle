"""Hook 机制核心类型 — HookEvent、AgentHook 基类、
HookContext、HookInputs 与控制流信号。

镜像 jiuwen 的 AgentCallbackEvent + AgentRail，为 Twinkle 的
学习向重实现适配，采用 Hook 命名。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Union


class HookEvent(enum.Enum):
    """Agent 执行循环中的生命周期事件 — hook 触发点。

    11 个值与 jiuwen 的 AgentCallbackEvent 一一对应。
    8 个当前会触发；3 个预留给未来使用。
    """
    BEFORE_INVOKE = "before_invoke"
    AFTER_INVOKE = "after_invoke"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    ON_MODEL_EXCEPTION = "on_model_exception"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_TOOL_EXCEPTION = "on_tool_exception"
    # 预留 — 当前 AgentLoop 不触发，但保留以对应 jiuwen
    AFTER_REACT_ITERATION = "after_react_iteration"
    BEFORE_TASK_ITERATION = "before_task_iteration"
    AFTER_TASK_ITERATION = "after_task_iteration"


# 生命周期方法名 → HookEvent 的映射
_EVENT_METHOD_MAP: dict[str, HookEvent] = {
    "before_invoke": HookEvent.BEFORE_INVOKE,
    "after_invoke": HookEvent.AFTER_INVOKE,
    "before_model_call": HookEvent.BEFORE_MODEL_CALL,
    "after_model_call": HookEvent.AFTER_MODEL_CALL,
    "on_model_exception": HookEvent.ON_MODEL_EXCEPTION,
    "before_tool_call": HookEvent.BEFORE_TOOL_CALL,
    "after_tool_call": HookEvent.AFTER_TOOL_CALL,
    "on_tool_exception": HookEvent.ON_TOOL_EXCEPTION,
    "after_react_iteration": HookEvent.AFTER_REACT_ITERATION,
    "before_task_iteration": HookEvent.BEFORE_TASK_ITERATION,
    "after_task_iteration": HookEvent.AFTER_TASK_ITERATION,
}


class AgentHook:
    """所有 agent 生命周期 hook 的基类。

    一个 Hook 是一个"能力束" — 它把多个生命周期 callback 聚合成一个类，
    共享一个 priority。子类化并只覆盖你关心的方法；其余都是 no-op，
    get_callbacks() 会自动跳过它们。

    镜像 jiuwen 的 AgentRail。
    """
    priority: int = 50  # 执行顺序：数值大者先跑

    def init(self, agent: Any) -> None:
        """本 hook 注册到 agent 上时调用。用于初始化
       （如存 agent 引用、读 config）。"""
        ...

    def uninit(self, agent: Any) -> None:
        """本 hook 注销时调用。用于清理。"""
        ...

    # 11 个生命周期 callback — 默认全为 no-op
    async def before_invoke(self, ctx: Any) -> None: ...
    async def after_invoke(self, ctx: Any) -> None: ...
    async def before_model_call(self, ctx: Any) -> None: ...
    async def after_model_call(self, ctx: Any) -> None: ...
    async def on_model_exception(self, ctx: Any) -> None: ...
    async def before_tool_call(self, ctx: Any) -> None: ...
    async def after_tool_call(self, ctx: Any) -> None: ...
    async def on_tool_exception(self, ctx: Any) -> None: ...
    async def after_react_iteration(self, ctx: Any) -> None: ...
    async def before_task_iteration(self, ctx: Any) -> None: ...
    async def after_task_iteration(self, ctx: Any) -> None: ...

    def _is_base_method(self, method: Callable) -> bool:
        """若 *method* 是基类默认实现（未被覆盖）则返回 True。

        将本实例上解析到的方法，与 AgentHook 上同名方法对比。
        若是同一个 function 对象，说明子类未覆盖它。
        """
        name = method.__func__.__name__ if hasattr(method, "__func__") else method.__name__
        base_method = getattr(AgentHook, name, None)
        if base_method is None:
            return False  # 不是已知的生命周期方法
        actual = method.__func__ if hasattr(method, "__func__") else method
        return actual is base_method

    def get_callbacks(self) -> dict[HookEvent, Callable]:
        """只返回子类实际覆盖的生命周期方法对应的 {HookEvent: bound_method}。
        init/uninit 不在内。
        """
        callbacks: dict[HookEvent, Callable] = {}
        for name, event in _EVENT_METHOD_MAP.items():
            method = getattr(self, name)
            if not self._is_base_method(method):
                callbacks[event] = method
        return callbacks


# --- HookInputs（按阶段的类型化数据）--- #


@dataclass
class InvokeInputs:
    """BEFORE/AFTER_INVOKE 事件的输入。"""
    query: str
    mode: str = ""  # "" = 普通，"team" = 团队协作
    envelope: Any = None  # E2AEnvelope — 用 Any 避免循环 import；已弃用，优先用 AgentRequest


@dataclass
class ModelCallInputs:
    """BEFORE/AFTER/ON_MODEL_CALL 事件的输入。"""
    messages: list[dict]
    tools: list[dict]


@dataclass
class ToolCallInputs:
    """BEFORE/AFTER/ON_TOOL_CALL 事件的输入。"""
    name: str
    args: dict
    tool_call_id: str


@dataclass
class TaskIterationInputs:
    """BEFORE/AFTER_TASK_ITERATION 事件的输入（预留）。"""
    envelope: Any


# 所有 inputs 的 Union 类型
HookInputs = Union[InvokeInputs, ModelCallInputs, ToolCallInputs, TaskIterationInputs]


# --- 控制流信号 --- #

@dataclass
class RetryRequest:
    """信号：hook 请求重试当前步骤（如上下文溢出恢复先压缩上下文再请求重调 LLM）。"""
    delay: float = 0


@dataclass
class ForceFinishRequest:
    """信号：hook 请求跳过当前步骤并立即返回结果（如安全拦截）。"""
    result: Any = None


class HookInterrupt(Exception):
    """信号：hook 立即中断执行（如 HITL 审批）。

    对应 jiuwen 的 ToolInterruptException。当前 roadmap 未实现
    permissions，但接口形态预留。
    """
    def __init__(self, message: str = "", data: dict | None = None):
        super().__init__(message)
        self.data = data or {}


# --- HookContext（统一数据包）--- #

@dataclass
class HookContext:
    """传给每个 hook callback 的 context 对象。

    承载：当前事件、按阶段的 inputs、session/request ID、
    用于 hook 间通信的共享 extra dict、异常信息，
    以及控制流信号方法。
    """
    agent: Any  # AgentLoop 引用 — 用 Any 避免循环 import
    event: HookEvent
    inputs: HookInputs
    session_id: str | None
    request_id: str | None
    extra: dict = field(default_factory=dict)
    builder: Any = None  # SystemPromptBuilder | None — loop 每步赋,hook 读
    exception: Exception | None = None
    retry_attempt: int = 0

    # 内部信号字段 — 不属于公共 API 面
    _retry_request: RetryRequest | None = field(default=None, repr=False)
    _force_finish_request: ForceFinishRequest | None = field(default=None, repr=False)

    def request_retry(self, delay: float = 0) -> None:
        """hook 在本 callback 结束后请求重试当前步骤。"""
        self._retry_request = RetryRequest(delay=delay)

    def request_force_finish(self, result: Any = None) -> None:
        """hook 请求跳过方法体并返回 *result*。"""
        self._force_finish_request = ForceFinishRequest(result=result)

    def consume_retry_request(self) -> RetryRequest | None:
        """调用方消费 retry 信号 — 返回它并清空。"""
        req = self._retry_request
        self._retry_request = None
        return req

    def consume_force_finish_request(self) -> ForceFinishRequest | None:
        """调用方消费 force-finish 信号 — 返回它并清空。"""
        req = self._force_finish_request
        self._force_finish_request = None
        return req

    def is_force_finish_requested(self) -> bool:
        """只读窥探 force_finish(不清空)。

        供 before 链中低 priority hook 观察跳过意图:高 priority hook(如
        PermissionHook 权限 deny)已 request_force_finish 后,低 priority hook 可
        在 before 里据此审计"工具将被跳过"(@hook 跳过方法体,after/on_exception
        都不触发)。只读,绝不在此 consume——清空信号会让 @hook 拿不到 force_finish。
        """
        return self._force_finish_request is not None
