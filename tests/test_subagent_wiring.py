"""create_agent always wires the subagent executor + spawn_subagent tool +
SubagentContextHook (subagent is always on). SubagentContextHook is auto-wired
by create_agent — it holds the executor, which is built there from the
loop's llm/store/tools (mirroring jiuwenswarm's adapter binding the executor
onto its stream rail)."""


def test_create_agent_wires_spawn_subagent_and_context_hook():
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.base import HookEvent
    import tempfile, pathlib
    store = SessionStore(str(pathlib.Path(tempfile.mkdtemp()) / "sessions"))
    loop = create_agent(store)
    # spawn_subagent registered on the loop's tool manager
    names = {t.card.name for t in loop._tool_manager.list()}
    assert "spawn_subagent" in names
    # SubagentContextHook auto-wired (BEFORE_INVOKE callback present)
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_INVOKE)
