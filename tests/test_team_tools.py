"""Tests for team task/message tools (Phase 19 Task 5).

Each tool is a thin wrapper: reads CURRENT_TEAM → calls team method → formats.
These tests exercise the happy path against a real Team + isolated todo store.
"""

import asyncio

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.manager import Team, TeamManager
from twinkle.agentserver.tools.builtin import team_tools


def _team(session_store):
    mgr = TeamManager(llm=None, store=session_store, parent_tools=None,
                     config=None)
    team = mgr.ensure_team("s1")
    CURRENT_TEAM.set(team)
    return team


def test_send_member_no_contextvar():
    CURRENT_TEAM.set(None)
    out = asyncio.run(team_tools.send_member.func("researcher", "hi"))
    assert "team unavailable" in out


def test_create_task_then_list(session_store, isolated_todo_store):
    _team(session_store)
    out = asyncio.run(team_tools.create_task.func("调研 X"))
    assert "Created" in out or "调研" in out
    listed = asyncio.run(team_tools.list_tasks.func())
    assert "调研 X" in listed


def test_claim_complete_flow(session_store, isolated_todo_store):
    team = _team(session_store)
    t = asyncio.run(team.task_store.create_task("T1"))  # 直接拿真实 task id
    claimed = asyncio.run(team_tools.claim_task.func(t.id, "researcher"))
    assert "researcher" in claimed or "Claimed" in claimed
    # Task 7 前 CURRENT_MEMBER_NAME 未 set;complete_task 走显式 member_name
    # (与 claim 同一 owner),否则 task_store 的 owner 校验会拒。
    done = asyncio.run(team_tools.complete_task.func(t.id, "结果",
                                                    member_name="researcher"))
    assert "Completed" in done or "completed" in done.lower()


def test_complete_task_help_reason_branch(session_store, isolated_todo_store):
    team = _team(session_store)
    t = asyncio.run(team.task_store.create_task("T1"))
    asyncio.run(team_tools.claim_task.func(t.id, "researcher"))
    out = asyncio.run(team_tools.complete_task.func(
        t.id, member_name="researcher", help_reason="need X data"))
    assert "Help requested" in out
    after = asyncio.run(team.task_store.get_task(t.id))
    assert after.metadata.get("help_reason") == "need X data"
    assert after.status == "in_progress"  # 求助不改 status
