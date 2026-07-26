"""skills 包入口 — re-exports + 进程级单例访问器(照 todo/__init__.py 形态)。"""
from twinkle.agentserver.skills.store import Skill, SkillManager, parse_skill_md

_SKILL_MANAGER: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    """进程级单例(惰性构造,处处共享同一实例)。@tool 函数与 SkillHook 都调它,
    便于测试用 _set_skill_manager 替换。lazy import config 避免 import-time 副作用。"""
    global _SKILL_MANAGER
    if _SKILL_MANAGER is None:
        from twinkle.config import SKILLS_DIR, ENABLED_SKILLS
        _SKILL_MANAGER = SkillManager(SKILLS_DIR, ENABLED_SKILLS or None)
    return _SKILL_MANAGER


def _set_skill_manager(mgr: SkillManager | None) -> None:
    """测试钩子:替换/重置单例(配 tmp_path 盘)。生产代码不调。"""
    global _SKILL_MANAGER
    _SKILL_MANAGER = mgr


__all__ = [
    "Skill", "SkillManager", "parse_skill_md",
    "get_skill_manager", "_set_skill_manager",
]
