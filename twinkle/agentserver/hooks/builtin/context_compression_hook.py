"""ContextCompressionHook — before_model_call 压缩历史。

每步主动压缩（与原内联调用等价）：估算 token 超 threshold 时，把 middle
LLM 摘要成一条 system 消息，保留 head(system)+tail(最近 N 对,tool 配对闭合)。
压缩结果不写回 SessionStore,只改 ctx.inputs.messages(赋新 list,不 in-place)。
复用 context_compression.compress_messages,算法逻辑不变,只换调用方。

阈值传 None 时从 config 读(生产),测试可直传(仿 SkillHook.mode)。
"""
from __future__ import annotations

from twinkle.agentserver.context_compression import compress_messages
from twinkle.agentserver.hooks.base import AgentHook, HookContext


class ContextCompressionHook(AgentHook):
    priority = 95  # 功能层;高于 SkillHook(90)/MemoryHook(80),确保先跑、看原始 session 消息

    def __init__(self, llm, *, token_threshold=None, keep_recent_pairs=None, summary_prompt=None):
        self._llm = llm
        self._token_threshold = token_threshold
        self._keep_recent_pairs = keep_recent_pairs
        self._summary_prompt = summary_prompt

    async def before_model_call(self, ctx: HookContext) -> None:
        compressed = await compress_messages(
            ctx.inputs.messages, self._llm,
            token_threshold=self._token_threshold or _get_token_threshold(),
            keep_recent_pairs=self._keep_recent_pairs or _get_keep_recent_pairs(),
            summary_system_prompt=self._summary_prompt or _get_summary_prompt(),
        )
        ctx.inputs.messages = compressed


def _get_token_threshold():
    from twinkle.config import CONTEXT_TOKEN_THRESHOLD
    return CONTEXT_TOKEN_THRESHOLD


def _get_keep_recent_pairs():
    from twinkle.config import CONTEXT_KEEP_RECENT_PAIRS
    return CONTEXT_KEEP_RECENT_PAIRS


def _get_summary_prompt():
    from twinkle.config import CONTEXT_SUMMARY_PROMPT
    return CONTEXT_SUMMARY_PROMPT
