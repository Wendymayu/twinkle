import asyncio

from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.subagent_tools import spawn_subagent
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.subagent_executor import SubagentExecutor
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.config.schema import SubagentConfig


def _make_executor(store, *, child_hooks=None, config=None):
    # parent ToolManager = base tools + spawn_subagent (mirrors build_agent_loop)
    parent_tm = tool_manager()
    parent_tm.register(spawn_subagent)
    return SubagentExecutor(
        llm=None, store=store, parent_tools=parent_tm,
        config=config or SubagentConfig(), child_hooks=child_hooks,
    )


def test_build_child_tool_manager_excludes_subagent_and_memory_writes(session_store):
    ex = _make_executor(session_store, child_hooks=[])
    child_tm = ex._build_child_tool_manager()
    names = {t.card.name for t in child_tm.list()}
    assert "spawn_subagent" not in names
    assert "write_memory" not in names
    assert "edit_memory" not in names
    # read-only memory + other tools still present
    assert "memory_search" in names
    assert "read_memory" in names
    assert "web_fetch" in names
    assert "command_exec" in names


def test_child_system_prompt_mentions_role_and_rules(session_store):
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    ex = _make_executor(session_store, child_hooks=[])
    t = SubagentTaskSpec(objective="do thing", role_id="Researcher")
    prompt = ex._child_system_prompt(t)
    assert "sub-agent" in prompt.lower()
    assert "Researcher" in prompt
    assert "spawn_subagent" in prompt          # tells child it cannot delegate
    assert "list_skill" in prompt              # tells child how to use skills


def test_build_query_objective_with_prompt(session_store):
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    ex = _make_executor(session_store, child_hooks=[])
    q = ex._build_query(SubagentTaskSpec(objective="OBJ", prompt="PROMPT"))
    assert "OBJ" in q and "PROMPT" in q
    q2 = ex._build_query(SubagentTaskSpec(objective="OBJ"))
    assert q2 == "OBJ"


def test_resolve_llm_reuses_parent_when_no_override(session_store):
    class _FakeLLM:
        def __init__(self, model): self.model = model
    ex = _make_executor(session_store, child_hooks=[])
    ex._llm = _FakeLLM("parent-model")
    assert ex._resolve_llm("") is ex._llm
    assert ex._resolve_llm("  ") is ex._llm      # whitespace = none


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


def test_execute_spawn_returns_child_final(session_store):
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    ex = _executor_with_scripted_child(session_store, [
        [TextDelta("sub "), TextDelta("answer"),
         Finish("stop", {"role": "assistant", "content": "sub answer", "tool_calls": None})],
    ])
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="do X"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is True
    assert result.result == "sub answer"


def test_execute_spawn_uses_isolated_child_session(session_store):
    """Child session != parent; child history has only [system, user, assistant]."""
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    # pre-populate the parent session with history the child must NOT inherit
    asyncio.run(session_store.append("p1", {"role": "user", "content": "parent secret"}))
    ex = _executor_with_scripted_child(session_store, [
        # TextDelta chunks accumulate into e2a.complete.body.result.content
        # (the agent loop emits full_text, not assistant_message.content) —
        # mirror a real stream: chunks then Finish carrying the assembled msg.
        [TextDelta("ok"),
         Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="child task"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success and result.result == "ok"
    # find the child session id (parent's history untouched)
    all_sids = [r["session_id"] for r in session_store.list_sessions(include_subagents=True)]
    child_sids = [s for s in all_sids if s.startswith("p1__sub_")]
    assert len(child_sids) == 1
    child_msgs = session_store.get_messages(child_sids[0])
    assert [m["role"] for m in child_msgs] == ["system", "user", "assistant"]
    assert child_msgs[1]["content"] == "child task"
    assert "parent secret" not in str(child_msgs)
    # parent session untouched by the child run
    assert [m["content"] for m in session_store.get_messages("p1")] == ["parent secret"]


def test_execute_spawn_child_error_returns_failure(session_store):
    """Child LLM raises -> SubagentResult(success=False) carrying the error."""
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec

    class _RaisingLLM:
        async def stream(self, messages, tools):
            raise RuntimeError("child boom")
            yield  # noqa: makes it an async generator

    ex = _make_executor(session_store, child_hooks=[])
    ex._llm = _RaisingLLM()
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "child boom" in (result.error or "") or "RuntimeError" in (result.error or "")


def test_drive_child_truncates_overlong_final(session_store):
    """Child final over max_result_chars is truncated (protects parent context)."""
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish, TextDelta
    long_text = "x" * 10000
    ex = _executor_with_scripted_child(session_store, [
        [TextDelta(long_text), Finish("stop", {"role": "assistant", "content": long_text, "tool_calls": None})],
    ], config=SubagentConfig(max_result_chars=50))
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is True
    assert result.result.endswith("[truncated]")
    assert len(result.result) <= 70          # 50 + truncation marker


def test_contextvar_isolation_parent_plan_todo_unchanged(session_store):
    """Child run sets PLAN_TODO_SESSION_ID=child_sid inside a child task (context
    copy); the parent's ContextVar must be untouched after execute_spawn returns."""
    from twinkle.agentserver.todo import PLAN_TODO_SESSION_ID
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish, TextDelta
    tok = PLAN_TODO_SESSION_ID.set("parent-sid")
    try:
        ex = _executor_with_scripted_child(session_store, [
            [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
        ])
        asyncio.run(ex.execute_spawn(
            SubagentTaskSpec(objective="o"), parent_session_id="parent-sid",
            parent_request_id="r1"))
        assert PLAN_TODO_SESSION_ID.get() == "parent-sid"
    finally:
        PLAN_TODO_SESSION_ID.reset(tok)


def test_hard_timeout_returns_failure(session_store):
    """Child LLM sleeps past hard_timeout -> success=False, error mentions hard timeout."""
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec

    class _SlowLLM:
        async def stream(self, messages, tools):
            await asyncio.sleep(10)
            yield  # noqa

    ex = _make_executor(session_store, child_hooks=[],
                        config=SubagentConfig(hard_timeout=0.1, soft_timeout=5.0))
    ex._llm = _SlowLLM()
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "hard timeout" in (result.error or "")


def test_soft_timeout_returns_failure(session_store):
    """Child produces nothing for soft_timeout -> success=False, error mentions soft timeout."""
    from twinkle.agentserver.tools.subagent_executor.models import SubagentTaskSpec
    from twinkle.agentserver.llm_client import Finish

    class _SilentThenSlowLLM:
        async def stream(self, messages, tools):
            await asyncio.sleep(10)   # no frames for >soft_timeout
            yield Finish("stop", {"role": "assistant", "content": "late", "tool_calls": None})

    ex = _make_executor(session_store, child_hooks=[],
                        config=SubagentConfig(hard_timeout=30.0, soft_timeout=0.1))
    ex._llm = _SilentThenSlowLLM()
    result = asyncio.run(ex.execute_spawn(
        SubagentTaskSpec(objective="o"), parent_session_id="p1", parent_request_id="r1"))
    assert result.success is False
    assert "soft timeout" in (result.error or "")


def test_abort_active_subagents_cancels_registered(session_store):
    """abort_active_subagents cancels tasks tracked in _active."""
    ex = _make_executor(session_store, child_hooks=[])

    async def _hang():
        await asyncio.sleep(100)

    async def _scenario():
        # the task must live in the same loop that runs abort_active_subagents
        t = asyncio.ensure_future(_hang())
        ex._active["t1"] = t
        n = await ex.abort_active_subagents(reason="test")
        return n, t

    n, t = asyncio.run(_scenario())
    assert n == 1
    assert t.cancelled() or t.done()
