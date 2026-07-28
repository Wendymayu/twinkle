import asyncio
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.tools import tool_manager


@pytest.fixture
def memory_enabled(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


def test_cross_session_recall_via_toolmanager(memory_enabled):
    """Session A writes a fact; Session B searches and the tool returns the hit.
    Mirrors the spec acceptance: A.write -> B.search hits."""
    tm = tool_manager()
    # Session A: write
    out = asyncio.run(tm.execute("write_memory",
                                 {"path": "MEMORY.md",
                                  "content": "用户偏好用中文交流。",
                                  "append": True}))
    assert "Stored" in out

    # Session B (separate process would share the same MEMORY_DIR on disk): search
    # Query is a substring of the stored fact (FTS-only fixture: no vector leg,
    # so recall is substring/exact-match, not semantic — jiuwenswarm's vector
    # leg handles semantic CJK recall; the FTS-only degrade path can't).
    hits = asyncio.run(tm.execute("memory_search", {"query": "用户偏好"}))
    assert "偏好" in hits  # the fact is recalled as a tool_result string


def test_hook_injects_then_tool_answers(memory_enabled):
    """MemoryHook injects the usage-strategy prompt on a populated store; the
    memory_search tool then returns a hit — proving the hook + tool cooperate."""
    tm = tool_manager()
    asyncio.run(tm.execute("write_memory",
                           {"path": "MEMORY.md",
                            "content": "项目架构是两进程 WebSocket。",
                            "append": True}))
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                      inputs=ModelCallInputs(messages=[{"role": "user", "content": "上次说的架构是啥"}], tools=[]),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_model_call(ctx))
    # hook injected the prompt
    assert ctx.inputs.messages[0]["role"] == "system"
    assert "memory_search" in ctx.inputs.messages[0]["content"]
    # and the tool actually returns a hit for the populated store
    hits = asyncio.run(tm.execute("memory_search", {"query": "架构"}))
    assert "WebSocket" in hits


def test_empty_store_hook_noop(memory_enabled):
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                      inputs=ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[]),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
