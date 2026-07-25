"""build_agent_loop wires PermissionHook when explicitly passed."""
def test_build_agent_loop_wires_permission(monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", '{"enabled": true}')
    import importlib
    import twinkle.config as cfg

    importlib.reload(cfg)
    from twinkle.agentserver.sessions import SessionStore, session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import PermissionHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    engine = permission_engine()
    loop = build_agent_loop(store, hooks=[PermissionHook(engine)])
    assert loop._hooks.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_build_agent_loop_without_hooks_has_no_callbacks():
    """Minimal AgentLoop — no PermissionHook, no callbacks, no permission engine."""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = build_agent_loop(store)
    for event in HookEvent:
        assert not loop._hooks.has_callbacks_for(event)
