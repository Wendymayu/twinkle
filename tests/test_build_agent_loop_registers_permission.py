def test_build_agent_loop_wires_permission(monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", '{"enabled": true}')
    import importlib
    import twinkle.config as cfg

    importlib.reload(cfg)
    from twinkle.agentserver.server import build_agent_loop

    loop, store = build_agent_loop()
    assert loop._permission is not None
    assert loop._permission._enabled is True
    from twinkle.agentserver.hooks.base import HookEvent

    assert loop._hooks.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)
