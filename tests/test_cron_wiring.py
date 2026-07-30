"""装配测试: tool_manager 注册 cron 工具; gateway main 构造 scheduler。

Task 8 wiring — verifies the 5 cron agent tools are registered in
``tool_manager()`` and that ``CronSchedulerService`` exposes the
start/stop contract the gateway ``main()`` relies on.

Note on ToolManager internals (verified against
``twinkle/agentserver/tools/manager.py``):
``ToolManager._tools`` is ``dict[str, Tool]`` keyed by ``tool.card.name``
(register stores ``self._tools[tool.card.name] = tool``). ``Tool`` /
``LocalFunction`` expose ``.card.name`` — NOT a bare ``.name`` — so we read
the dict keys directly (they *are* the tool names).
"""
from __future__ import annotations

import asyncio


def run(coro):
    return asyncio.run(coro)


def test_tool_manager_has_cron_tools():
    from twinkle.agentserver.tools import tool_manager

    tm = tool_manager()
    # _tools is dict[str, Tool] keyed by tool.card.name (see manager.py:14,17)
    names = set(tm._tools.keys())
    for n in ("cron_list_jobs", "cron_create_job", "cron_update_job",
              "cron_delete_job", "cron_run_now"):
        assert n in names, f"missing tool: {n}"


def test_gateway_main_builds_scheduler(monkeypatch, tmp_path):
    """gateway main 依赖 CronSchedulerService 的 start/stop 契约。"""
    from twinkle.gateway.cron.scheduler import CronSchedulerService

    assert hasattr(CronSchedulerService, "start")
    assert hasattr(CronSchedulerService, "stop")
