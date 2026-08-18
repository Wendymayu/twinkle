"""ToolError + format_tool_error — single chokepoint for tool-failure content.

Mirrors openclaw createErrorToolResult + coerceErrorMessage: tools raise on
failure (never encode errors into content); the loop's catch points call
format_tool_error to render one unified [tool error] prefix.
"""
from __future__ import annotations

import pytest

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
    # kind must NOT appear in content.
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
    # The producer must reference TOOL_ERROR_PREFIX, not a literal, so it
    # cannot drift from the observability consumer (instrumentors/tool.py).
    assert format_tool_error(ToolError("x")).startswith(TOOL_ERROR_PREFIX)
