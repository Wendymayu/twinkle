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
    """Session A 写入一条事实；Session B 搜索，tool 返回命中。
    对齐 spec 验收：A.write -> B.search 命中。"""
    tm = tool_manager()
    # Session A：写入
    out = asyncio.run(tm.execute("write_memory",
                                 {"path": "MEMORY.md",
                                  "content": "用户偏好用中文交流。",
                                  "append": True}))
    assert "Stored" in out

    # Session B（独立进程会共享同一 MEMORY_DIR 磁盘）：搜索
    # query 是所存事实的子串（FTS-only fixture：无 vector 腿，故召回靠
    # 子串/精确匹配，非语义——jiuwenswarm 的 vector 腿负责语义 CJK 召回；
    # FTS-only 降级路径做不到）。
    hits = asyncio.run(tm.execute("memory_search", {"query": "用户偏好"}))
    assert "偏好" in hits  # 事实作为 tool_result 字符串被召回


def test_hook_injects_then_tool_answers(memory_enabled):
    """MemoryHook.before_invoke 把策略 section stash 到 frozen_sections
    （loop 会把它应用到 prefix）；memory_search tool 随后返回命中——
    证明 hook + tool 在 per-invoke 设计下端到端协作。"""
    tm = tool_manager()
    asyncio.run(tm.execute("write_memory",
                           {"path": "MEMORY.md",
                            "content": "项目架构是两进程 WebSocket。",
                            "append": True}))
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_INVOKE,
                      inputs=InvokeInputs(query="上次说的架构是啥", mode=""),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_invoke(ctx))
    # hook 把策略 section（含 memory_search 提示）stash 到 frozen_sections
    sections = ctx.extra.get("frozen_sections", [])
    strat = next((s for s in sections if s.name == "memory_strategy"), None)
    assert strat is not None
    assert "memory_search" in strat.content
    # 且 tool 对有数据的 store 确实返回命中
    hits = asyncio.run(tm.execute("memory_search", {"query": "架构"}))
    assert "WebSocket" in hits


def test_empty_store_hook_noop(memory_enabled):
    """空 store → before_invoke no-op(不 stash frozen_sections,不碰 inputs)。"""
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_INVOKE,
                      inputs=InvokeInputs(query="hi", mode=""),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_invoke(ctx))
    assert "frozen_sections" not in ctx.extra  # 空 store → no-op，不创建 key
