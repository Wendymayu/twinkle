"""OnlineEvolutionOrchestrator — 编排一次完整进化流程。

检测→生成→stage→审批→commit→打分。Pending 存内存 dict（v1 不持久化）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from twinkle.agentserver.evolution.types import EvolutionRecord, ConversationSignal

if TYPE_CHECKING:
    from twinkle.agentserver.evolution.store import EvolutionStore
    from twinkle.agentserver.evolution.optimizer import SkillExperienceOptimizer
    from twinkle.agentserver.evolution.scorer import ExperienceScorer
    from twinkle.agentserver.evolution.signal_detector import ConversationSignalDetector

log = logging.getLogger("twinkle.evolution.orchestrator")


@dataclass
class EvolutionResult:
    """一次进化操作的结果。"""
    status: str  # staged / auto_approved / no_signals / no_records / skipped / approved / rejected / persistence_failed
    skill_name: str = ""
    records: list[EvolutionRecord] = field(default_factory=list)
    message: str = ""


class OnlineEvolutionOrchestrator:
    """编排器：组合 detector + optimizer + store + scorer，对外提供一个 evolve() 入口。"""

    def __init__(self, store: EvolutionStore, optimizer: SkillExperienceOptimizer,
                 scorer: ExperienceScorer, detector: ConversationSignalDetector,
                 auto_save: bool = False) -> None:
        self._store: EvolutionStore = store
        self._optimizer: SkillExperienceOptimizer = optimizer
        self._scorer: ExperienceScorer = scorer
        self._detector: ConversationSignalDetector = detector
        self._auto_save: bool = auto_save
        # v1: 待批记录存内存（不持久化，重启即丢）
        self._staged_records: dict[str, list[EvolutionRecord]] = {}

    # --- evolve 主入口 ---

    async def evolve(self, skill_name: str, conversation_messages: list[dict],
                     signals: list[ConversationSignal] | None = None) -> EvolutionResult:
        """编排一次完整进化：检测→生成→stage→(审批?)→commit→打分。

        如果 *signals* 为 None，自动调 detector 检测。
        SKILL.md 内容由本方法内部读取（无外部注入点）。
        """
        # 守卫：skill 是否存在
        skill_md = self._store.skill_md_path(skill_name)
        if not skill_md.exists():
            return EvolutionResult(status="skipped_skill_not_found", skill_name=skill_name,
                                   message=f"skill '{skill_name}' not found")

        # 1. 信号检测：单 skill 只给 detector 喂 [skill_name]（不查全集）
        if signals is None:
            enabled_signals = _get_enabled_signals()
            signals = self._detector.detect(conversation_messages, [skill_name], enabled_signals)
            # 主路归因可能落到别的 skill（agent 读过别的 SKILL.md），只留当前 skill 的
            signals = [s for s in signals if s.skill_name == skill_name]

        if not signals:
            return EvolutionResult(status="no_signals", skill_name=skill_name,
                                   message="no signals detected")

        # 2. 读取 skill 内容 + 已有经验
        try:
            skill_content = skill_md.read_text(encoding="utf-8")
        except OSError:
            return EvolutionResult(status="skipped_skill_not_found", skill_name=skill_name)

        existing_log = self._store.read_evolution_log(skill_name)
        existing_dicts = [
            {"id": r.id, "summary": r.summary, "change": {"section": r.change.section, "summary": r.change.summary}}
            for r in existing_log.entries
        ]

        # 3. LLM 生成经验（数量上限走 config，对齐 simplify 的 DISTILL 模式）
        from twinkle.config import EVOLUTION_MAX_TEXT_RECORDS, EVOLUTION_MAX_SCRIPT_RECORDS
        records = await self._optimizer.generate_records(
            skill_name, signals, skill_content, existing_dicts,
            max_text=EVOLUTION_MAX_TEXT_RECORDS, max_script=EVOLUTION_MAX_SCRIPT_RECORDS,
        )

        if not records:
            return EvolutionResult(status="no_records", skill_name=skill_name,
                                   message="optimizer produced no records")

        # 4. 审批分支
        if not self._auto_save:
            self._staged_records.setdefault(skill_name, []).extend(records)
            return EvolutionResult(status="staged", skill_name=skill_name, records=records,
                                   message=f"{len(records)} record(s) staged for approval")

        # auto_save: 直接落盘
        return await self._commit(skill_name, records)

    async def _commit(self, skill_name: str, records: list[EvolutionRecord]) -> EvolutionResult:
        """落盘 + 重渲染索引块。"""
        try:
            for r in records:
                self._store.append_record(skill_name, r)
            # 重渲染索引块
            evolution_log = self._store.read_evolution_log(skill_name)
            self._store.render_evolution_markdown(skill_name, evolution_log.entries)
            return EvolutionResult(status="auto_approved", skill_name=skill_name, records=records,
                                   message=f"{len(records)} record(s) committed")
        except Exception:
            log.exception("commit failed for skill=%s", skill_name)
            return EvolutionResult(status="persistence_failed", skill_name=skill_name,
                                   message="commit failed")

    # --- 批量进化（detect 一次） ---

    async def evolve_all(self, conversation_messages: list[dict],
                         skill_names: list[str] | None = None) -> dict[str, EvolutionResult]:
        """批量进化：一次检测、按 skill_name 分组、逐个 evolve。

        解决 per-skill 循环调 evolve(signals=None) 让 detector 对同一段对话扫 N 遍的问题：
        detector 只跑一次，结果按 skill_name 分组后预填给各 skill 的 evolve（signals 非 None
        → evolve 跳过自动检测）。per-skill try/except 隔离，一个 skill 炸不影响别的。
        """
        from twinkle.agentserver.skills import get_skill_manager
        if skill_names is None:
            skill_names = [s.name for s in get_skill_manager().list_skills()]
        if not skill_names:
            return {}

        enabled_signals = _get_enabled_signals()
        all_signals = self._detector.detect(conversation_messages, skill_names, enabled_signals)
        by_skill: dict[str, list[ConversationSignal]] = {}
        for sig in all_signals:
            by_skill.setdefault(sig.skill_name, []).append(sig)

        results: dict[str, EvolutionResult] = {}
        for skill_name in skill_names:
            try:
                results[skill_name] = await self.evolve(
                    skill_name, conversation_messages, signals=by_skill.get(skill_name, []))
            except Exception:
                log.exception("evolve failed for skill=%s", skill_name)
                results[skill_name] = EvolutionResult(
                    status="skipped", skill_name=skill_name, message="evolve raised")
        return results

    # --- 审批操作 ---

    def get_staged_records(self, skill_name: str | None = None) -> dict[str, list[EvolutionRecord]]:
        """获取待批记录。skill_name 为 None 返全部。"""
        if skill_name:
            return {skill_name: self._staged_records.get(skill_name, [])}
        return dict(self._staged_records)

    async def approve(self, skill_name: str, record_ids: list[str] | None = None) -> EvolutionResult:
        """批准待批记录并落盘。record_ids 为 None 批准全部。"""
        staged = self._staged_records.pop(skill_name, [])
        if not staged:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no staged records")

        if record_ids:
            approved = [r for r in staged if r.id in record_ids]
            rejected = [r for r in staged if r.id not in record_ids]
            if rejected:
                self._staged_records[skill_name] = rejected
        else:
            approved = staged

        if not approved:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no matching records")

        return await self._commit(skill_name, approved)

    async def reject(self, skill_name: str, record_ids: list[str] | None = None) -> EvolutionResult:
        """拒绝待批记录。record_ids 为 None 拒绝全部。"""
        staged = self._staged_records.pop(skill_name, [])
        if not staged:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no staged records")

        if record_ids:
            kept = [r for r in staged if r.id not in record_ids]
            if kept:
                self._staged_records[skill_name] = kept

        return EvolutionResult(status="rejected", skill_name=skill_name,
                               message=f"{len(staged) if record_ids is None else len(record_ids)} record(s) rejected")

    # --- 反馈环 ---

    async def run_feedback_loop(self, skill_name: str, presented_ids: list[str],
                                conversation_snippet: str) -> None:
        """反馈环：判定本轮呈现经验的效果 → 回写 UsageStats → 重算分 → 落盘。

        presented_records 是 evolution_log.entries 的子集（列表推导保留对象引用、非副本），
        对它们的原地修改（计数 + update_score）都落在 entries 上，末尾一次 save 全部落盘。
        呈现计数在此累加而非在 after_tool_call 呈现时：呈现层只记 ids（对象不入盘），
        此处从 store fresh 读出后 +1 再 save，否则 evolutions.json 里 times_presented 恒 0、
        U 维（used/presented）永走 0.5 兜底。
        """
        evolution_log = self._store.read_evolution_log(skill_name)
        presented_records = [r for r in evolution_log.entries if r.id in presented_ids]
        if not presented_records:
            return

        # 1. 呈现计数 +1
        self._bump_presented_counts(presented_records)
        # 2. LLM 判定每条经验是否被采纳 / 正面 / 负面
        eval_results = await self._scorer.evaluate(skill_name, presented_records, conversation_snippet)
        # 3. 回写判定 → 重算分（原地改 presented_records，即改 entries）
        self._apply_eval_results(eval_results, presented_records)
        # 4. 一次落盘：计数与分数都已原地改在 entries 上
        self._store.save_evolution_log(skill_name, evolution_log.entries)

    def _bump_presented_counts(self, records: list[EvolutionRecord]) -> None:
        """times_presented +1 并打 last_presented_at。原地改 records（即 entries 子集）。"""
        from datetime import datetime, timezone
        from twinkle.agentserver.evolution.types import UsageStats
        for rec in records:
            if rec.usage_stats is None:
                rec.usage_stats = UsageStats()
            rec.usage_stats.times_presented += 1
            rec.usage_stats.last_presented_at = datetime.now(timezone.utc).isoformat()

    def _apply_eval_results(self, eval_results: list[dict],
                            presented_records: list[EvolutionRecord]) -> None:
        """把 LLM 判定回写到对应记录的 UsageStats 并重算 score。原地改 presented_records。

        evaluate 只把 presented_records 喂给 LLM，故它只会回这些 id；在 presented_records
        内遍历即得目标记录，rec 直接来自该列表——与末尾 save 的 entries 同源，身份自明。
        """
        for eval_r in eval_results:
            rid = eval_r.get("record_id")
            for rec in presented_records:
                if rec.id == rid:
                    self._scorer.update_score(rec, eval_r)
                    break

    # --- 蒸馏 ---

    async def simplify(self, skill_name: str) -> EvolutionResult:
        """蒸馏清理：逐条提 DELETE/MERGE/REFINE/KEEP。"""
        from twinkle.config import EVOLUTION_DISTILL_MIN_SCORE
        evolution_log = self._store.read_evolution_log(skill_name)
        if not evolution_log.entries:
            return EvolutionResult(status="no_records", skill_name=skill_name, message="no records to simplify")

        suggestions = await self._scorer.simplify(skill_name, evolution_log.entries,
                                                   min_score=EVOLUTION_DISTILL_MIN_SCORE)
        # 执行 DELETE
        delete_ids = {s["record_id"] for s in suggestions if s.get("action") == "DELETE"}
        if delete_ids:
            evolution_log.entries = [r for r in evolution_log.entries if r.id not in delete_ids]
            self._store.save_evolution_log(skill_name, evolution_log.entries)
            self._store.render_evolution_markdown(skill_name, evolution_log.entries)

        return EvolutionResult(status="simplified", skill_name=skill_name,
                               message=f"{len(delete_ids)} deleted, {len(suggestions)} suggestions total")


def _get_enabled_signals() -> set[str]:
    """从 config 读启用的信号类型。"""
    from twinkle.config import EVOLUTION_SIGNAL_FAILURE, EVOLUTION_SIGNAL_SCRIPT, EVOLUTION_SIGNAL_USER_INTENT
    enabled_signals: set[str] = set()
    if EVOLUTION_SIGNAL_FAILURE:
        enabled_signals.add("execution_failure")
    if EVOLUTION_SIGNAL_SCRIPT:
        enabled_signals.add("script_artifact")
    if EVOLUTION_SIGNAL_USER_INTENT:
        enabled_signals.add("user_intent")
    return enabled_signals
