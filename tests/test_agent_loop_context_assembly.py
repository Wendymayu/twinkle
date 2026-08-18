# tests/test_agent_loop_context_assembly.py
"""loop 上下文组装:builder.build() 作首条 system,env 在尾部 UserMessage,无 merge,session_store 不存 system。"""
import asyncio

from twinkle.agentserver.agent import ReActAgent, AgentRequest, normal_base_sections
from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


class _FinishLLM:
    """LLM stub:第一次 stream 直接 Finish,捕获 messages。"""
    def __init__(self):
        self.captured = None

    async def stream(self, messages, tools):
        self.captured = [dict(m) for m in messages]
        yield Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})


class _TM:
    def schemas(self): return []


def _make_agent(store, *, base_sections=None, hooks=()):
    return ReActAgent(
        _FinishLLM(), store, _TM(),
        hooks=tuple(hooks),
        base_sections=base_sections,
        max_steps=2,
    )


def test_first_message_is_builder_build_and_env_at_tail(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(), hooks=[RuntimeEnvHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())

    msgs = agent._llm.captured
    assert msgs[0]["role"] == "system"
    assert "身份与行为原则" in msgs[0]["content"]
    assert msgs[-1]["role"] == "user"
    assert "<environment_context>" in msgs[-1]["content"]
    assert "当前日期" in msgs[-1]["content"]
    assert "当前日期" not in msgs[0]["content"]


def test_session_store_does_not_persist_system(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(), hooks=[RuntimeEnvHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())

    persisted = store.get_messages("s1")
    assert persisted[0]["role"] == "user"
    assert not any(m["role"] == "system" for m in persisted)


def test_merge_system_messages_deleted():
    assert not hasattr(ReActAgent, "_merge_system_messages")


# --- per-invoke frozen prefix tests --- #


class _MarkerHook(AgentHook):
    """before_invoke: stash a marker section to frozen_sections (simulates SkillHook/MemoryHook)."""
    async def before_invoke(self, ctx: HookContext) -> None:
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("marker", "MARKER-TOKEN-XYZ", priority=50))


def _reg_with_echo_tool():
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    m = ToolManager()
    m.register(echo)
    return m


class _ScriptedLLM:
    """Returns one canned event-list per stream() call, in order; captures system msg each call."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.seen_systems: list[str] = []

    async def stream(self, messages, tools):
        self.seen_systems.append(messages[0]["content"])
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


class _CountingTM:
    """Wraps a ToolManager; counts schemas() calls to verify per-invoke freeze."""
    def __init__(self, inner):
        self._inner = inner
        self.schemas_calls = 0

    def schemas(self):
        self.schemas_calls += 1
        return self._inner.schemas()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_frozen_sections_applied_to_builder(tmp_path):
    """before_invoke stashed section → loop 每步套用到 builder → builder.build() 含它。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(), hooks=[_MarkerHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert "MARKER-TOKEN-XYZ" in agent._llm.captured[0]["content"]


def test_frozen_sections_byte_stable_across_steps(tmp_path):
    """frozen_sections 每步套用 + system prefix 跨步字节一致。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    reg = _reg_with_echo_tool()
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    agent = ReActAgent(llm, store, reg, hooks=(_MarkerHook(),),
                       base_sections=normal_base_sections(), max_steps=3)
    req = AgentRequest(session_id="s1", request_id="r1", query="call echo")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert llm.calls == 2
    assert "MARKER-TOKEN-XYZ" in llm.seen_systems[0]
    assert "MARKER-TOKEN-XYZ" in llm.seen_systems[1]
    assert llm.seen_systems[0] == llm.seen_systems[1]  # byte-stable across steps


def test_tool_schemas_frozen_once_per_invoke(tmp_path):
    """tool_schemas 在 invoke 内只算一次(for-loop 复用),不每步重建。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    tm = _CountingTM(_reg_with_echo_tool())
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    agent = ReActAgent(llm, store, tm, base_sections=normal_base_sections(), max_steps=3)
    req = AgentRequest(session_id="s1", request_id="r1", query="call echo")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert llm.calls == 2            # 跑了 2 步
    assert tm.schemas_calls == 1     # 但 schemas() 只调一次 = 冻结
