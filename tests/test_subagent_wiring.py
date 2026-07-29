"""build_agent_loop always wires the subagent executor + spawn_subagent tool +
SubagentContextHook (subagent is always on — no enabled switch)."""


def test_build_agent_loop_registers_spawn_subagent_and_context_hook():
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.hooks.base import HookEvent
    import tempfile, pathlib
    store = SessionStore(str(pathlib.Path(tempfile.mkdtemp()) / "sessions"))
    loop = build_agent_loop(store)
    # spawn_subagent registered on the loop's tool manager
    names = {t.card.name for t in loop._tool_manager.list()}
    assert "spawn_subagent" in names
    # SubagentContextHook registered (BEFORE_INVOKE callback present)
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_INVOKE)
    # executor attached
    assert getattr(loop, "_subagent_executor", None) is not None
