"""Skill registration + workspace seeding tests."""
import importlib


def test_skill_hook_registers_in_create_agent(tmp_path, monkeypatch):
    """create_agent forwards SkillHook; verify it lands as a before_model_call callback."""
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import twinkle.config as cfg
    importlib.reload(cfg)
    from twinkle.agentserver.sessions import session_store
    from twinkle.agentserver.server import create_agent
    from twinkle.agentserver.hooks.builtin import SkillHook
    from twinkle.agentserver.hooks.base import HookEvent
    loop = create_agent(session_store(), hooks=[SkillHook()])
    assert loop._hook_manager.has_callbacks_for(HookEvent.BEFORE_MODEL_CALL)


def test_ensure_workspace_dir_seeds_example_skill(tmp_path, monkeypatch):
    """ensure_workspace_dir mkdirs WORKSPACE + skills + copies the bundled doc-audit example."""
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import twinkle.config as cfg
    importlib.reload(cfg)
    from twinkle.workspace import ensure_workspace_dir
    ensure_workspace_dir()
    # 示例 skill 被拷到 <WORKSPACE>/skills/doc-audit/SKILL.md
    assert (tmp_path / "skills" / "doc-audit" / "SKILL.md").is_file()
