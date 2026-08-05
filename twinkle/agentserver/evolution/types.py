"""Evolution 数据模型 — EvolutionRecord / EvolutionPatch / UsageStats.

对齐 jiuwenswarm openjiuwen/agent_evolving/checkpointing/types.py，去掉 applied 摆设字段。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _make_id() -> str:
    return f"ev_{secrets.token_hex(4)}"


@dataclass
class UsageStats:
    """经验使用统计——由 scorer + 注入层共同维护。"""
    times_presented: int = 0
    times_used: int = 0
    times_positive: int = 0
    times_negative: int = 0
    last_presented_at: str | None = None
    last_evaluated_at: str | None = None


@dataclass
class EvolutionPatch:
    """一条具体的改动内容。"""
    section: str        # Instructions / Examples / Troubleshooting / Scripts / ...
    action: str         # append / merge / replace / skip
    content: str        # Markdown 或脚本源码
    target: str = "body"       # description / body / script
    skip_reason: str | None = None
    merge_target: str | None = None   # 改写哪条已有记录 (ev_xxxxxxxx)
    script_filename: str | None = None
    script_language: str | None = None
    script_purpose: str | None = None
    keywords: list[str] | None = None
    summary: str | None = None


@dataclass
class EvolutionRecord:
    """一条进化经验记录。"""
    id: str                        # ev_<8位hex>
    source: str                    # execution_failure / user_intent / script_artifact / conversation_review
    timestamp: str
    context: str                   # 信号上下文
    change: EvolutionPatch
    score: float = 0.6             # E/U/F 综合分
    usage_stats: UsageStats | None = None
    skill_version: str | None = None
    summary: str | None = None
    # 注意：无 applied 字段。jiuwenswarm 里它是摆设（mark_records_applied 无人调用）。
    # Twinkle 以"渲染进索引块"为生效事实。

    @classmethod
    def make(cls, source: str, context: str, change: EvolutionPatch,
             score: float = 0.6, skill_version: str | None = None,
             summary: str | None = None) -> EvolutionRecord:
        return cls(
            id=_make_id(),
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            context=context,
            change=change,
            score=score,
            usage_stats=UsageStats(),
            skill_version=skill_version,
            summary=summary,
        )


@dataclass
class EvolutionLog:
    """evolutions.json 的顶层结构。"""
    entries: list[EvolutionRecord] = field(default_factory=list)


@dataclass
class ConversationSignal:
    """从对话中检测到的一条信号（信号检测器产出，优化器消费）。"""
    type: str           # execution_failure / script_artifact / user_intent
    skill_name: str     # 归因到的 skill
    context: str        # 摘取的工具调用/对话片段
    msg_index: int = -1 # 在消息列表中的位置


# 种子分（生成时的初值，区别于后续 E/U/F 重算）
INITIAL_SCORE_BY_SIGNAL: dict[str, float] = {
    "execution_failure": 0.65,
    "user_intent": 0.70,
    "script_artifact": 0.60,
    "conversation_review": 0.50,
}

# 失败关键词 —— 信号检测器用
FAILURE_KEYWORDS: list[str] = [
    "error", "exception", "failed", "timeout", "econnrefused",
    "enoent", "permission denied", "traceback", "not found",
    "connection refused", "command not found", "no such file",
]
