"""SkillEvolutionHook — 接线层 Hook（priority≈80），挂在 AFTER_TOOL_CALL + AFTER_INVOKE。

把进化事件路由到核心层 OnlineEvolutionOrchestrator。
after_tool_call 监听 read_skill 记经验正文进上下文(top-N 随 SKILL.md 返回值、sidecar 按 section)
为 presented；after_invoke 按 evolution.trigger 分派(默认 after_invoke 跑反馈环+进化；none 不跑；
after_model_call/after_tool_call 回调 deferred 本轮不实现)。
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
                 enabled: bool = True, trigger: str | None = None) -> None:
        self._orchestrator: OnlineEvolutionOrchestrator | None = orchestrator
        self._enabled: bool = enabled
        # 触发点：默认读 config EVOLUTION_TRIGGER；trigger 参数可覆盖(测试用)
        if trigger is None:
            from twinkle.config import EVOLUTION_TRIGGER
            trigger = EVOLUTION_TRIGGER
        self._trigger: str = trigger
        # skill_name → (本轮 presented 的经验 ids, 呈现点消息索引)
        # 呈现点索引用来取"呈现之后"的对话片段做反馈环判定
        self._presented_ids_by_skill: dict[str, tuple[list[str], int]] = {}

    # --- after_tool_call: read_skill 呈现 → 记 presented ---

    async def after_tool_call(self, ctx: HookContext) -> None:
        """read_skill 把经验正文带进上下文 → 记 presented,供 after_invoke 反馈环判定。

        presented = "经验正文进上下文",不是"SKILL.md 被读"。
        - read_skill(skill,"SKILL.md")：已把 top-N 高分经验正文拼进返回值 → 记 top-N 为 presented；
        - read_skill(skill,"evolution/<section>.md") sidecar：该 section 正文进上下文 → 记该 section 全部 non-skip 记录；
        - 其他 read_skill(脚本文件等)：正文不是经验 → 不记。
        呈现点索引 = 当前消息数,供反馈环取呈现后片段。同 invoke 多次 read 同 skill 覆盖(只记最后一次)。
        """
        if not self._enabled or self._orchestrator is None:
            return
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or inputs.name != "read_skill":
            return
        skill_name = inputs.args.get("skill_name")
        relative_file_path = inputs.args.get("relative_file_path") or "SKILL.md"
        if not skill_name:
            return

        # 呈现点消息索引(取呈现之后的对话片段用)
        try:
            presented_index = len(ctx.agent._messages) if hasattr(ctx.agent, "_messages") else 0
        except Exception:
            presented_index = 0

        store = self._orchestrator._store
        try:
            entries = store.read_evolution_log(skill_name).entries
        except Exception:
            return

        if relative_file_path == "SKILL.md":
            # top-N 高分经验正文已随 SKILL.md 返回值进上下文(见 skill_tools._append_top_experiences)
            top_records = store.get_records_by_score(skill_name, limit=3)
            presented_ids = [r.id for r in top_records if r.change.action != "skip"]
        elif (relative_file_path.startswith("evolution/")
              and not relative_file_path.startswith("evolution/scripts/")
              and relative_file_path.endswith(".md")):
            # sidecar evolution/<section>.md：记该 section 全部 non-skip 记录为 presented
            section = relative_file_path[len("evolution/"):-3]  # 去 evolution/ 前缀 + .md 后缀
            presented_ids = [r.id for r in entries
                             if r.change.action != "skip"
                             and (r.change.section or "General") == section]
        else:
            return  # 脚本文件 / 其他路径：不是经验正文,不记

        if presented_ids:
            self._presented_ids_by_skill[skill_name] = (presented_ids, presented_index)

    # --- after_invoke: 按 trigger 分派 反馈环 + 进化 ---

    async def after_invoke(self, ctx: HookContext) -> None:
        """AFTER_INVOKE: 按 evolution.trigger 分派。

        after_invoke(默认) → 跑反馈环 + 进化；
        none → 不自动跑(只手动 RPC)；
        after_model_call/after_tool_call → 对应回调 deferred 未实现,不在 after_invoke 跑(不生效)。
        """
        if not self._enabled or self._orchestrator is None:
            return
        if self._trigger != "after_invoke":
            return
        await self._run_feedback_loop(ctx)
        await self._run_evolution(ctx)

    async def _run_feedback_loop(self, ctx: HookContext) -> None:
        """对本轮 presented 的经验,取呈现点之后的对话片段做 LLM 效果判定。

        snippet 取呈现点之后的消息(all_messages[presented_index:]),不是尾部 10 条——
        避免 read_skill 在前段、后段聊别的导致漏判 used。AFTER_INVOKE 时 ctx.inputs 是
        InvokeInputs(无 messages 字段),消息从 ctx.agent._messages 取(同 _run_evolution)。
        """
        if not self._presented_ids_by_skill:
            return

        try:
            agent = ctx.agent
            all_messages = list(agent._messages) if hasattr(agent, "_messages") else []
        except Exception:
            all_messages = []

        for skill_name, (record_ids, presented_index) in self._presented_ids_by_skill.items():
            # 取呈现点之后的对话片段(正序,截断 ~4000 字)
            post = all_messages[presented_index:] if presented_index < len(all_messages) else []
            snippet = ""
            for msg in post:
                content = str(msg.get("content", ""))
                snippet += content[:1000] + "\n"
                if len(snippet) > 4000:
                    break
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
