"""Tool 失败原语 —— tool-error content 的唯一收口。

对齐 openclaw 的契约:"失败时抛异常,而非把错误编码进 `content`。"
tool 失败时抛 ToolError,成功时返回 str。agent loop 的 catch 点调用
format_tool_error 渲染统一的 ``[tool error]`` 前缀(复用 TOOL_ERROR_PREFIX,
使生产方与可观测消费方不会漂移)。

为何不用数值 StatusCode(jiuwenswarm ~250 项 enum)也不用
ToolResult{content,details}(openclaw)或 ToolOutput{success,data,error}
(jiuwenswarm):Twinkle 是基于 OpenAI function-calling wire 的精简学习
重实现(content 是纯字符串,无 isError 字段),也没有 partial-output
软错误场景。一个前缀 + kind 字段是解决问题的最小方案。
"""
from __future__ import annotations

from twinkle.observability.attributes import TOOL_ERROR_PREFIX


class ToolError(Exception):
    """在 tool 内部失败时抛出。绝不把错误编码进返回 content。

    ``kind`` 留在异常对象上供未来消费方使用(RetryHook 按 kind 重试;
    session-store 的 is_error 元数据 B 计划),format_tool_error 不会把它
    渲染进 content。当前无消费方 —— 现有 instrumentor 依据 content 上的
    TOOL_ERROR_PREFIX 而非 kind。保留它作为零成本的交接点(YAGNI 边界)。
    """

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def format_tool_error(source: str | BaseException) -> str:
    """把任意 tool 失败渲染成统一的 ``[tool error] ...`` content。

    - ToolError        -> ``[tool error] {message}``        (不渲染 kind)
    - 其他 Exception  -> ``[tool error] {ExcType}: {msg}`` (保留类型名便于调试)
    - str              -> ``[tool error] {str}``            (denied 等直接在 loop 里构造)

    前缀复用 TOOL_ERROR_PREFIX,使生产方不会与可观测消费方
    (instrumentors/tool.py 的 startswith 检查)漂移。
    """
    if isinstance(source, ToolError):
        return f"{TOOL_ERROR_PREFIX} {source}"
    if isinstance(source, BaseException):
        return f"{TOOL_ERROR_PREFIX} {type(source).__name__}: {source}"
    return f"{TOOL_ERROR_PREFIX} {source}"
