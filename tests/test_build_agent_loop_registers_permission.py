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


def test_build_agent_loop_auto_wires_only_subagent_callback():
    """Minimal AgentLoop (no explicit hooks) auto-wires ONLY SubagentContextHook
    (BEFORE_INVOKE); retry/permission/skill/memory/logging are caller-passed
    (no deps), not auto-wired."""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = build_agent_loop(store)
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_INVOKE)
    for event in HookEvent:
        if event == HookEvent.BEFORE_INVOKE:
            continue
        assert not loop._hook_manager.has_callbacks_for(event)


def test_build_agent_loop_wires_retry_when_caller_passed():
    """RetryHook has no deps (like PermissionHook/SkillHook) so it's
    caller-passed; build_agent_loop wires it when passed, registering
    ON_MODEL_EXCEPTION + ON_TOOL_EXCEPTION."""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import build_agent_loop
    from twinkle.agentserver.hooks.builtin import RetryHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = build_agent_loop(store, hooks=[RetryHook()])
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_MODEL_EXCEPTION)
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_TOOL_EXCEPTION)
