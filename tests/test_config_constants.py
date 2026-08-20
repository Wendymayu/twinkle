import importlib


def test_constants_match_packaged_defaults(monkeypatch):
    # Hermetic against a developer's .env: the YAML reads AGENTSERVER_PORT /
    # GATEWAY_PORT / LLM_MODEL via ${ENV:-default}. _load_env_file re-sets them
    # from .env via setdefault, so delenv alone won't stick. Force the packaged
    # defaults via real env (which wins over .env's setdefault) so the exported
    # constants match the shipped defaults regardless of local .env.
    monkeypatch.setenv("TWINKLE_AGENTSERVER_PORT", "18000")
    monkeypatch.setenv("TWINKLE_GATEWAY_PORT", "19000")
    monkeypatch.setenv("TWINKLE_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("TWINKLE_LLM_API_KEY", raising=False)
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.AGENTSERVER_PORT == 18000
    assert cfg.GATEWAY_PORT == 19000
    assert cfg.LLM_MODEL == "gpt-4o-mini"
    assert cfg.AGENT_MAX_STEPS == 1000
    assert cfg.SKILL_MODE == "all"
    assert cfg.ENABLED_SKILLS == []
    assert cfg.CONTEXT_TOKEN_THRESHOLD == 0  # 0=动态(窗口×trigger_ratio);旧固定 60000 已废
    assert cfg.CONTEXT_KEEP_RECENT_PAIRS == 6
    assert cfg.CONTEXT_SUMMARY_PROMPT.startswith("你是对话上下文压缩器")
    assert cfg.PERMISSIONS_ENABLED is False
    assert cfg.PERMISSIONS_ENABLED_CHANNELS == {"web"}
    assert cfg.PERMISSIONS_GLOBAL_DEFAULT == "allow"
    assert cfg.PERMISSIONS_TOOLS["command_exec"] == "require-approval"
    assert cfg.PERMISSIONS_RULES == []
    assert isinstance(cfg.PERMISSIONS, dict)


def test_workspace_env_derives_paths(monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", "/tmp/twinkle-const-test")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.WORKSPACE_DIR.replace("\\", "/") == "/tmp/twinkle-const-test"
    assert cfg.SESSIONS_DIR.replace("\\", "/").endswith(
        ".twinkle_data/sessions")
    assert cfg.PERMISSION_OVERRIDES_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    importlib.reload(cfg)  # restore for downstream tests
