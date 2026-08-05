"""OnlineEvolutionOrchestrator — 编排一次完整进化流程。

检测→生成→stage→审批→commit→打分。Pending 存内存 dict（v1 不持久化）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from twinkle.agentserver.evolution.types import EvolutionRecord, ConversationSignal

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

    def __init__(self, store, optimizer, scorer, detector, auto_save: bool = False):
        self._store = store
        self._optimizer = optimizer
        self._scorer = scorer
        self._detector = detector
        self._auto_save = auto_save
        # v1: pending 存内存
        self._pending: dict[str, list[EvolutionRecord]] = {}

    # --- evolve 主入口 ---

    async def evolve(self, skill_name: str, conversation_messages: list[dict],
                     signals: list[ConversationSignal] | None = None,
                     skill_content: str | None = None) -> EvolutionResult:
        """编排一次完整进化：检测→生成→stage→(审批?)→commit→打分。

        如果 *signals* 为 None，自动调 detector 检测。
        如果 *skill_content* 为 None，从 store 读 SKILL.md。
        """
        # 守卫：skill 是否存在
        skill_dir = self._store._skill_dir(skill_name)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return EvolutionResult(status="skipped_skill_not_found", skill_name=skill_name,
                                   message=f"skill '{skill_name}' not found")

        # 1. 信号检测
        if signals is None:
            from twinkle.agentserver.skills import get_skill_manager
            all_skills = [s.name for s in get_skill_manager().list_skills()]
            enabled = _get_enabled_signals()
            signals = self._detector.detect(conversation_messages, all_skills, enabled)
            # 只保留归因到当前 skill 的信号
            signals = [s for s in signals if s.skill_name == skill_name]

        if not signals:
            return EvolutionResult(status="no_signals", skill_name=skill_name,
                                   message="no signals detected")

        # 2. 读取 skill 内容 + 已有经验
        if skill_content is None:
            try:
                skill_content = skill_md.read_text(encoding="utf-8")
            except OSError:
                return EvolutionResult(status="skipped_skill_not_found", skill_name=skill_name)

        existing = self._store._read_evolution_log(skill_name)
        existing_dicts = [
            {"id": r.id, "summary": r.summary, "change": {"section": r.change.section, "summary": r.change.summary}}
            for r in existing.entries
        ]

        # 3. LLM 生成经验
        records = await self._optimizer.generate_records(
            skill_name, signals, skill_content, existing_dicts,
        )

        if not records:
            return EvolutionResult(status="no_records", skill_name=skill_name,
                                   message="optimizer produced no records")

        # 4. 审批分支
        if not self._auto_save:
            self._pending.setdefault(skill_name, []).extend(records)
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
            all_records = self._store._read_evolution_log(skill_name)
            self._store.render_evolution_markdown(skill_name, all_records.entries)
            return EvolutionResult(status="auto_approved", skill_name=skill_name, records=records,
                                   message=f"{len(records)} record(s) committed")
        except Exception:
            log.exception("commit failed for skill=%s", skill_name)
            return EvolutionResult(status="persistence_failed", skill_name=skill_name,
                                   message="commit failed")

    # --- 审批操作 ---

    def get_pending(self, skill_name: str | None = None) -> dict[str, list[EvolutionRecord]]:
        """获取待批记录。skill_name 为 None 返全部。"""
        if skill_name:
            return {skill_name: self._pending.get(skill_name, [])}
        return dict(self._pending)

    async def approve(self, skill_name: str, record_ids: list[str] | None = None) -> EvolutionResult:
        """批准 pending 记录并落盘。record_ids 为 None 批准全部。"""
        pending = self._pending.pop(skill_name, [])
        if not pending:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no pending records")

        if record_ids:
            approved = [r for r in pending if r.id in record_ids]
            rejected = [r for r in pending if r.id not in record_ids]
            if rejected:
                self._pending[skill_name] = rejected
        else:
            approved = pending

        if not approved:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no matching records")

        return await self._commit(skill_name, approved)

    async def reject(self, skill_name: str, record_ids: list[str] | None = None) -> EvolutionResult:
        """拒绝 pending 记录。record_ids 为 None 拒绝全部。"""
        pending = self._pending.pop(skill_name, [])
        if not pending:
            return EvolutionResult(status="skipped", skill_name=skill_name, message="no pending records")

        if record_ids:
            kept = [r for r in pending if r.id not in record_ids]
            if kept:
                self._pending[skill_name] = kept

        return EvolutionResult(status="rejected", skill_name=skill_name,
                               message=f"{len(pending) if record_ids is None else len(record_ids)} record(s) rejected")

    # --- 反馈环 ---

    async def run_feedback_loop(self, skill_name: str, presented_ids: list[str],
                                conversation_snippet: str) -> None:
        """反馈环：注入后的对话片段 → LLM 判定效果 → 回写 UsageStats → 重算分。"""
        all_records = self._store._read_evolution_log(skill_name)
        presented = [r for r in all_records.entries if r.id in presented_ids]
        if not presented:
            return

        eval_results = await self._scorer.evaluate(skill_name, presented, conversation_snippet)
        for eval_r in eval_results:
            rid = eval_r.get("record_id")
            for rec in all_records.entries:
                if rec.id == rid:
                    self._scorer.update_score(rec, eval_r)
                    break

        # 重写 evolutions.json（分数已更新在 all_records.entries 里）
        self._store.save_evolution_log(skill_name, all_records.entries)

    # --- 蒸馏 ---

    async def simplify(self, skill_name: str) -> EvolutionResult:
        """蒸馏清理：逐条提 DELETE/MERGE/REFINE/KEEP。"""
        from twinkle.config import EVOLUTION_DISTILL_MIN_SCORE
        all_records = self._store._read_evolution_log(skill_name)
        if not all_records.entries:
            return EvolutionResult(status="no_records", skill_name=skill_name, message="no records to simplify")

        suggestions = await self._scorer.simplify(skill_name, all_records.entries,
                                                   min_score=EVOLUTION_DISTILL_MIN_SCORE)
        # 执行 DELETE
        delete_ids = {s["record_id"] for s in suggestions if s.get("action") == "DELETE"}
        if delete_ids:
            all_records.entries = [r for r in all_records.entries if r.id not in delete_ids]
            self._store.save_evolution_log(skill_name, all_records.entries)
            self._store.render_evolution_markdown(skill_name, all_records.entries)

        return EvolutionResult(status="simplified", skill_name=skill_name,
                               message=f"{len(delete_ids)} deleted, {len(suggestions)} suggestions total")


def _get_enabled_signals() -> set[str]:
    """从 config 读启用的信号类型。"""
    from twinkle.config import EVOLUTION_SIGNAL_FAILURE, EVOLUTION_SIGNAL_SCRIPT, EVOLUTION_SIGNAL_USER_INTENT
    enabled: set[str] = set()
    if EVOLUTION_SIGNAL_FAILURE:
        enabled.add("execution_failure")
    if EVOLUTION_SIGNAL_SCRIPT:
        enabled.add("script_artifact")
    if EVOLUTION_SIGNAL_USER_INTENT:
        enabled.add("user_intent")
    return enabled
