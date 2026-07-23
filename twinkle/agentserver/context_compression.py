"""Phase 3: long-conversation context compression.

Sliding-window + LLM summary. When the estimated token count of the session
messages exceeds a threshold, the middle is summarized into one system message,
keeping the head (system prompt) and the recent tail (with tool_call/result
pairs intact). Compression output is NOT written back to SessionStore —
history.json stays lossless; this only shapes what the LLM sees.
"""
from __future__ import annotations

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta


def estimate_tokens(msgs: list[dict]) -> int:
    """Char-based token estimate (//3, CN/EN compromise). No tiktoken dep."""
    total = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total += len(part.get("text", "") or "")
                else:
                    total += len(str(part))
        elif c is not None:
            total += len(str(c))
        tcs = m.get("tool_calls")
        if tcs:
            for tc in tcs:
                fn = tc.get("function") or {}
                total += len(fn.get("name", "") or "")
                total += len(fn.get("arguments", "") or "")
    return total // 3


def _split_keep_tool_pairs(
    msgs: list[dict], tail_count: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split into (head, middle, tail). head = first system message (if any).
    tail = last tail_count msgs, but if the tail starts on a tool-result
    message, walk left so its pairing assistant(tool_calls) is also in tail
    (a tool result without its assistant call in front breaks the OpenAI
    message contract)."""
    n = len(msgs)
    if n <= tail_count:
        head = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
        return head, [], list(msgs)
    tail_start = n - tail_count
    while tail_start > 0 and msgs[tail_start].get("role") == "tool":
        tail_start -= 1
    head = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
    middle = msgs[len(head):tail_start]
    tail = msgs[tail_start:]
    return head, middle, tail


def _render_messages_text(msgs: list[dict]) -> str:
    lines: list[str] = []
    for m in msgs:
        role = m.get("role", "?")
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        lines.append(f"[{role}] {c}")
        tcs = m.get("tool_calls")
        if tcs:
            for tc in tcs:
                fn = tc.get("function") or {}
                lines.append(f"  tool_call: {fn.get('name', '')}({fn.get('arguments', '')})")
    return "\n".join(lines)


async def _summarize(llm: LLMClient, summary_system_prompt: str, middle_text: str) -> str:
    """Call llm.stream (tools=[]) and concatenate all TextDelta fragments."""
    messages = [
        {"role": "system", "content": summary_system_prompt},
        {"role": "user", "content":
            "把以下历史对话压成摘要，保留关键事实与工具结果：\n\n" + middle_text},
    ]
    parts: list[str] = []
    async for ev in llm.stream(messages=messages, tools=[]):
        if isinstance(ev, TextDelta):
            parts.append(ev.content)
    return "".join(parts) or "(无摘要产出)"


async def compress_messages(
    msgs: list[dict],
    llm: LLMClient,
    *,
    token_threshold: int,
    keep_recent_pairs: int,
    summary_system_prompt: str,
) -> list[dict]:
    """If estimated tokens exceed threshold, replace the middle with an LLM
    summary; keep head (system) + recent tail (tool pairs intact). Returns a
    new list; never mutates input. No-op (copy) when under threshold or when
    there is no middle to summarize."""
    if estimate_tokens(msgs) <= token_threshold:
        return list(msgs)
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=keep_recent_pairs * 2)
    if not middle:
        return list(msgs)
    try:
        summary = await _summarize(llm, summary_system_prompt, _render_messages_text(middle))
    except Exception:
        # Summary is an optimization, not load-bearing — degrade to a
        # summary-less sliding window (head + tail, middle dropped) instead
        # of failing the whole agent turn on a transient LLM error.
        return head + tail
    summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
    return head + [summary_msg] + tail
