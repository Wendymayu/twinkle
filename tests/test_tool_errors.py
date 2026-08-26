"""ToolError + format_tool_error —— 工具失败内容的单一收口点。

镜像 openclaw 的 createErrorToolResult + coerceErrorMessage：工具失败时直接抛出
（绝不把错误编码进 content）；循环的 catch 点调用 format_tool_error 渲染统一的 [tool error] 前缀。
"""
from __future__ import annotations

from twinkle.agentserver.tools.errors import ToolError, format_tool_error
from twinkle.observability.attributes import TOOL_ERROR_PREFIX


def test_tool_error_carries_kind_but_str_is_just_message():
    e = ToolError("file_path is required", kind="validation")
    assert str(e) == "file_path is required"
    assert e.kind == "validation"
    assert isinstance(e, Exception)


def test_tool_error_default_kind_is_failed():
    assert ToolError("oops").kind == "failed"


def test_format_tool_error_for_toolerror_is_prefix_plus_message():
    # kind 不得出现在 content 中。
    out = format_tool_error(ToolError("file_path is required", kind="validation"))
    assert out == f"{TOOL_ERROR_PREFIX} file_path is required"
    assert "validation" not in out


def test_format_tool_error_for_unknown_exception_keeps_type_name():
    out = format_tool_error(ValueError("boom"))
    assert out == f"{TOOL_ERROR_PREFIX} ValueError: boom"


def test_format_tool_error_for_str_is_prefix_plus_text():
    out = format_tool_error("tool denied by user: bash — reason")
    assert out == f"{TOOL_ERROR_PREFIX} tool denied by user: bash — reason"


def test_format_tool_error_reuses_constant_not_literal():
    # 生产方必须引用 TOOL_ERROR_PREFIX 而非字面量，这样才不会与可观测消费方
    # （instrumentors/tool.py）发生漂移。
    assert format_tool_error(ToolError("x")).startswith(TOOL_ERROR_PREFIX)
