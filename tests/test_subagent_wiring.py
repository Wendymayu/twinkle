"""create_agent 总是装配 subagent executor + spawn_subagent tool +
SubagentContextHook（subagent 始终开启）。SubagentContextHook 由
create_agent 自动装配——它持有 executor，而 executor 在那里由 loop 的
llm/store/tools 构建（对应 jiuwenswarm 的 adapter 把 executor 绑到其
stream rail 上的做法）。"""


def test_create_agent_wires_spawn_subagent_and_context_hook():
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.base import HookEvent
    import tempfile, pathlib
    store = SessionStore(str(pathlib.Path(tempfile.mkdtemp()) / "sessions"))
    loop = create_agent(store)
    # spawn_subagent 注册在 loop 的 tool manager 上
    names = {t.card.name for t in loop._tool_manager.list()}
    assert "spawn_subagent" in names
    # SubagentContextHook 自动装配（存在 BEFORE_INVOKE 回调）
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_INVOKE)
