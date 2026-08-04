"""evolution 包入口 — re-exports + 进程级单例访问器（照 skills/__init__.py 形态）。"""
from twinkle.agentserver.evolution.types import (
    EvolutionRecord, EvolutionPatch, UsageStats, EvolutionLog,
    ConversationSignal, INITIAL_SCORE_BY_SIGNAL, FAILURE_KEYWORDS,
)
from twinkle.agentserver.evolution.store import EvolutionStore
from twinkle.agentserver.evolution.scorer import ExperienceScorer
from twinkle.agentserver.evolution.signal_detector import ConversationSignalDetector
from twinkle.agentserver.evolution.optimizer import SkillExperienceOptimizer
from twinkle.agentserver.evolution.orchestrator import OnlineEvolutionOrchestrator

_EVOLUTION_STORE: EvolutionStore | None = None
_ORCHESTRATOR: OnlineEvolutionOrchestrator | None = None


def get_evolution_store() -> EvolutionStore:
    """进程级单例（惰性构造，处处共享同一实例）。"""
    global _EVOLUTION_STORE
    if _EVOLUTION_STORE is None:
        from twinkle.config import SKILLS_DIR
        _EVOLUTION_STORE = EvolutionStore(SKILLS_DIR)
    return _EVOLUTION_STORE


def _set_evolution_store(store: EvolutionStore | None) -> None:
    """测试钩子：替换/重置单例。生产代码不调。"""
    global _EVOLUTION_STORE
    _EVOLUTION_STORE = store


def get_orchestrator() -> OnlineEvolutionOrchestrator:
    """进程级单例 — 编排器（惰性，组合 store + optimizer + scorer + detector）。"""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        from twinkle.agentserver.llm_client import LLMClient
        from twinkle.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT
        llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL, timeout=LLM_TIMEOUT)
        store = get_evolution_store()
        _ORCHESTRATOR = OnlineEvolutionOrchestrator(
            store=store,
            optimizer=SkillExperienceOptimizer(llm),
            scorer=ExperienceScorer(llm),
            detector=ConversationSignalDetector(),
        )
    return _ORCHESTRATOR


def _set_orchestrator(o: OnlineEvolutionOrchestrator | None) -> None:
    """测试钩子：替换/重置单例。生产代码不调。"""
    global _ORCHESTRATOR
    _ORCHESTRATOR = o


__all__ = [
    "EvolutionRecord", "EvolutionPatch", "UsageStats", "EvolutionLog",
    "ConversationSignal", "INITIAL_SCORE_BY_SIGNAL", "FAILURE_KEYWORDS",
    "EvolutionStore", "ExperienceScorer",
    "ConversationSignalDetector", "SkillExperienceOptimizer",
    "OnlineEvolutionOrchestrator",
    "get_evolution_store", "_set_evolution_store",
    "get_orchestrator", "_set_orchestrator",
]
