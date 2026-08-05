"""skills 包入口 — re-exports + 进程级单例访问器(照 todo/__init__.py 形态)。"""
from twinkle.agentserver.skills.store import Skill, SkillManager, parse_skill_md
from twinkle.agentserver.skills.remote import SkillNetClient, SkillNetError, SkillNetSkill
from twinkle.agentserver.skills.skillhub import SkillHubClient, SkillHubSkill, SkillHubError

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


_SKILLNET_CLIENT: SkillNetClient | None = None


def get_skillnet_client() -> SkillNetClient:
    """进程级单例(惰性,从 config 构造)。测试用 _set_skillnet_client 替换。
    lazy import config 避免 import-time 副作用。"""
    global _SKILLNET_CLIENT
    if _SKILLNET_CLIENT is None:
        from twinkle.config import (
            SKILLS_SKILLNET_API_URL, SKILLS_GITHUB_TOKEN,
            SKILLS_REMOTE_TIMEOUT, SKILLS_REMOTE_MAX_RETRIES,
        )
        _SKILLNET_CLIENT = SkillNetClient(
            skillnet_api_url=SKILLS_SKILLNET_API_URL, github_token=SKILLS_GITHUB_TOKEN,
            timeout=SKILLS_REMOTE_TIMEOUT, max_retries=SKILLS_REMOTE_MAX_RETRIES,
        )
    return _SKILLNET_CLIENT


def _set_skillnet_client(c: SkillNetClient | None) -> None:
    """测试钩子:替换/重置单例。生产代码不调。"""
    global _SKILLNET_CLIENT
    _SKILLNET_CLIENT = c


_SKILLHUB_CLIENT: SkillHubClient | None = None


def get_skillhub_client() -> SkillHubClient:
    """进程级单例(惰性,从 config 构造)。测试用 _set_skillhub_client 替换。
    lazy import config 避免 import-time 副作用。"""
    global _SKILLHUB_CLIENT
    if _SKILLHUB_CLIENT is None:
        from twinkle.config import (
            SKILLS_SKILLHUB_API_URL, SKILLS_REMOTE_TIMEOUT, SKILLS_REMOTE_MAX_RETRIES,
        )
        _SKILLHUB_CLIENT = SkillHubClient(
            skillhub_api_url=SKILLS_SKILLHUB_API_URL,
            timeout=SKILLS_REMOTE_TIMEOUT, max_retries=SKILLS_REMOTE_MAX_RETRIES,
        )
    return _SKILLHUB_CLIENT


def _set_skillhub_client(c: SkillHubClient | None) -> None:
    """测试钩子:替换/重置单例。生产代码不调。"""
    global _SKILLHUB_CLIENT
    _SKILLHUB_CLIENT = c


__all__ = [
    "Skill", "SkillManager", "parse_skill_md",
    "get_skill_manager", "_set_skill_manager",
    "SkillNetClient", "SkillNetError", "SkillNetSkill",
    "get_skillnet_client", "_set_skillnet_client",
    "SkillHubClient", "SkillHubSkill", "SkillHubError",
    "get_skillhub_client", "_set_skillhub_client",
]
