"""MemoryFlushHook — 压缩前兜底（spec §3）。

priority 96（> ContextCompressionHook 95），在压缩前先跑：should_compress
为真时，LLM 查「即将丢弃的 middle」有无未持久化重要信息→有则 write_memory
落盘。兜底≠抽取：LLM 读 middle 含 write_memory tool_call 历史，排除已写。

opt-in（memory.flush.enabled 默认关）。无 LLM/should_compress=false/no-op。
fail-soft：任何异常 log + 不崩（兜底是优化非承重）。
"""
from __future__ import annotations

import json
import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.llm_client import TextDelta

log = logging.getLogger("twinkle.memory.flush")

# 兜底器 system prompt（_flush 用 json.loads 解析其 JSON 输出）——项目常量不进 config：用户改坏→解析失败→兜底静默失效
_FLUSH_PROMPT = (
    "你是记忆兜底器。下面是即将被上下文压缩丢弃的对话中段（middle）。\n"
    "检查其中有无【重要但尚未写进长期记忆】的信息：用户偏好/决策/持久事实/当日事件。\n"
    "判定规则：\n"
    "- middle 里的 write_memory 调用已把信息写进记忆的 → 不算漏，排除。\n"
    "- 临时数据、当前任务过程性状态、寒暄、本轮就过期的事 → 不算漏。\n"
    "- 已被覆盖的信息不要重复写。\n"
    "有漏则输出要写的条目（JSON 数组），无漏则输出空数组 []。\n"
    "只输出 JSON，禁止非 JSON 文本（不要代码块、不要解释）：\n"
    '[{"path":"MEMORY.md|USER.md|daily_memory/YYYY-MM-DD.md","content":"要写的内容","append":true}]\n'
    "path 必须是 USER.md / MEMORY.md / daily_memory/YYYY-MM-DD.md 之一。"
)


class MemoryFlushHook(AgentHook):
    priority = 96  # 功能层；高于 ContextCompressionHook(95)

    def __init__(self, llm) -> None:
        self._llm = llm

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.config import MEMORY_FLUSH_ENABLED
        if not MEMORY_FLUSH_ENABLED or self._llm is None:
            return
        try:
            await self._flush_if_compressing(ctx)
        except Exception:
            log.exception("memory flush failed (fail-soft)")

    async def _flush_if_compressing(self, ctx: HookContext) -> None:
        from twinkle.config import CONTEXT_KEEP_RECENT_PAIRS, CONTEXT_TOKEN_THRESHOLD
        from twinkle.agentserver.compression import should_compress
        msgs = ctx.inputs.messages
        if not should_compress(msgs, token_threshold=CONTEXT_TOKEN_THRESHOLD,
                                keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS):
            return
        from twinkle.agentserver.compression import split_messages_head_middle_tail
        _head, middle, _tail = split_messages_head_middle_tail(
            msgs, tail_count=CONTEXT_KEEP_RECENT_PAIRS * 2)
        if not middle:
            return
        await self._flush(middle)

    async def _flush(self, middle: list[dict]) -> tuple[int, int]:
        from twinkle.agentserver.compression import _render_messages_text
        from twinkle.agentserver.memory import get_memory_manager
        middle_text = _render_messages_text(middle)
        parts: list[str] = []
        async for ev in self._llm.stream(
            messages=[{"role": "system", "content": _FLUSH_PROMPT},
                      {"role": "user", "content": middle_text}],
            tools=[],
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.content)
        raw = "".join(parts) or "[]"
        try:
            items = json.loads(raw)
        except Exception:
            log.warning("flush LLM output not JSON, skipping: %.80s", raw)
            return 0, 1
        if not isinstance(items, list):
            return 0, 0
        mgr = get_memory_manager()
        new_writes = 0
        errors = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            path = it.get("path", "")
            content = it.get("content", "")
            append = bool(it.get("append", True))
            try:
                result = mgr.write(path, content, append=append)
                if result.startswith("Error:"):
                    errors += 1
                else:
                    new_writes += 1
            except Exception:
                log.exception("flush write failed for path=%s", path)
                errors += 1
        return new_writes, errors
