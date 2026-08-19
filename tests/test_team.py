"""Tests for Team Phase 18: TeamManager, Team, delegate_to_member, wiring."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.manager import MEMBER_TOOL_WHITELIST, Team, TeamManager
from twinkle.agentserver.team.workspace import ensure_team_workspace, team_workspace_dir
from twinkle.agentserver.tools.errors import ToolError
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config.schema import TeamConfig


# ── helpers ───────────────────────────────────────────────────

def _parent_tools() -> ToolManager:
    from twinkle.agentserver.tools import tool_manager
    return tool_manager()


def _team_manager(store, config=None):
    return TeamManager(
        llm=None, store=store,
        parent_tools=_parent_tools(),
        config=config or TeamConfig(),
    )


class _ScriptedLLM:
    """Returns one canned event-list per call, in order."""

    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _team_with_scripted_llm(store, scripts, config=None):
    mgr = _team_manager(store, config=config)
    team = mgr.ensure_team("s1")
    team._llm = _ScriptedLLM(scripts)
    return team


# ── workspace ─────────────────────────────────────────────────

def test_team_workspace_dir():
    d = team_workspace_dir("s1")
    assert "team" in str(d)
    assert "s1" in str(d)


def test_ensure_team_workspace_creates_dir(tmp_path):
    d = ensure_team_workspace("s1")
    assert d.exists()
    assert d.is_dir()


# ── member key ────────────────────────────────────────────────

def test_member_key_stable():
    k1 = Team._member_key("researcher")
    k2 = Team._member_key("researcher")
    assert k1 == k2
    assert isinstance(k1, str)
    assert k1 == "researcher"  # member_name is the key (spec §3.1)


def test_member_key_different_persona():
    k1 = Team._member_key("researcher")
    k2 = Team._member_key("writer")
    assert k1 != k2


# ── TeamManager lifecycle ─────────────────────────────────────

def test_team_manager_ensure_team(session_store):
    mgr = _team_manager(session_store)
    t1 = mgr.ensure_team("s1")
    t2 = mgr.ensure_team("s1")
    assert t1 is t2  # same session → same Team


def test_team_manager_different_sessions(session_store):
    mgr = _team_manager(session_store)
    t1 = mgr.ensure_team("s1")
    t2 = mgr.ensure_team("s2")
    assert t1 is not t2


def test_team_manager_destroy(session_store):
    mgr = _team_manager(session_store)
    mgr.ensure_team("s1")
    assert "s1" in mgr._teams
    mgr.destroy_team("s1")
    assert "s1" not in mgr._teams


def test_team_manager_destroy_nonexistent_noop(session_store):
    mgr = _team_manager(session_store)
    mgr.destroy_team("nonexistent")  # should not raise


# ── Team._build_member ────────────────────────────────────────

def test_build_member_filtered_tools(session_store):
    mgr = _team_manager(session_store)
    mgr._llm = _ScriptedLLM([])
    team = mgr.ensure_team("s1")
    team._llm = _ScriptedLLM([])

    async def _run():
        return await team._build_member("tester", "tester persona")
    member = asyncio.run(_run())

    tool_names = {t.card.name for t in member._tool_manager.list()}
    # tools in whitelist present
    for name in ["web_search", "read_file", "write_file", "command_exec"]:
        assert name in tool_names, f"{name} should be in member tools"
    # excluded tools
    for name in ["spawn_subagent", "delegate_to_member", "write_memory"]:
        assert name not in tool_names, f"{name} should NOT be in member tools"


def test_build_member_persona_in_system_prompt(session_store):
    mgr = _team_manager(session_store)
    mgr._llm = _ScriptedLLM([])
    team = mgr.ensure_team("s1")
    team._llm = _ScriptedLLM([])

    async def _run():
        return await team._build_member("researcher", "金融分析师")
    member = asyncio.run(_run())

    # persona baked into base_sections (injected at construction); session store no longer seeds a system msg
    built = "\n\n".join(s.content for s in member._base_sections)
    assert "researcher" in built
    assert "金融分析师" in built


def test_build_member_workspace_in_prompt(session_store):
    mgr = _team_manager(session_store)
    mgr._llm = _ScriptedLLM([])
    team = mgr.ensure_team("s1")
    team._llm = _ScriptedLLM([])

    async def _run():
        return await team._build_member("tester", "tester persona")
    member = asyncio.run(_run())

    built = "\n\n".join(s.content for s in member._base_sections)
    assert "workspace" in built.lower() or team.workspace.name in built


# ── delegate ──────────────────────────────────────────────────

def test_delegate_runs_member_to_completion(session_store):
    team = _team_with_scripted_llm(session_store, [
        [TextDelta("analysis done"), Finish("stop",
                                            {"role": "assistant", "content": "analysis done", "tool_calls": None})],
    ])
    result = asyncio.run(team.delegate("researcher", "researcher persona", "analyze data"))
    assert "analysis done" in result


def test_delegate_reuses_member(session_store):
    team = _team_with_scripted_llm(session_store, [
        [TextDelta("result1"), Finish("stop",
                                      {"role": "assistant", "content": "result1", "tool_calls": None})],
        [TextDelta("result2"), Finish("stop",
                                      {"role": "assistant", "content": "result2", "tool_calls": None})],
    ])
    r1 = asyncio.run(team.delegate("researcher", "researcher persona", "task1"))
    r2 = asyncio.run(team.delegate("researcher", "researcher persona", "task2"))
    assert "result1" in r1
    assert "result2" in r2
    # same persona -> same member key -> only one member in cache
    assert len(team._members) == 1


def test_member_run_end_releases_uncompleted_claim(session_store, isolated_todo_store):
    team = _team_with_scripted_llm(session_store, [
        # member run 一轮就 stop(claim 了但没 complete)
        [TextDelta("claimed"), Finish("stop", {"role": "assistant",
          "content": "claimed", "tool_calls": None})],
    ])
    # 建一个 task 并手动让 member claim(member 没真调工具,模拟)
    t = asyncio.run(team.task_store.create_task("T1"))
    asyncio.run(team.task_store.claim_task(t.id, "researcher"))
    # member run 结束(delegate 跑完)
    asyncio.run(team.delegate("researcher", "researcher persona", "claim T1"))
    # member run 结束 → T1 应被释放(未 complete)
    after = asyncio.run(team.task_store.get_task(t.id))
    assert after.status == "pending"
    assert after.owner == ""


# ── delegate_to_member tool ───────────────────────────────────

def test_delegate_to_member_no_contextvar():
    """When CURRENT_TEAM is not set, raises ToolError (unavailable)."""
    from twinkle.agentserver.tools.builtin.team_tools import delegate_to_member

    token = CURRENT_TEAM.set(None)
    try:
        with pytest.raises(ToolError, match="team feature not initialized"):
            asyncio.run(delegate_to_member.func("researcher", "researcher persona", "task"))
    finally:
        CURRENT_TEAM.reset(token)


# ── TeamContextHook ───────────────────────────────────────────

def test_team_context_hook_sets_contextvar(session_store):
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
    from twinkle.agentserver.hooks.builtin.team_context_hook import TeamContextHook

    mgr = _team_manager(session_store)
    hook = TeamContextHook(mgr)

    async def _run():
        ctx = HookContext(
            agent=None, event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query="test", mode="team"),
            session_id="s1", request_id="r1",
        )
        await hook.before_invoke(ctx)
        team = CURRENT_TEAM.get()
        assert team is not None
        assert team._session_id == "s1"

    asyncio.run(_run())


def test_team_context_hook_noop_when_normal_mode(session_store):
    """TeamContextHook sets CURRENT_TEAM to None when mode != team."""
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
    from twinkle.agentserver.hooks.builtin.team_context_hook import TeamContextHook

    mgr = _team_manager(session_store)
    hook = TeamContextHook(mgr)

    async def _run():
        ctx = HookContext(
            agent=None, event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query="test"),  # mode defaults to ""
            session_id="s1", request_id="r1",
        )
        await hook.before_invoke(ctx)
        team = CURRENT_TEAM.get()
        assert team is None

    asyncio.run(_run())


# ── config wiring ─────────────────────────────────────────────

def test_team_config_defaults_disabled():
    cfg = TeamConfig()
    assert cfg.enabled is False


def test_delegate_to_member_always_registered():
    """delegate_to_member is always registered (mode controls visibility, not registration)."""
    import twinkle.agentserver.tools as tm_pkg
    tm = tm_pkg.tool_manager()
    names = {t.card.name for t in tm.list()}
    assert "delegate_to_member" in names


def test_leader_system_prompt_structure():
    """build_leader_system_prompt includes team role, workflow, and delegate_to_member."""
    from twinkle.agentserver.agent import build_leader_system_prompt
    prompt = build_leader_system_prompt()
    assert "TeamLeader" in prompt
    assert "delegate_to_member" in prompt
    assert "团队角色" in prompt
    assert "核心职责" in prompt
    assert "工作流程" in prompt
    assert "只协调，不执行" in prompt
    assert "运行环境" in prompt
    assert "工具使用指南" in prompt


def test_leader_prompt_omits_user_facing_rules():
    """Leader prompt must NOT include user-facing identity rules from base prompt."""
    from twinkle.agentserver.agent import build_leader_system_prompt
    prompt = build_leader_system_prompt()
    assert "身份与行为原则" not in prompt
    assert "对外交流时" not in prompt
    assert "尽量不拒绝" not in prompt
    # No global workspace paths — leader delegates file work
    assert "工作区根目录" not in prompt
    assert "长期记忆存储" not in prompt


def test_leader_whitelist_excludes_execution_tools():
    """Leader in team mode must NOT have command_exec, write_file, edit_file."""
    from twinkle.agentserver.agent import _TEAM_LEADER_TOOL_WHITELIST
    assert "command_exec" not in _TEAM_LEADER_TOOL_WHITELIST
    assert "write_file" not in _TEAM_LEADER_TOOL_WHITELIST
    assert "edit_file" not in _TEAM_LEADER_TOOL_WHITELIST
    assert "spawn_subagent" not in _TEAM_LEADER_TOOL_WHITELIST
    assert "execute_workflow" not in _TEAM_LEADER_TOOL_WHITELIST


def test_leader_whitelist_has_coordination_tools():
    """Leader must have delegate_to_member, todo, and read-only tools."""
    from twinkle.agentserver.agent import _TEAM_LEADER_TOOL_WHITELIST
    assert "delegate_to_member" in _TEAM_LEADER_TOOL_WHITELIST
    assert "read_file" in _TEAM_LEADER_TOOL_WHITELIST
    assert "web_search" in _TEAM_LEADER_TOOL_WHITELIST
    assert "todo_create" in _TEAM_LEADER_TOOL_WHITELIST


def test_leader_whitelist_has_team_task_tools():
    from twinkle.agentserver.agent import _TEAM_LEADER_TOOL_WHITELIST
    for name in ("create_task", "cancel_task", "list_tasks", "get_task",
                 "send_member", "delegate_to_member"):
        assert name in _TEAM_LEADER_TOOL_WHITELIST, f"missing {name}"


def test_member_whitelist_has_claim_complete():
    for name in ("claim_task", "complete_task", "list_tasks", "get_task"):
        assert name in MEMBER_TOOL_WHITELIST, f"missing {name}"
    # member 不应能 create/cancel/send_member(协调权归 leader)
    for name in ("create_task", "cancel_task", "send_member"):
        assert name not in MEMBER_TOOL_WHITELIST, f"{name} should not be in member whitelist"


def test_base_prompt_omits_team_section():
    """build_system_prompt (normal mode) does NOT include team/leader content."""
    from twinkle.agentserver.agent import build_system_prompt
    prompt = build_system_prompt()
    assert "delegate_to_member" not in prompt
    assert "TeamLeader" not in prompt
    assert "团队角色" not in prompt


# ── member prompt structure (aligned with jiuwenswarm) ────────────

def test_member_prompt_omits_user_facing_identity():
    """Member prompt must NOT include user-facing identity/behavior rules."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="tester", workspace="/tmp/ws")
    assert "身份与行为原则" not in prompt
    assert "对外交流时" not in prompt
    assert "尽量不拒绝" not in prompt


def test_member_prompt_omits_global_workspace_paths():
    """Member prompt must NOT show global WORKSPACE_DIR/MEMORY_DIR/SKILLS_DIR."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="tester", workspace="/tmp/ws")
    assert "工作区根目录" not in prompt
    assert "长期记忆存储" not in prompt
    assert "技能库" not in prompt  # the table row, not the tool section


def test_member_prompt_has_runtime_environment():
    """Member prompt includes runtime environment block (platform/date moved to env-tail)."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="tester", workspace="/tmp/ws")
    assert "运行环境" in prompt
    # 当前平台：/当前日期： env 数值行已移到尾部 <environment_context>(RuntimeEnvHook);
    # 引导句"与当前平台匹配的命令语法"保留(无冒号,不误伤)。
    assert "当前平台：" not in prompt
    assert "当前日期：" not in prompt


def test_member_prompt_has_tool_usage_guide():
    """Member prompt includes Todo tool usage guidance (Memory/Skill injected by hooks)."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="tester", workspace="/tmp/ws")
    assert "工具使用指南" in prompt
    assert "todo_create" in prompt
    # memory_search / list_skill are injected by MemoryHook / SkillHook,
    # not baked into the static prompt — no duplication with hook content.


def test_member_prompt_has_team_sections():
    """Member prompt leads with team role + persona + workspace."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="数据分析师", workspace="/shared")
    assert "团队角色" in prompt
    assert "Teammate" in prompt
    assert "当前人设" in prompt
    assert "数据分析师" in prompt
    assert "团队共享工作区" in prompt
    assert "/shared" in prompt
    # Team sections come before runtime prompt
    assert prompt.index("团队角色") < prompt.index("运行环境")


# ── member_name addressing (Task 3) ───────────────────────────────

def test_member_session_id_uses_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    sid = team._member_session_id("researcher")
    assert "researcher" in sid
    assert sid.startswith("s1__team_")  # _session_id="s1"


def test_ensure_member_keys_by_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    assert "researcher" in team._members
    assert "researcher" in team._inboxes


def test_ensure_member_rejects_same_name_different_persona(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    with pytest.raises(Exception):
        asyncio.run(team._ensure_member("researcher", "different persona"))


def test_send_member_puts_into_inbox(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    asyncio.run(team.send_member("researcher", "add risk section"))
    assert team._inboxes["researcher"].drain() == ["add risk section"]


def test_send_member_unknown_name_errors(session_store):
    team = _team_with_scripted_llm(session_store, [])
    with pytest.raises(KeyError):
        asyncio.run(team.send_member("nobody", "msg"))


def test_member_prompt_contains_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    member = asyncio.run(team._ensure_member("researcher", "金融分析师"))
    # member_name baked into base_sections (injected at construction); session store no longer seeds a system msg
    built = "\n\n".join(s.content for s in member._base_sections)
    assert "researcher" in built
