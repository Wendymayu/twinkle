"""AgentServer tools package + default manager builder."""
from __future__ import annotations

from twinkle.agentserver.tools import command_exec, web_fetch, web_search
from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def tool_manager() -> ToolManager:
    """Build a ToolManager pre-loaded with the default read-only tools."""
    tm = ToolManager()
    tm.register(tool(web_fetch.web_fetch))
    tm.register(tool(web_search.web_search))
    tm.register(tool(command_exec.command_exec))
    return tm


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "tool_manager",
]
