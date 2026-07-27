import importlib
import twinkle.config as cfg


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
