"""Tool failure primitives — the single chokepoint for tool-error content.

Aligned with openclaw's contract: "Throw on failure instead of encoding
errors in `content`." Tools raise ToolError on failure, return str on success.
The agent loop's catch points call format_tool_error to render one unified
``[tool error]`` prefix (reusing TOOL_ERROR_PREFIX so producer and the
observability consumer cannot drift).

Why no numeric StatusCode (jiuwenswarm ~250-entry enum) and no
ToolResult{content,details} (openclaw) or ToolOutput{success,data,error}
(jiuwenswarm): Twinkle is a slim learning reimplementation on OpenAI
function-calling wire (content is a plain string, no isError field) with no
partial-output soft-error case. A prefix + kind field is the minimum that
solves the problem.
"""
from __future__ import annotations

from twinkle.observability.attributes import TOOL_ERROR_PREFIX


class ToolError(Exception):
    """Raise inside a tool on failure. Never encode errors into return content.

    ``kind`` stays on the exception object for future consumers (RetryHook
    retry-by-kind, observability is_error metadata) — it is NOT rendered into
    content by format_tool_error. Has no current consumer (YAGNI border); kept
    as the zero-cost hand-off point for the deferred observability B-plan.
    """

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def format_tool_error(source: "str | BaseException") -> str:
    """Render any tool failure into the unified ``[tool error] ...`` content.

    - ToolError        -> ``[tool error] {message}``        (kind not rendered)
    - other Exception  -> ``[tool error] {ExcType}: {msg}`` (keep type name for debugging)
    - str              -> ``[tool error] {str}``            (denied etc. built directly in the loop)

    The prefix reuses TOOL_ERROR_PREFIX so the producer cannot drift from the
    observability consumer (instrumentors/tool.py startswith check).
    """
    if isinstance(source, ToolError):
        return f"{TOOL_ERROR_PREFIX} {source}"
    if isinstance(source, BaseException):
        return f"{TOOL_ERROR_PREFIX} {type(source).__name__}: {source}"
    return f"{TOOL_ERROR_PREFIX} {source}"
