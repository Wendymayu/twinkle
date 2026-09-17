"""create_agent 在显式传入时装配 PermissionHook。"""
def test_create_agent_wires_permission(monkeypatch):
    import importlib
    import twinkle.config as cfg

    importlib.reload(cfg)
    # 经 config 常量开启（TWINKLE_PERMISSIONS env 在 v1 已移除；
    # permission_engine() 在调用时现读 PERMISSIONS_ENABLED）。
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


def test_create_agent_auto_wires_subagent_and_compression(monkeypatch):
    """最小 AgentLoop（无显式 hooks）自动装配：
    - SubagentContextHook (BEFORE_INVOKE)
    - ContextCompressionHook (BEFORE_MODEL_CALL)
    - MemoryFlushHook (BEFORE_MODEL_CALL)
    - ContextOverflowRecoveryHook (ON_MODEL_EXCEPTION, AFTER_MODEL_CALL)
    - RepeatToolCallDetectorHook (ON_TOOL_EXCEPTION, BEFORE_MODEL_CALL)
    其余（retry/permission/skill/memory/logging）由调用方传入（无依赖），不自动装配。
    evolution.enabled 默认 true 会自动装配 SkillEvolutionHook(AFTER_TOOL_CALL+AFTER_INVOKE);
    本测试聚焦 subagent/compression 装配,关 evolution 隔离之。"""
    import twinkle.agentserver.server as _server
    monkeypatch.setattr(_server, "_EVOLUTION_ENABLED", False)  # 关 evolution 隔离
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
    """RetryHook 无依赖（同 PermissionHook/SkillHook）故由调用方传入；
    create_agent 在传入时装配它，注册 ON_MODEL_EXCEPTION + ON_TOOL_EXCEPTION。"""
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.builtin import RetryHook
    from twinkle.agentserver.hooks.base import HookEvent

    store = session_store()
    loop = create_agent(store, hooks=[RetryHook()])
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_MODEL_EXCEPTION)
    assert loop._hook_manager.has_callbacks_for(HookEvent.ON_TOOL_EXCEPTION)
