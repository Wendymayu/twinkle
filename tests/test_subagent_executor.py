import asyncio

from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.config.schema import SubagentConfig


def _make_executor(store, *, child_hooks=None, config=None):
    # parent ToolManager = default tools (incl spawn_subagent, registered in tool_manager())
    parent_tm = tool_manager()
    return SubagentExecutor(
        llm=None, store=store, parent_tools=parent_tm,
        config=config or SubagentConfig(), child_hooks=child_hooks,
    )


def test_build_tool_manager_excludes_subagent_and_memory_writes(session_store):
    ex = _make_executor(session_store, child_hooks=[])
    tool_manager = ex._build_tool_manager()
    names = {t.card.name for t in tool_manager.list()}
    assert "spawn_subagent" not in names
    assert "write_memory" not in names
    assert "edit_memory" not in names
    # read-only memory + other tools still present
    assert "memory_search" in names
    assert "read_memory" in names
    assert "web_fetch" in names
    assert "command_exec" in names


def test_system_prompt_mentions_rules(session_store):
    ex = _make_executor(session_store, child_hooks=[])
    child = ex._build_child_agent()
    # child identity = normal base prompt + sub-agent addendum, baked into base_sections
    built = "\n\n".join(s.content for s in child._base_sections)
    assert "子 agent" in built               # 子 agent 角色 (addendum)
    assert "spawn_subagent" not in built    # 不注入子调不了的工具名（schema 已排除）
    assert "list_skill" in built             # skill 用法
    assert "memory_search" in built          # 记忆只读


def test_build_query_objective_with_prompt(session_store):
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    ex = _make_executor(session_store, child_hooks=[])
    q = ex._build_query(SubagentTaskSpec(objective="OBJ", prompt="PROMPT"))
    assert "OBJ" in q and "PROMPT" in q
    q2 = ex._build_query(SubagentTaskSpec(objective="OBJ"))
    assert q2 == "OBJ"


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


def _executor_with_scripted_child(store, scripts, *, child_hooks=None, config=None):
    ex = _make_executor(store, child_hooks=child_hooks, config=config)
    ex._llm = _ScriptedLLM(scripts)
    return ex


def test_execute_subagent_returns_child_final(session_store):
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    ex = _executor_with_scripted_child(session_store, [
        [TextDelta("sub "), TextDelta("answer"),
         Finish("stop", {"role": "assistant", "content": "sub answer", "tool_calls": None})],
    ])
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="do X"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is True
    assert result.result == "sub answer"


def test_execute_subagent_uses_isolated_child_session(session_store):
    """Child session != parent; child history has only [user, assistant]."""
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    # pre-populate the parent session with history the child must NOT inherit
    asyncio.run(session_store.append("p1", {"role": "user", "content": "parent secret"}))
    ex = _executor_with_scripted_child(session_store, [
        # TextDelta chunks accumulate into e2a.complete.body.result.content
        # (the agent loop emits full_text, not assistant_message.content) —
        # mirror a real stream: chunks then Finish carrying the assembled msg.
        [TextDelta("ok"),
         Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="child task"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success and result.result == "ok"
    # find the child session id (parent's history untouched)
    all_sids = [r["session_id"] for r in session_store.list_sessions(include_subagents=True)]
    child_sids = [s for s in all_sids if s.startswith("p1__sub_")]
    assert len(child_sids) == 1
    child_msgs = session_store.get_messages(child_sids[0])
    assert [m["role"] for m in child_msgs] == ["user", "assistant"]
    assert child_msgs[0]["content"] == "child task"
    assert "parent secret" not in str(child_msgs)
    # parent session untouched by the child run
    assert [m["content"] for m in session_store.get_messages("p1")] == ["parent secret"]


def test_execute_subagent_child_error_returns_failure(session_store):
    """Child LLM raises -> SubagentResult(success=False) carrying the error."""
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec

    class _RaisingLLM:
        async def stream(self, messages, tools):
            raise RuntimeError("child boom")
            yield  # noqa: makes it an async generator

    ex = _make_executor(session_store, child_hooks=[])
    ex._llm = _RaisingLLM()
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "child boom" in (result.error or "") or "RuntimeError" in (result.error or "")


def test_drive_child_truncates_overlong_final(session_store):
    """Child final over max_result_chars is truncated (protects parent context)."""
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish, TextDelta
    long_text = "x" * 10000
    ex = _executor_with_scripted_child(session_store, [
        [TextDelta(long_text), Finish("stop", {"role": "assistant", "content": long_text, "tool_calls": None})],
    ], config=SubagentConfig(max_result_chars=50))
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is True
    assert result.result.endswith("[truncated]")
    assert len(result.result) <= 70          # 50 + truncation marker


def test_contextvar_isolation_parent_plan_todo_unchanged(session_store):
    """Child run sets PLAN_TODO_SESSION_ID=child_sid inside a child task (context
    copy); the parent's ContextVar must be untouched after execute_subagent returns."""
    from twinkle.agentserver.todo import PLAN_TODO_SESSION_ID
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish, TextDelta
    tok = PLAN_TODO_SESSION_ID.set("parent-session-id")
    try:
        ex = _executor_with_scripted_child(session_store, [
            [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
        ])
        asyncio.run(ex.execute_subagent(
            SubagentTaskSpec(objective="o"), parent_session_id="parent-session-id",
            parent_request_id="r1"))
        assert PLAN_TODO_SESSION_ID.get() == "parent-session-id"
    finally:
        PLAN_TODO_SESSION_ID.reset(tok)


def test_hard_timeout_returns_failure(session_store):
    """Child LLM sleeps past hard_timeout -> success=False, error mentions hard timeout."""
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec

    class _SlowLLM:
        async def stream(self, messages, tools):
            await asyncio.sleep(10)
            yield  # noqa

    ex = _make_executor(session_store, child_hooks=[],
                        config=SubagentConfig(hard_timeout=0.1, soft_timeout=5.0))
    ex._llm = _SlowLLM()
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "hard timeout" in (result.error or "")


def test_soft_timeout_returns_failure(session_store):
    """Child produces nothing for soft_timeout -> success=False, error mentions soft timeout."""
    from twinkle.agentserver.tools.builtin.subagent import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish

    class _SilentThenSlowLLM:
        async def stream(self, messages, tools):
            await asyncio.sleep(10)   # no frames for >soft_timeout
            yield Finish("stop", {"role": "assistant", "content": "late", "tool_calls": None})

    ex = _make_executor(session_store, child_hooks=[],
                        config=SubagentConfig(hard_timeout=30.0, soft_timeout=0.1))
    ex._llm = _SilentThenSlowLLM()
    result = asyncio.run(ex.execute_subagent(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "soft timeout" in (result.error or "")
