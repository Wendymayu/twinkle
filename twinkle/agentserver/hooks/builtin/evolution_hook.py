"""SkillEvolutionHook — 接线层 Hook（priority≈80），挂在 AFTER_TOOL_CALL + AFTER_INVOKE。

把进化事件路由到核心层 OnlineEvolutionOrchestrator。
after_tool_call 监听 read_skill(SKILL.md) 记经验 presented,after_invoke 跑反馈环+进化。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext, ToolCallInputs

if TYPE_CHECKING:
    from twinkle.agentserver.evolution.orchestrator import OnlineEvolutionOrchestrator

log = logging.getLogger("twinkle.hooks.evolution")


class SkillEvolutionHook(AgentHook):
    priority = 80  # 低于 SkillHook(90)

    def __init__(self, orchestrator: OnlineEvolutionOrchestrator | None = None,
                 enabled: bool = True) -> None:
        self._orchestrator: OnlineEvolutionOrchestrator | None = orchestrator
        self._enabled: bool = enabled
        # 跟踪本次呈现的经验(read_skill 触发),供 after_invoke 反馈环判定
        self._presented_ids_by_skill: dict[str, list[str]] = {}  # skill_name → [record_id, ...]

    # --- after_tool_call: read_skill 呈现 → 记 presented ---

    async def after_tool_call(self, ctx: HookContext) -> None:
        """read_skill(skill,"SKILL.md") = 模型加载该 skill 主体(含经验索引块) →
        记该 skill 全部 non-skip 经验为 presented,供 after_invoke 反馈环判定。

        取代旧 before_model_call 的每步全量注入:经验不再主动灌进 system message,
        只在模型真 read_skill 时才算"被呈现"——presented 计数真实(不读的 skill
        不涨),与 skill body 同走按需加载,不再每步对所有 skill 虚增 times_presented
        让 U 维分母失真。只记 SKILL.md(主体含全部索引);读 sidecar/脚本不重复记。
        """
        if not self._enabled or self._orchestrator is None:
            return
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or inputs.name != "read_skill":
            return
        skill_name = inputs.args.get("skill_name")
        relative_file_path = inputs.args.get("relative_file_path") or "SKILL.md"
        if not skill_name or relative_file_path != "SKILL.md":
            return  # 只记读 SKILL.md(主体含全部索引);sidecar/脚本不重复 +1
        try:
            entries = self._orchestrator._store.read_evolution_log(skill_name).entries
        except Exception:
            return
        presented_ids = [r.id for r in entries if r.change.action != "skip"]
        if presented_ids:
            # 赋值(覆盖)非 append:同 invoke 内多次 read 同 skill 只记一次 presented
            self._presented_ids_by_skill[skill_name] = presented_ids

    # --- after_invoke: 信号检测 + 生成经验 + 反馈环 ---

    async def after_invoke(self, ctx: HookContext) -> None:
        """AFTER_INVOKE: 检测信号 → 生成经验 → stage/commit → 反馈环。"""
        if not self._enabled or self._orchestrator is None:
            return

        # 反馈环：先判定本轮呈现的经验效果
        await self._run_feedback_loop(ctx)

        # 信号检测 + 生成经验
        await self._run_evolution(ctx)

    async def _run_feedback_loop(self, ctx: HookContext) -> None:
        """对本轮呈现的经验，取对话片段做 LLM 效果判定。

        AFTER_INVOKE 时 ctx.inputs 是 InvokeInputs(无 messages 字段)，对话消息
        从 ctx.agent._messages 取(同 _run_evolution)——否则 snippet 恒空、
        run_feedback_loop 永不被调，times_presented 恒 0、EUF 反馈环死。
        """
        if not self._presented_ids_by_skill:
            return

        try:
            agent = ctx.agent
            messages = list(agent._messages) if hasattr(agent, "_messages") else []
        except Exception:
            messages = []

        # 取最后 ~3000 字的对话作为 snippet（呈现之后的近似）
        snippet = ""
        if messages:
            for msg in reversed(messages[-10:]):
                content = str(msg.get("content", ""))
                snippet = content[:500] + "\n" + snippet
                if len(snippet) > 3000:
                    break

        for skill_name, record_ids in self._presented_ids_by_skill.items():
            if record_ids and snippet:
                try:
                    await self._orchestrator.run_feedback_loop(skill_name, record_ids, snippet)
                except Exception:
                    log.exception("feedback loop failed for skill=%s", skill_name)

        self._presented_ids_by_skill.clear()

    async def _run_evolution(self, ctx: HookContext) -> None:
        """扫对话信号触发进化（detect 一次、按 skill 分发，见 orchestrator.evolve_all）。"""
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

        results = await self._orchestrator.evolve_all(messages, [s.name for s in skills])
        for skill_name, result in results.items():
            if result.status not in ("no_signals", "no_records", "skipped_skill_not_found", "skipped"):
                log.info("evolution for %s: %s — %s", skill_name, result.status, result.message)
