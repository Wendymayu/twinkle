"""ExperienceScorer — E/U/F 打分 + 反馈环判定 + 蒸馏清理。

E: 效能（贝叶斯平滑）
U: 利用率
F: 新鲜度（90 天半衰期 + 版本不匹配惩罚）
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from twinkle.agentserver.evolution.types import EvolutionRecord, UsageStats

log = logging.getLogger("twinkle.evolution.scorer")

# 默认权重（可由 config 覆盖）
DEFAULT_W_E = 0.5
DEFAULT_W_U = 0.3
DEFAULT_W_F = 0.2
DEFAULT_HALF_LIFE_DAYS = 90
DEFAULT_STALE_PENALTY = 0.7
DEFAULT_MIN_SCORE = 0.4

EXPERIENCE_EVAL_PROMPT = """你是一个经验评估专家。根据对话片段，评估之前展示给 Agent 的经验是否被有效使用。

## 展示给 Agent 的经验
{presented_experiences}

## 对话片段（展示经验之后的部分）
{conversation_snippet}

## 评估任务
对于每条展示的经验，判断：
1. 该经验是否被 Agent 理解和采纳（内容被用于指导后续行为）
2. 该经验是否产生了积极效果（帮助解决了问题或改进了输出）
3. 该经验是否产生了消极效果（导致错误或误导）

## 输出格式
输出 JSON 数组，每条经验一个对象：
[{{"record_id":"...","used":true/false,"positive":true/false,"negative":true/false,"reason":"简短说明"}}]
只输出 JSON，不要其他内容。"""

SIMPLIFY_PROMPT = """你是一个经验库清理专家。检查以下经验记录，对每条给出清理建议。

## 经验记录
{records_text}

## 清理任务
对每条经验给出建议：
- DELETE: 内容过时/错误/不再相关，或分数极低且无使用记录
- MERGE <id>: 与指定 id 的记录内容重复，合并
- REFINE: 内容有价值但需要改写得更清晰
- KEEP: 内容有效，保留

## 输出格式
[{{"record_id":"ev_xxxxxxxx","action":"DELETE|MERGE|REFINE|KEEP","target_id":"ev_yyyyyyyy 或 null","reason":"简短说明"}}]
只输出 JSON，不要其他内容。"""


def calc_effectiveness(stats: UsageStats | None) -> float:
    """贝叶斯平滑效能: (pos+1)/(pos+neg+2)，Beta(1,1) 先验。无数据 → 0.5。"""
    if stats is None:
        return 0.5
    pos = stats.times_positive
    neg = stats.times_negative
    if pos + neg == 0:
        return 0.5
    return (pos + 1) / (pos + neg + 2)


def calc_utilization(stats: UsageStats | None) -> float:
    """利用率: used/presented。无数据 → 0.5。"""
    if stats is None or stats.times_presented == 0:
        return 0.5
    return stats.times_used / stats.times_presented


def calc_freshness(record: EvolutionRecord, current_skill_version: str | None = None,
                   half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
                   stale_penalty: float = DEFAULT_STALE_PENALTY) -> float:
    """新鲜度衰减: 0.5 + 0.5 * 2^(-days/half_life)。版本不匹配 × penalty。"""
    try:
        ts = datetime.fromisoformat(record.timestamp)
    except (ValueError, TypeError):
        return 0.5
    now = datetime.now(timezone.utc)
    # ts 可能没有 tzinfo，当作 UTC
    if ts.tzinfo is None:
        from datetime import timezone as tz
        ts = ts.replace(tzinfo=tz.utc)
    days_old = max(0, (now - ts).total_seconds() / 86400)
    freshness = 0.5 + 0.5 * math.pow(2, -days_old / max(1, half_life_days))

    # 版本不匹配惩罚
    if (current_skill_version and record.skill_version
            and current_skill_version != record.skill_version):
        freshness *= stale_penalty

    return freshness


def calc_score(record: EvolutionRecord, current_skill_version: str | None = None,
               w_e: float = DEFAULT_W_E, w_u: float = DEFAULT_W_U, w_f: float = DEFAULT_W_F,
               half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
               stale_penalty: float = DEFAULT_STALE_PENALTY) -> float:
    """综合: w_e*E + w_u*U + w_f*F。"""
    stats = record.usage_stats
    e = calc_effectiveness(stats)
    u = calc_utilization(stats)
    f = calc_freshness(record, current_skill_version, half_life_days, stale_penalty)
    return w_e * e + w_u * u + w_f * f


class ExperienceScorer:
    """经验打分 + 效果评估 + 蒸馏。"""

    def __init__(self, llm_client):
        self._llm = llm_client

    # --- 反馈环 ---

    def _format_presented_experiences(self, records: list[EvolutionRecord]) -> str:
        """格式化经验列表供 LLM 评估。"""
        lines: list[str] = []
        for r in records:
            content_preview = r.change.content[:200] if r.change.content else "(empty)"
            lines.append(f"[{r.id}] section={r.change.section}, summary={r.summary or 'N/A'}")
            lines.append(f"  content: {content_preview}")
        return "\n".join(lines)

    async def evaluate(self, skill_name: str, presented_records: list[EvolutionRecord],
                       conversation_snippet: str) -> list[dict]:
        """LLM 逐条判定 used/positive/negative，返 [{record_id, used, positive, negative, reason}]。

        *presented_records*: 本轮注入给 agent 的经验记录
        *conversation_snippet*: 注入后的对话片段（截断到 ~4000 字）
        """
        if not presented_records:
            return []

        snippet = conversation_snippet[:4000]
        presented_text = self._format_presented_experiences(presented_records)

        prompt = EXPERIENCE_EVAL_PROMPT.format(
            presented_experiences=presented_text,
            conversation_snippet=snippet,
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self._llm.chat(messages, tools=None)
            content = resp.choices[0].message.content if resp.choices else ""
            # 尝试提取 JSON 数组
            content = content.strip()
            if content.startswith("```"):
                # 剥 markdown 代码块
                lines = content.splitlines()
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            return json.loads(content)
        except Exception:
            log.exception("evaluate failed for skill=%s", skill_name)
            return []

    def update_score(self, record: EvolutionRecord, eval_result: dict,
                     current_skill_version: str | None = None) -> None:
        """消费 used/positive/negative → 更新 UsageStats → 重算 score。"""
        stats = record.usage_stats or UsageStats()
        if eval_result.get("used"):
            stats.times_used += 1
        if eval_result.get("positive"):
            stats.times_positive += 1
        if eval_result.get("negative"):
            stats.times_negative += 1
        stats.last_evaluated_at = datetime.now(timezone.utc).isoformat()
        record.usage_stats = stats
        record.score = calc_score(record, current_skill_version)

    # --- 蒸馏 ---

    async def simplify(self, skill_name: str, records: list[EvolutionRecord],
                       min_score: float = DEFAULT_MIN_SCORE) -> list[dict]:
        """逐条提 DELETE/MERGE/REFINE/KEEP 建议。

        规则前置：分<min_score 且零调用 → 直接 DELETE，不调 LLM。
        其余送 LLM 判定。
        """
        if not records:
            return []

        results: list[dict] = []
        llm_candidates: list[EvolutionRecord] = []

        for r in records:
            stats = r.usage_stats
            called = (stats.times_used or 0) + (stats.times_positive or 0) + (stats.times_negative or 0)
            if r.score < min_score and called == 0:
                results.append({
                    "record_id": r.id, "action": "DELETE", "target_id": None,
                    "reason": f"score={r.score:.2f}<{min_score} and never called",
                })
            else:
                llm_candidates.append(r)

        if not llm_candidates:
            return results

        # 送 LLM 判定
        records_text = "\n".join(
            f"[{r.id}] score={r.score:.2f} section={r.change.section} "
            f"summary={r.summary or 'N/A'} used={r.usage_stats.times_used if r.usage_stats else 0}"
            for r in llm_candidates
        )
        prompt = SIMPLIFY_PROMPT.format(records_text=records_text)

        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self._llm.chat(messages, tools=None)
            content = resp.choices[0].message.content if resp.choices else ""
            content = content.strip()
            if content.startswith("```"):
                lines = content.splitlines()
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            llm_results = json.loads(content)
            results.extend(llm_results)
        except Exception:
            log.exception("simplify LLM call failed for skill=%s", skill_name)

        return results
