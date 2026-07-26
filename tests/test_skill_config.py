import importlib
import twinkle.config as cfg


def test_skill_config_defaults():
    importlib.reload(cfg)
    assert cfg.SKILL_MODE == "all"
    assert cfg.SKILLS_DIR.endswith("skills")
    assert cfg.ENABLED_SKILLS == []


def test_skill_config_env_override(monkeypatch):
    monkeypatch.setenv("TWINKLE_SKILL_MODE", "auto_list")
    monkeypatch.setenv("TWINKLE_SKILLS_DIR", "/tmp/skills")
    monkeypatch.setenv("TWINKLE_ENABLED_SKILLS", "doc-audit, code-refactor")
    importlib.reload(cfg)
    assert cfg.SKILL_MODE == "auto_list"
    assert cfg.SKILLS_DIR == "/tmp/skills"
    assert cfg.ENABLED_SKILLS == ["doc-audit", "code-refactor"]
