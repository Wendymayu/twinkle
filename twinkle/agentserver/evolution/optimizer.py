"""SkillExperienceOptimizer — LLM 生成 EvolutionRecord（上限+去重）。

三渠道：
A - 预检测信号（规则已归因的 failure/script）
B - 执行轨迹直接分析（多次重试才成功的 workaround）
C - 脚本工件提取
"""
from __future__ import annotations

import json
import logging

from twinkle.agentserver.evolution.types import (
    EvolutionRecord, EvolutionPatch, ConversationSignal,
    INITIAL_SCORE_BY_SIGNAL,
)

log = logging.getLogger("twinkle.evolution.optimizer")

SKILL_EXPERIENCE_GENERATE_PROMPT = """你是一个 Skill 经验优化器。根据工具调用的执行结果和对话上下文，为 skill 生成可复用的经验记录。

## 当前 Skill
名称：{skill_name}
内容摘要：
{skill_summary}

## 预检测信号
{signals_text}

## 已有经验（用于去重）
{existing_text}

## 经验生成规则

### 经验来自三个渠道：
A. **预检测信号** — 已由规则引擎归因到当前 skill 的 execution_failure/script_artifact，默认应产出至少一条 append
B. **执行轨迹分析** — Agent 多次重试才成功的 workaround、导致错误的具体调用顺序/参数/前置检查缺失/恢复步骤
C. **脚本工件提取** — Agent 生成并成功执行的脚本（图表/数据处理/自动化），用 target="script"

### 数量限制（严格遵守）：
- 文本经验不超过 2 条，脚本经验不超过 1 条，独立计数互不影响
- 超过则按优先级保留最重要的，其余标 skip：
  1. 导致失败/错误 > 导致低效但最终成功
  2. 高频/可复现 > 单次偶发

### 决策流：
1. 相关性判断 → 不相关标 skip="irrelevant"
2. 去重判断 → 重复标 skip="duplicate"；相似但有增量用 merge_target 改写已有记录；**相似但本轮仍出错 → 优先改写不要跳过**
3. 全新 → 继续
4. 优先级筛选 → top2 文本 + top1 脚本，其余标 skip="low_priority"
5. 定 target（description/body/script）+ section（Instructions/Examples/Troubleshooting/Scripts）

### 输出 JSON 数组：
[
  {{
    "action": "append | skip",
    "skip_reason": "irrelevant | duplicate | low_priority | null",
    "target": "description | body | script",
    "section": "Instructions | Examples | Troubleshooting | Scripts",
    "summary": "一句话中文摘要 | null",
    "keywords": ["6-12 个关键词"],
    "content": "Markdown 或脚本源码 | null（skip 时可为空字符串）",
    "merge_target": "ev_xxxxxxxx 或 null",
    "script_filename": "...",
    "script_language": "...",
    "script_purpose": "..."
  }}
]
只输出 JSON 数组，不要其他内容。"""


class SkillExperienceOptimizer:
    """LLM 驱动的经验记录生成器。"""

    def __init__(self, llm_client):
        self._llm = llm_client

    async def generate_records(self, skill_name: str, signals: list[ConversationSignal],
                               skill_content: str, existing_records: list[dict],
                               max_text: int = 2, max_script: int = 1) -> list[EvolutionRecord]:
        """信号 + SKILL.md + 已有经验 → LLM → EvolutionRecord[]。

        *signals*: 预检测到的信号（可能为空——optimizer 仍可从对话直接分析）
        *skill_content*: SKILL.md 的完整内容
        *existing_records*: 已有经验记录（用于去重）
        *max_text* / *max_script*: 数量上限
        """
        # 构建 prompt
        skill_summary = self._summarize_skill(skill_content)
        signals_text = self._format_signals(signals) if signals else "（无预检测信号，请从执行轨迹直接分析）"
        existing_text = self._format_existing(existing_records) if existing_records else "（无已有经验）"

        prompt = SKILL_EXPERIENCE_GENERATE_PROMPT.format(
            skill_name=skill_name,
            skill_summary=skill_summary,
            signals_text=signals_text,
            existing_text=existing_text,
        )

        # 调 LLM + 解析
        drafts = await self._generate_drafts_with_retries(prompt)
        return self._build_records_from_drafts(drafts, signals, max_text, max_script)

    async def _generate_drafts_with_retries(self, prompt: str, max_retries: int = 2) -> list[dict]:
        """调 LLM 生成 draft JSON，解析失败则重试。"""
        for attempt in range(max_retries + 1):
            try:
                messages = [{"role": "user", "content": prompt}]
                resp = await self._llm.chat(messages, tools=None)
                content = resp.choices[0].message.content if resp.choices else ""
                content = content.strip()

                # 剥 markdown 代码块
                if content.startswith("```"):
                    lines = content.splitlines()
                    content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

                drafts = json.loads(content)
                if isinstance(drafts, list):
                    return drafts
            except Exception:
                log.warning("optimizer draft parse failed, attempt %d/%d", attempt + 1, max_retries + 1)
                if attempt < max_retries:
                    prompt = prompt + "\n\n上一次输出格式无效，请确保输出合法的 JSON 数组。"
        return []

    def _build_records_from_drafts(self, drafts: list[dict], signals: list[ConversationSignal],
                                   max_text: int, max_script: int) -> list[EvolutionRecord]:
        """从 draft dict 构建 EvolutionRecord，强制数量上限。"""
        records: list[EvolutionRecord] = []
        signal_source = signals[0].type if signals else "conversation_review"
        signal_context = signals[0].context if signals else ""
        text_count = 0
        script_count = 0

        for draft in drafts:
            action = draft.get("action", "append")
            target = draft.get("target", "body")
            is_script = target == "script"

            # 强制上限
            if is_script:
                if script_count >= max_script:
                    continue
                script_count += 1
            else:
                if text_count >= max_text:
                    continue
                text_count += 1

            if action == "skip":
                continue

            patch = EvolutionPatch(
                section=draft.get("section", "Troubleshooting"),
                action=action,
                content=draft.get("content", ""),
                target=target,
                skip_reason=draft.get("skip_reason"),
                merge_target=draft.get("merge_target"),
                script_filename=draft.get("script_filename"),
                script_language=draft.get("script_language"),
                script_purpose=draft.get("script_purpose"),
                keywords=draft.get("keywords"),
                summary=draft.get("summary"),
            )
            seed_score = INITIAL_SCORE_BY_SIGNAL.get(signal_source, 0.60)
            record = EvolutionRecord.make(
                source=signal_source,
                context=signal_context,
                change=patch,
                score=seed_score,
                summary=draft.get("summary"),
            )
            records.append(record)

        return records

    # --- 格式化辅助 ---

    @staticmethod
    def _summarize_skill(content: str, max_chars: int = 1500) -> str:
        """截取 SKILL.md 的前 max_chars 字符作为摘要。"""
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + "\n... (truncated)"

    @staticmethod
    def _format_signals(signals: list[ConversationSignal]) -> str:
        lines: list[str] = []
        for s in signals:
            lines.append(f"- [{s.type}] skill={s.skill_name}")
            lines.append(f"  context: {s.context[:300]}")
        return "\n".join(lines)

    @staticmethod
    def _format_existing(records: list[dict]) -> str:
        lines: list[str] = []
        for r in records[-10:]:  # 最多展示最近 10 条
            record_id = r.get("id", "?")
            summary = r.get("summary", "") or r.get("change", {}).get("summary", "") or "N/A"
            section = r.get("change", {}).get("section", "?")
            lines.append(f"- [{record_id}] section={section} summary={summary}")
        return "\n".join(lines)
