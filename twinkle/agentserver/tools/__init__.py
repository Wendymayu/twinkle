"""AgentServer tools package + default manager builder."""
from __future__ import annotations

from twinkle.agentserver.tools import web_fetch, web_search
from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def build_default_manager() -> ToolManager:
    """Register the default read-only tools via the @tool decorator."""
    m = ToolManager()
    m.register(tool(web_fetch.web_fetch))
    m.register(tool(web_search.web_search))
    return m


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "build_default_manager",
]
