import asyncio
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
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
    """MemoryHook.before_invoke stashes the strategy section to frozen_sections
    (loop applies it to the prefix); the memory_search tool then returns a hit —
    proving hook + tool cooperate end-to-end under the per-invoke design."""
    tm = tool_manager()
    asyncio.run(tm.execute("write_memory",
                           {"path": "MEMORY.md",
                            "content": "项目架构是两进程 WebSocket。",
                            "append": True}))
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_INVOKE,
                      inputs=InvokeInputs(query="上次说的架构是啥", mode=""),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_invoke(ctx))
    # hook stashed the strategy section (contains memory_search hint) to frozen_sections
    sections = ctx.extra.get("frozen_sections", [])
    strat = next((s for s in sections if s.name == "memory_strategy"), None)
    assert strat is not None
    assert "memory_search" in strat.content
    # and the tool actually returns a hit for the populated store
    hits = asyncio.run(tm.execute("memory_search", {"query": "架构"}))
    assert "WebSocket" in hits


def test_empty_store_hook_noop(memory_enabled):
    """空 store → before_invoke no-op(不 stash frozen_sections,不碰 inputs)。"""
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_INVOKE,
                      inputs=InvokeInputs(query="hi", mode=""),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_invoke(ctx))
    assert "frozen_sections" not in ctx.extra  # empty store → no-op,不创建 key
