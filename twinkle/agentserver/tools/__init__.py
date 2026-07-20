"""AgentServer tools package + default manager builder."""
from __future__ import annotations

from twinkle.agentserver.tools import web_fetch, web_search
from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def default_tool_manager() -> ToolManager:
    """Build a ToolManager pre-loaded with the default read-only tools."""
    tool_manager = ToolManager()
    tool_manager.register(tool(web_fetch.web_fetch))
    tool_manager.register(tool(web_search.web_search))
    return tool_manager


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "default_tool_manager",
]
