"""AgentServer tools package + default manager builder.

Framework layer (``Tool`` / ``ToolCard`` / ``LocalFunction`` / ``@tool`` /
``ToolManager``) lives here at the top level; concrete tool implementations
live in the :mod:`twinkle.agentserver.tools.builtin` subpackage. Add a new
tool under ``builtin/``, then register it in :func:`tool_manager`.
"""
from __future__ import annotations

from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.builtin import command_exec, todo_tools, web_fetch, web_search
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def tool_manager() -> ToolManager:
    """Build a ToolManager pre-loaded with the default tools."""
    tm = ToolManager()
    tm.register(web_fetch.web_fetch)
    tm.register(web_search.web_search)
    tm.register(command_exec.command_exec)
    tm.register(todo_tools.todo_create)
    tm.register(todo_tools.todo_complete)
    tm.register(todo_tools.todo_list)
    return tm


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "tool_manager",
]
