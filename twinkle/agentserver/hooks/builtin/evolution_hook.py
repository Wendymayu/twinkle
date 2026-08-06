"""SkillEvolutionHook — 接线层 Hook（priority≈80），挂在 AFTER_INVOKE。

把进化事件路由到核心层 OnlineEvolutionOrchestrator。
同时 before_model_call 注入 top-N 高分经验到 agent context。
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.evolution")


class SkillEvolutionHook(AgentHook):
    priority = 80  # 低于 SkillHook(90)，在 skill 清单注入后运行

    def __init__(self, orchestrator=None, enabled: bool = True) -> None:
        self._orchestrator = orchestrator
        self._enabled = enabled
        # 跟踪本次注入的经验，供 after_invoke 反馈环判定
        self._presented: dict[str, list[str]] = {}  # skill_name → [record_id, ...]

    # --- before_model_call: 注入经验 ---

    async def before_model_call(self, ctx: HookContext) -> None:
        """注入 top-N 高分经验摘要到 system message。"""
        if not self._enabled or self._orchestrator is None:
            return

        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return

        store = self._orchestrator._store
        self._presented.clear()
        experience_lines: list[str] = []

        for skill in skills:
            try:
                top = store.get_records_by_score(skill.name, min_score=0.3, limit=3)
            except Exception:
                continue
            if not top:
                continue
            self._presented[skill.name] = [record.id for record in top]
            experience_lines.append(f"### {skill.name} 的进化经验")
            for record in top:
                # times_presented += 1（由注入层维护，非 scorer）
                if record.usage_stats:
                    record.usage_stats.times_presented += 1
                    from datetime import datetime, timezone
                    record.usage_stats.last_presented_at = datetime.now(timezone.utc).isoformat()
                summary = record.summary or record.change.summary or "no summary"
                content_preview = (record.change.content or "")[:150]
                experience_lines.append(f"- [{record.id}] ({record.change.section}, score={record.score:.2f}) {summary}")
                if content_preview:
                    experience_lines.append(f"  {content_preview}")

        if experience_lines:
            header = "## 技能进化经验\n以下是从历史使用中积累的经验，可能对当前任务有帮助：\n"
            content = header + "\n".join(experience_lines)
            # 赋新 list（不 in-place mutate）
            ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages

    # --- after_invoke: 信号检测 + 生成经验 + 反馈环 ---

    async def after_invoke(self, ctx: HookContext) -> None:
        """AFTER_INVOKE: 检测信号 → 生成经验 → stage/commit → 反馈环。"""
        if not self._enabled or self._orchestrator is None:
            return

        # 反馈环：先判定本轮注入的经验效果
        await self._run_feedback_loop(ctx)

        # 信号检测 + 生成经验
        await self._run_evolution(ctx)

    async def _run_feedback_loop(self, ctx: HookContext) -> None:
        """对本轮注入的经验，取对话片段做 LLM 效果判定。"""
        if not self._presented:
            return

        # 取注入之后的对话片段（简化：取最后几轮消息作为 snippet）
        messages = getattr(ctx.inputs, "messages", []) if hasattr(ctx.inputs, "messages") else []
        snippet = ""
        if messages:
            # 取最后 ~3000 字的对话
            for msg in reversed(messages[-10:]):
                content = str(msg.get("content", ""))
                snippet = content[:500] + "\n" + snippet
                if len(snippet) > 3000:
                    break

        for skill_name, record_ids in self._presented.items():
            if record_ids and snippet:
                try:
                    await self._orchestrator.run_feedback_loop(skill_name, record_ids, snippet)
                except Exception:
                    log.exception("feedback loop failed for skill=%s", skill_name)

        self._presented.clear()

    async def _run_evolution(self, ctx: HookContext) -> None:
        """扫对话信号，触发进化。"""
        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return

        # 从 ctx 取对话消息。HookContext.inputs 在 AFTER_INVOKE 是 InvokeInputs，
        # 消息需要通过 agent loop 的 message store 去取。
        # v1 简化：从 ctx.agent 拿 message store
        try:
            agent = ctx.agent
            messages = list(agent._messages) if hasattr(agent, "_messages") else []
        except Exception:
            log.debug("cannot access agent messages, skipping evolution")
            return

        if not messages:
            return

        skill_names = [s.name for s in skills]
        for skill_name in skill_names:
            try:
                result = await self._orchestrator.evolve(skill_name, messages)
                if result.status not in ("no_signals", "no_records", "skipped_skill_not_found"):
                    log.info("evolution for %s: %s — %s", skill_name, result.status, result.message)
            except Exception:
                log.exception("evolution failed for skill=%s", skill_name)
