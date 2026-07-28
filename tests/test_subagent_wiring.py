"""build_agent_loop wires the subagent executor + spawn_subagent tool +
SubagentContextHook when SUBAGENT_ENABLED, and skips all of it when disabled.
"""


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


def test_build_agent_loop_skips_subagent_when_disabled(monkeypatch):
    import importlib
    import twinkle.config as cfg
    importlib.reload(cfg)
    monkeypatch.setattr(cfg.settings.subagent, "enabled", False)
    monkeypatch.setattr(cfg, "SUBAGENT_ENABLED", False)
    import tempfile, pathlib
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.agentserver.server import build_agent_loop
    store = SessionStore(str(pathlib.Path(tempfile.mkdtemp()) / "sessions"))
    loop = build_agent_loop(store)
    names = {t.card.name for t in loop._tool_manager.list()}
    assert "spawn_subagent" not in names
