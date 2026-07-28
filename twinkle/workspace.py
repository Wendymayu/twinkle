"""Workspace bootstrap — ensure_workspace_dir + seed example skills.

Moved out of config.py so config stays pure constants; this is the only runtime
side-effect module (called at server startup, never at import — keeps tests that
repoint WORKSPACE_DIR via env + importlib.reload(twinkle.config) side-effect-free
on the host).

Reads WORKSPACE_DIR / SKILLS_DIR dynamically from ``twinkle.config`` at call
time (not a top-level snapshot) so a test that reloads twinkle.config to point
the workspace elsewhere sees the new value — this mirrors how the old
in-config ensure_workspace_dir read its own module globals post-reload. The
bundled-resources path is resolved from this file's location (twinkle/workspace.py
sits directly under twinkle/, same as the old config.py, so `resources/skills`
resolves identically).
"""
import os
import shutil
from pathlib import Path

from twinkle import config as _cfg


def ensure_workspace_dir() -> str:
    """Create WORKSPACE_DIR + SKILLS_DIR if missing (idempotent), seed example
    skills on first start. Call at server startup so read/list/glob work on a
    fresh ~/.twinkle without a "not found" error. Not called at import time to
    keep tests (which repoint WORKSPACE_DIR) side-effect-free on the host.
    """
    os.makedirs(_cfg.WORKSPACE_DIR, exist_ok=True)
    os.makedirs(_cfg.SKILLS_DIR, exist_ok=True)
    os.makedirs(_cfg.MEMORY_DIR, exist_ok=True)
    os.makedirs(os.path.join(_cfg.MEMORY_DIR, "daily_memory"), exist_ok=True)
    _seed_example_skills(_cfg.SKILLS_DIR)
    return _cfg.WORKSPACE_DIR


def _seed_example_skills(skills_dir: str) -> None:
    """First-start: copy bundled example skills (twinkle/resources/skills/*) to
    <WORKSPACE>/skills. Skip if target exists (preserve user edits). No-op if
    there are no bundled resources."""
    src = Path(__file__).resolve().parent / "resources" / "skills"
    if not src.is_dir():
        return
    for skill_dir in src.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        dst = Path(skills_dir) / skill_dir.name
        if dst.exists():
            continue  # 用户已有(可能改过),不覆盖
        shutil.copytree(skill_dir, dst)
