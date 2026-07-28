import asyncio
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager


def _ctx(messages=None):
    return HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                       inputs=ModelCallInputs(messages=messages or [], tools=[]),
                       session_id="s", request_id="r")


@pytest.fixture
def empty_memory(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


@pytest.fixture
def populated_memory(tmp_path):
    mgr = MemoryManager(str(tmp_path), embed_provider=None)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    _set_memory_manager(mgr)
    yield tmp_path
    _set_memory_manager(None)


def test_noop_when_no_memory(empty_memory):
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]


def test_injects_prompt_when_memory_present(populated_memory):
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages[0]["role"] == "system"
    body = ctx.inputs.messages[0]["content"]
    assert "memory_search" in body
    assert "write_memory" in body
    assert "USER.md" in body and "MEMORY.md" in body
    # today's daily path substituted
    import datetime
    assert f"daily_memory/{datetime.date.today().isoformat()}.md" in body
    # original message preserved after
    assert ctx.inputs.messages[1] == {"role": "user", "content": "hi"}


def test_replaces_list_not_mutate(populated_memory):
    original = [{"role": "user", "content": "hi"}]
    ctx = _ctx(original)
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert original == [{"role": "user", "content": "hi"}]  # not mutated in place
    assert ctx.inputs.messages is not original
