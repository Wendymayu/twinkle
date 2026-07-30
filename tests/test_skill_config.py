import importlib

import twinkle.config as cfg


def test_skills_skillnet_defaults():
    # skillnet_api_url 来自打包 config.yaml 的字面量(稳定,非 env 驱动),可直接断言
    # loaded 常量。github_token 是 env 驱动(${TWINKLE_GITHUB_TOKEN:-}),故其默认值在
    # schema 模型上断言,不碰 env-loaded 常量(对 .env hermetic)。
    assert cfg.settings.skills.skillnet_api_url == "http://api-skillnet.openkg.cn"
    assert cfg.SKILLS_SKILLNET_API_URL == "http://api-skillnet.openkg.cn"
    assert cfg.SKILLS_REMOTE_TIMEOUT == 60.0
    assert cfg.SKILLS_REMOTE_MAX_RETRIES == 3
    from twinkle.config.schema import SkillsConfig
    assert SkillsConfig().github_token == ""


def test_skill_config_defaults():
    importlib.reload(cfg)
    assert cfg.SKILL_MODE == "all"
    assert cfg.SKILLS_DIR.endswith("skills")
    assert cfg.ENABLED_SKILLS == []


def test_skill_config_yaml_override(tmp_path):
    """v1: skill mode + enabled are config.yaml literals (env vars removed).
    Override by pointing load_config at a custom YAML; SKILLS_DIR stays
    ${ENV:-default}-overridable (covered by test_config_constants)."""
    from twinkle.config.loader import load_config
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "skills:\n  mode: auto_list\n  dir: /tmp/skills\n"
        "  enabled: [doc-audit, code-refactor]\n", encoding="utf-8")
    c = load_config(custom)
    assert c.skills.mode == "auto_list"
    assert c.skills.dir == "/tmp/skills"
    assert c.skills.enabled == ["doc-audit", "code-refactor"]
