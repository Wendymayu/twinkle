"""AgentServer tools package + default manager builder.

Framework layer (``Tool`` / ``ToolCard`` / ``LocalFunction`` / ``@tool`` /
``ToolManager``) lives here at the top level; concrete tool implementations
live in the :mod:`twinkle.agentserver.tools.builtin` subpackage. Add a new
tool under ``builtin/``, then register it in :func:`tool_manager`.
"""
from __future__ import annotations

from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.builtin import command_exec, cron_tools, file_tools, memory_tools, skill_tools, subagent, team_tools, todo_tools, web_fetch, web_search
from twinkle.agentserver.workflow import tools as workflow_tools
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def tool_manager() -> ToolManager:
    """Build a ToolManager pre-loaded with the default tools."""
    tm = ToolManager()
    tm.register(web_fetch.web_fetch)
    tm.register(web_search.web_search)
    tm.register(command_exec.command_exec)
    tm.register(file_tools.read_file)
    tm.register(file_tools.write_file)
    tm.register(file_tools.edit_file)
    tm.register(file_tools.list_files)
    tm.register(file_tools.glob)
    tm.register(todo_tools.todo_create)
    tm.register(todo_tools.todo_update)
    tm.register(todo_tools.todo_list)
    tm.register(todo_tools.todo_get)
    tm.register(skill_tools.list_skill)
    tm.register(skill_tools.read_skill)
    tm.register(memory_tools.memory_search)
    tm.register(memory_tools.write_memory)
    tm.register(memory_tools.read_memory)
    tm.register(memory_tools.edit_memory)
    tm.register(cron_tools.cron_list_jobs)
    tm.register(cron_tools.cron_create_job)
    tm.register(cron_tools.cron_update_job)
    tm.register(cron_tools.cron_delete_job)
    tm.register(cron_tools.cron_run_now)
    tm.register(subagent.spawn_subagent)
    # Dynamic description — lists available workflows so the LLM knows what to call
    wf_tool = workflow_tools.execute_workflow
    wf_tool.card.description = workflow_tools._build_tool_description()
    tm.register(wf_tool)
    tm.register(team_tools.delegate_to_member)
    return tm


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "tool_manager",
]
