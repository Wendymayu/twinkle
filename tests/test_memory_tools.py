import asyncio
import pytest
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.tools.manager import ToolManager


@pytest.fixture
def isolated_memory(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


def _names(tm):
    return sorted(c.card.name for c in tm._tools.values())  # noqa: SLF001


def test_three_tools_registered():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    for n in ("memory_search", "write_memory", "read_memory"):
        assert n in _names(tm), f"{n} not registered"


def test_write_read_search_round_trip(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import (
        memory_search, read_memory, write_memory)
    out = asyncio.run(write_memory.func("MEMORY.md", "用户偏好中文。", True))
    assert "Stored" in out
    body = asyncio.run(read_memory.func("MEMORY.md"))
    assert "用户偏好中文。" in body
    hits = asyncio.run(memory_search.func("偏好"))
    assert any("偏好" in h for h in [hits])  # search 返回格式化字符串
    assert "偏好" in hits


def test_tool_returns_error_string_on_bad_path(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import write_memory
    out = asyncio.run(write_memory.func("../escape.md", "x", True))
    assert "invalid" in out.lower()  # 不抛异常，ReAct 不崩


def test_schemas_expose_params():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    schemas = {c.card.name: c.card.parameters for c in tm._tools.values()}  # noqa: SLF001
    assert schemas["memory_search"]["properties"]["query"]["type"] == "string"
    assert "max_results" in schemas["memory_search"]["properties"]
    assert schemas["write_memory"]["properties"]["append"]["type"] == "boolean"


def test_memory_search_empty_query_returns_no_memories(isolated_memory):
    """空/空白 query 不能抛异常（FTS5 MATCH '""' 是语法错误）；
    tool 返回 memory 形状的 'No relevant memories found.' 字符串，
    而不是把框架的 [tool error] 泄露给模型。"""
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search
    assert asyncio.run(memory_search.func("")) == "No relevant memories found."
    assert asyncio.run(memory_search.func("   ")) == "No relevant memories found."


def test_memory_search_returns_anchor_and_line_range(isolated_memory):
    """memory_search 返回含 [§ section_title] 锚点 + L{start}-{end} 行号,
    让 agent 能定位"这是哪次对话/哪段"——治召回孤立片段无上下文。"""
    from twinkle.agentserver.memory import get_memory_manager
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func(
        "MEMORY.md", "## 2023-08-23 — Conv\n- Caroline applied to adoption this week", True))
    # 模型 B:write 零索引零 dirty(由 watcher 标);isolated_memory 用默认 watcher,
    # on_modified 异步未必先于 search 触发 → 模拟 watcher 标 dirty 保确定性(对齐
    # test_memory_store.py 的 established pattern)。
    get_memory_manager()._mark_dirty("MEMORY.md")  # noqa: SLF001
    out = asyncio.run(memory_search.func("adoption"))
    assert "§" in out, "返回应含 [§ anchor] 锚点"
    assert "2023-08-23" in out, "锚点应含对话日期"
    assert "L1-" in out, "返回应含行号范围"


def test_memory_search_header_english_for_english_query(isolated_memory):
    """英文 query(无 CJK)→ 英文返回头,不诱导 agent 中文作答(#73 根因之一)。"""
    from twinkle.agentserver.memory import get_memory_manager
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "## 2023-08-23\n- applied to adoption", True))
    get_memory_manager()._mark_dirty("MEMORY.md")  # noqa: SLF001
    out = asyncio.run(memory_search.func("adoption"))
    assert "## Memory Recall" in out
    assert "记忆召回" not in out


def test_memory_search_header_chinese_for_cjk_query(isolated_memory):
    """中文 query(含 CJK)→ 中文返回头。"""
    from twinkle.agentserver.memory import get_memory_manager
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "- 用户偏好中文", True))
    get_memory_manager()._mark_dirty("MEMORY.md")  # noqa: SLF001
    out = asyncio.run(memory_search.func("偏好"))
    assert "## 记忆召回" in out
