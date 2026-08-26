import importlib


def test_constants_match_packaged_defaults(monkeypatch):
    # 隔离开发者本地 .env：YAML 通过 ${ENV:-default} 读取 AGENTSERVER_PORT /
    # GATEWAY_PORT / LLM_MODEL。_load_env_file 经 setdefault 从 .env 重新设置它们，
    # 故仅 delenv 不生效。用真实 env（优先级高于 .env 的 setdefault）强制打包默认值，
    # 使导出的常量与随仓库发布的默认值一致，不受本地 .env 影响。
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
