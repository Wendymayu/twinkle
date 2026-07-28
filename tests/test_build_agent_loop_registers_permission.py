"""build_agent_loop wires PermissionHook when explicitly passed."""
def test_build_agent_loop_wires_permission(monkeypatch):
    import importlib
    import twinkle.config as cfg

    importlib.reload(cfg)
    # Enable via the config constant (TWINKLE_PERMISSIONS env was removed in v1;
    # permission_engine() reads PERMISSIONS_ENABLED fresh at call time).
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    from twinkle.agentserver.sessions import SessionStore, session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import PermissionHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    engine = permission_engine()
    loop = build_agent_loop(store, hooks=[PermissionHook(engine)])
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_build_agent_loop_without_hooks_has_no_callbacks(monkeypatch):
    """Minimal AgentLoop — no PermissionHook, no callbacks, no permission engine.

    Subagent wiring is config-driven (SUBAGENT_ENABLED, default True) and
    happens inside build_agent_loop; disable it here so this test keeps its
    original intent: "no *explicitly-passed* hooks → zero callbacks".
    """
    import importlib
    import twinkle.config as cfg
    importlib.reload(cfg)
    monkeypatch.setattr(cfg.settings.subagent, "enabled", False)
    monkeypatch.setattr(cfg, "SUBAGENT_ENABLED", False)
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = build_agent_loop(store)
    for event in HookEvent:
        assert not loop._hook_manager.has_callbacks_for(event)
