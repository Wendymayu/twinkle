# tests/test_agent_loop_context_assembly.py
"""loop 上下文组装:builder.build() 作首条 system,env 在尾部 UserMessage,无 merge,session_store 不存 system。"""
import asyncio

from twinkle.agentserver.agent import ReActAgent, AgentRequest, normal_base_sections
from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.skills import SkillManager, _set_skill_manager
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager


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


def test_skill_and_memory_hooks_cooperate_through_loop(tmp_path, monkeypatch):
    """真 SkillHook + 真 MemoryHook 同跑 2 步:两 hook 协作 append 同一 frozen_sections →
    loop 每步套用 → build() 按 priority 升序排(memory_strategy80 < memory_static81 < skills90)→ 跨步字节稳定。

    回归守卫:既有测试只单 hook 或用 _MarkerHook,缺真双 hook 同跑端到端断言。
    未来若某 hook 把 setdefault 改成直接赋值覆写 list,本测试会红。"""
    import twinkle.config

    # --- SkillManager 单例:1 个 skill(镜像 test_skill_hook 的 isolated_skills) ---
    skill_root = tmp_path / "skillroot"
    skill_a = skill_root / "a"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: a\ndescription: desc a\n---\n\nbody\n", encoding="utf-8"
    )
    _set_skill_manager(SkillManager(str(skill_root)))

    # --- MemoryManager 单例:MEMORY.md(镜像 test_memory_hook 的 _with_mgr + _mgr) ---
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY", 12000)
    mgr = MemoryManager(str(tmp_path / "memory"), embed_provider=None)
    mem_body = "项目用 Python 3.12 被动召回"
    mgr.write("MEMORY.md", mem_body, append=True)
    _set_memory_manager(mgr)

    try:
        store = SessionStore(str(tmp_path / "sessions"))
        asyncio.run(store.create_session("s1"))
        reg = _reg_with_echo_tool()
        llm = _ScriptedLLM([
            [Finish("tool_calls", {"role": "assistant", "content": None,
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
            [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
        ])
        agent = ReActAgent(llm, store, reg,
                           hooks=[SkillHook(mode="all"), MemoryHook()],
                           base_sections=normal_base_sections(), max_steps=3)
        req = AgentRequest(session_id="s1", request_id="r1", query="call echo")

        async def _run():
            async for _frame in agent.run(req):
                pass

        asyncio.run(_run())

        # --- 断言:协作契约 ---
        # 1) 真跑了 2 步(2 次模型调用)
        assert llm.calls == 2

        mem_strat_marker = "## 长期记忆"
        skills_marker = "## 可用技能"
        mem_static_marker = "## 被动召回"

        # 2) 两 hook 的 section 在两步都出现
        assert mem_strat_marker in llm.seen_systems[0]
        assert skills_marker in llm.seen_systems[0]
        assert mem_strat_marker in llm.seen_systems[1]
        assert skills_marker in llm.seen_systems[1]

        # 3) opt-in memory_static 也在(证 auto-inject 路径)+ MEMORY.md 正文进前缀
        assert mem_static_marker in llm.seen_systems[0]
        assert mem_body in llm.seen_systems[0]

        # 4) priority 顺序:memory_strategy(80) 在 skills(90) 之前(build 升序排)
        assert llm.seen_systems[0].index(mem_strat_marker) < llm.seen_systems[0].index(skills_marker)

        # 5) 跨步字节稳定
        assert llm.seen_systems[0] == llm.seen_systems[1]
    finally:
        _set_skill_manager(None)
        _set_memory_manager(None)
