"""create_agent wires PermissionHook when explicitly passed."""
def test_create_agent_wires_permission(monkeypatch):
    import importlib
    import twinkle.config as cfg

    importlib.reload(cfg)
    # Enable via the config constant (TWINKLE_PERMISSIONS env was removed in v1;
    # permission_engine() reads PERMISSIONS_ENABLED fresh at call time).
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    from twinkle.agentserver.sessions import SessionStore, session_store
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import PermissionHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    engine = permission_engine()
    loop = create_agent(store, hooks=[PermissionHook(engine)])
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_create_agent_auto_wires_subagent_and_compression():
    """Minimal AgentLoop (no explicit hooks) auto-wires:
    - SubagentContextHook (BEFORE_INVOKE)
    - ContextCompressionHook (BEFORE_MODEL_CALL)
    - MemoryFlushHook (BEFORE_MODEL_CALL)
    - ContextOverflowRecoveryHook (ON_MODEL_EXCEPTION, AFTER_MODEL_CALL)
    - RepeatToolCallDetectorHook (ON_TOOL_EXCEPTION, BEFORE_MODEL_CALL)
    The rest (retry/permission/skill/memory/logging) are caller-passed (no deps),
    not auto-wired."""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.base import HookEvent

    auto_wired_events = {
        HookEvent.BEFORE_INVOKE,           # SubagentContextHook
        HookEvent.BEFORE_MODEL_CALL,       # ContextCompressionHook + RepeatToolCallDetectorHook
        HookEvent.AFTER_MODEL_CALL,        # ContextOverflowRecoveryHook
        HookEvent.BEFORE_TOOL_CALL,        # RepeatToolCallDetectorHook
        HookEvent.AFTER_TOOL_CALL,         # RepeatToolCallDetectorHook
        HookEvent.ON_MODEL_EXCEPTION,      # ContextOverflowRecoveryHook
        HookEvent.ON_TOOL_EXCEPTION,       # RepeatToolCallDetectorHook
    }
    store = session_store()
    loop = create_agent(store)
    for event in auto_wired_events:
        assert loop._hook_manager.has_callbacks_for(event), f"expected callbacks for {event}"
    for event in HookEvent:
        if event in auto_wired_events:
            continue
        assert not loop._hook_manager.has_callbacks_for(event), f"unexpected callbacks for {event}"


def test_create_agent_wires_retry_when_caller_passed():
    """RetryHook has no deps (like PermissionHook/SkillHook) so it's
    caller-passed; create_agent wires it when passed, registering
    ON_MODEL_EXCEPTION + ON_TOOL_EXCEPTION."""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.builtin import RetryHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = create_agent(store, hooks=[RetryHook()])
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_MODEL_EXCEPTION)
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_TOOL_EXCEPTION)
