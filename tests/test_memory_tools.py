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


def test_four_tools_registered():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    for n in ("memory_search", "write_memory", "read_memory", "edit_memory"):
        assert n in _names(tm), f"{n} not registered"


def test_write_read_search_round_trip(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import (
        memory_search, read_memory, write_memory)
    out = asyncio.run(write_memory.func("MEMORY.md", "用户偏好中文。", True))
    assert "Stored" in out
    body = asyncio.run(read_memory.func("MEMORY.md"))
    assert "用户偏好中文。" in body
    hits = asyncio.run(memory_search.func("偏好"))
    assert any("偏好" in h for h in [hits])  # search returns a formatted string
    assert "偏好" in hits


def test_edit_tool(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import edit_memory, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "偏好英文。", True))
    out = asyncio.run(edit_memory.func("MEMORY.md", "英文", "中文"))
    assert "Edited" in out


def test_tool_returns_error_string_on_bad_path(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import write_memory
    out = asyncio.run(write_memory.func("../escape.md", "x", True))
    assert "invalid" in out.lower()  # no raise, no ReAct crash


def test_schemas_expose_params():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    schemas = {c.card.name: c.card.parameters for c in tm._tools.values()}  # noqa: SLF001
    assert schemas["memory_search"]["properties"]["query"]["type"] == "string"
    assert "max_results" in schemas["memory_search"]["properties"]
    assert schemas["write_memory"]["properties"]["append"]["type"] == "boolean"


def test_memory_search_empty_query_returns_no_memories(isolated_memory):
    """Empty/whitespace query must not raise (FTS5 MATCH '""' is a syntax error);
    the tool returns the memory-shaped 'No relevant memories found.' string
    instead of leaking a framework [tool error] to the model."""
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search
    assert asyncio.run(memory_search.func("")) == "No relevant memories found."
    assert asyncio.run(memory_search.func("   ")) == "No relevant memories found."
