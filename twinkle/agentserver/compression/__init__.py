"""Phase 3: long-conversation context compression.

Sliding-window + LLM summary. When the estimated token count of the session
messages exceeds a threshold, the middle is summarized into one system message,
keeping the head (system prompt) and the recent tail (with tool_call/result
pairs intact). Compression output is NOT written back to SessionStore —
history.json stays lossless; this only shapes what the LLM sees.
"""
from __future__ import annotations

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.config import (
    MICRO_COMPACT_TRIGGER_THRESHOLD,
    MICRO_COMPACT_KEEP_RECENT_PER_TOOL,
    MICRO_COMPACT_COMPACTABLE_TOOL_NAMES,
    MICRO_COMPACT_CLEARED_MARKER,
    TOOL_RESULT_BUDGET_TOKENS_THRESHOLD,
    TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD,
    TOOL_RESULT_BUDGET_TRIM_SIZE,
    TOOL_RESULT_BUDGET_PROTECT_LATEST,
    CONTEXT_SUMMARY_PROMPT_MODE,
)


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


def _tool_result_budget(msgs: list[dict]) -> list[dict]:
    """ToolResultBudget: 所有 tool 结果 token 总量超 tokens_threshold → 把最大单条
    (> large_message_threshold 的候选里最大)换成 trim_size 字符预览。
    protect_latest: 最新 N 条 tool result 永不 offload。不触发则原样 copy。
    只改"发 LLM 这份",history.json 不动。"""
    tool_indices = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    if not tool_indices:
        return list(msgs)
    total = sum(estimate_tokens([msgs[i]]) for i in tool_indices)
    if total <= TOOL_RESULT_BUDGET_TOKENS_THRESHOLD:
        return list(msgs)
    protect = TOOL_RESULT_BUDGET_PROTECT_LATEST
    exempt_count = min(protect, len(tool_indices))
    eligible = tool_indices[:-exempt_count] if exempt_count else tool_indices[:]
    candidates = [i for i in eligible
                 if estimate_tokens([msgs[i]]) > TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD]
    if not candidates:
        return list(msgs)
    target = max(candidates, key=lambda i: estimate_tokens([msgs[i]]))
    out = list(msgs)
    content = out[target].get("content", "")
    if isinstance(content, str):
        out[target] = {
            **out[target],
            "content": content[:TOOL_RESULT_BUDGET_TRIM_SIZE]
                + f"\n[...trimmed, original {len(content)} chars in history.json]",
        }
    return out


def _micro_compact(msgs: list[dict]) -> list[dict]:
    """MicroCompact: 同工具名下,可清条数(总数-keep_recent) > trigger 才清,
    留最近 keep_recent_per_tool 条原文,更旧清成 cleared_marker。
    只清 compactable_tool_names 里的工具。不改输入,返回新 list。"""
    # 建 tool_call_id → tool_name 映射(扫所有 assistant.tool_calls)
    name_by_id: dict[str | None, str | None] = {}
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            name_by_id[tc.get("id")] = fn.get("name")
    # 按工具名分组 tool 消息的索引(时序)
    groups: dict[str, list[int]] = {}
    for i, m in enumerate(msgs):
        if m.get("role") != "tool":
            continue
        name = name_by_id.get(m.get("tool_call_id"))
        if name in MICRO_COMPACT_COMPACTABLE_TOOL_NAMES:
            groups.setdefault(name, []).append(i)
    clear_indices: set[int] = set()
    for _name, idxs in groups.items():
        keep = MICRO_COMPACT_KEEP_RECENT_PER_TOOL
        clearable = len(idxs) - keep
        if clearable > MICRO_COMPACT_TRIGGER_THRESHOLD:
            for i in idxs[:-keep]:
                clear_indices.add(i)
    if not clear_indices:
        return list(msgs)
    out = list(msgs)
    for i in clear_indices:
        out[i] = {**out[i], "content": MICRO_COMPACT_CLEARED_MARKER}
    return out


def precompress_messages(msgs: list[dict]) -> list[dict]:
    """块2: 无 LLM 两层前置压缩。链B顺序 ToolResultBudget → MicroCompact。
    只改"发 LLM 这份",history.json 原文不动。不改输入,返回新 list。"""
    msgs = _tool_result_budget(msgs)
    msgs = _micro_compact(msgs)
    return msgs


def split_messages_head_middle_tail(
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


_STRUCTURED_SUMMARY_PROMPT = (
    "你是对话上下文压缩器。把下面即将被压缩丢弃的对话中段压成结构化摘要，"
    "供接续的 AI 继续工作。按以下固定 4 节输出，每节有内容才写，无内容写\"(无)\"：\n\n"
    "## 关键事实与决定\n- [定下的关键事实、用户偏好、已做决策]\n\n"
    "## 已用工具与文件\n- [已调用过的工具及涉及/确认过的文件路径、函数名]\n\n"
    "## 待办与当前任务\n- [尚未完成的任务、当前正在做的事、用户的原始目标]\n\n"
    "## 错误与修复\n- [遇到过的错误及如何修复的，便于避免重复踩坑]\n\n"
    "保留确切的文件路径、函数名、错误信息原文。不要寒暄、不要复述过程。用中文。"
)


async def _summarize(llm: LLMClient, summary_system_prompt: str, middle_text: str) -> str:
    """Call llm.stream (tools=[]) and concatenate all TextDelta fragments.
    summary_prompt_mode: structured → 用硬编码 4 节常量；free → 用传入的 summary_system_prompt。"""
    sys_prompt = (_STRUCTURED_SUMMARY_PROMPT if CONTEXT_SUMMARY_PROMPT_MODE == "structured"
                  else summary_system_prompt)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content":
            "把以下历史对话压成摘要，保留关键事实与工具结果：\n\n" + middle_text},
    ]
    parts: list[str] = []
    async for ev in llm.stream(messages=messages, tools=[]):
        if isinstance(ev, TextDelta):
            parts.append(ev.content)
    return "".join(parts) or "(无摘要产出)"


def should_compress(msgs: list[dict], *, token_threshold: int,
                    keep_recent_pairs: int) -> bool:
    """两道闸:token 闸 + middle 闸。True 表示确实要压缩。

    抽成独立谓词供 compress_messages 委派、也供 instrumentor 判定是否产 span
    (避免 patch 执行函数时产生假阳性 span)。
    """
    if estimate_tokens(msgs) <= token_threshold:
        return False
    _head, middle, _tail = split_messages_head_middle_tail(msgs, tail_count=keep_recent_pairs * 2)
    return bool(middle)


async def do_compress(msgs: list[dict], llm: "LLMClient", *,
                      keep_recent_pairs: int,
                      summary_system_prompt: str) -> list[dict]:
    """真正执行压缩。假设 should_compress 已为 True(仍保留 `if not middle`
    兜底以防被直接调用)。含 _summarize 的 LLM 调用。返回新 list,不改输入。"""
    head, middle, tail = split_messages_head_middle_tail(msgs, tail_count=keep_recent_pairs * 2)
    if not middle:
        return list(msgs)
    try:
        summary = await _summarize(llm, summary_system_prompt, _render_messages_text(middle))
    except Exception:
        # 摘要是优化非承重——降级为无摘要滑窗(head+tail,丢 middle)
        return head + tail
    summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
    return head + [summary_msg] + tail


async def compress_messages(
    msgs: list[dict],
    llm: "LLMClient",
    *,
    token_threshold: int,
    keep_recent_pairs: int,
    summary_system_prompt: str,
) -> list[dict]:
    """薄壳:先无 LLM 前置压缩(precompress)→ 判定 → 委派。

    precompress 在 should_compress 之前:先降 token,可能 precompress 完就低于阈值
    → 省一次 LLM 摘要(对齐 jiuwenswarm 成本递进)。No-op 当 precompress 后仍不超阈值。
    do_compress 经模块 globals 解析,被 instrumentor patch 后到达 wrapper。"""
    precompressed = precompress_messages(msgs)
    if not should_compress(precompressed, token_threshold=token_threshold,
                           keep_recent_pairs=keep_recent_pairs):
        return precompressed
    return await do_compress(precompressed, llm, keep_recent_pairs=keep_recent_pairs,
                             summary_system_prompt=summary_system_prompt)
