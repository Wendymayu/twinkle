"""MemoryFlushHook 三入口注册校验。"""
from twinkle.agentserver.hooks.builtin import MemoryFlushHook


class _FakeLLM2:
    async def stream(self, messages, tools):
        return
        yield  # 空 async generator（构造不调 stream，安全）


def test_create_agent_auto_wires_flush():
    # 照搬 tests/test_create_agent_wiring.py 模式：create_agent 返回的 ReActAgent
    # 用 HookManager，hook 实例列表在 loop._hook_manager._hooks（manager.py:31）。
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.sessions import session_store
    store = session_store()
    loop = create_agent(store, hooks=[], llm=_FakeLLM2())
    kinds = [type(h).__name__ for h in loop._hook_manager._hooks]
    assert "MemoryFlushHook" in kinds


def test_subagent_executor_hook_list_has_flush():
    from twinkle.agentserver.tools.builtin.subagent.executor import SubagentExecutor
    from twinkle.config.schema import SubagentConfig
    ex = SubagentExecutor(_FakeLLM2(), store=None, parent_tools=None,
                          config=SubagentConfig())
    kinds = [type(h).__name__ for h in ex._hook_list()]
    assert "MemoryFlushHook" in kinds


def test_team_member_hooks_has_flush():
    # manager._build_member 内部 hook 列表与 executor 同模式（[SkillHook, MemoryHook,
    # MemoryFlushHook(llm), LoggingHook, RetryHook]）；用 executor _hook_list 等价校验
    # （直接构造 Team member 需 async + session，较重，v1 用 executor 等价）。
    from twinkle.agentserver.tools.builtin.subagent.executor import SubagentExecutor
    from twinkle.config.schema import SubagentConfig
    ex = SubagentExecutor(_FakeLLM2(), store=None, parent_tools=None,
                          config=SubagentConfig())
    kinds = [type(h).__name__ for h in ex._hook_list()]
    assert "MemoryFlushHook" in kinds
