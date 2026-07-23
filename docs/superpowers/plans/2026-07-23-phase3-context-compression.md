# Phase 3 — 长会话上下文压缩 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 agent loop 喂 LLM 前对超阈值的历史消息做"滑窗 + LLM 摘要"压缩，控 token、保关键事实、不破坏 tool_call 配对。

**Architecture:** 新增独立模块 `context_compression.py`（`estimate_tokens` / `_split_keep_tool_pairs` / `_render_messages_text` / `_summarize` / `compress_messages`），`AgentLoop.run_stream` 在 `get_messages` 与 `llm.stream` 之间插一次压缩。压缩结果不写回 `SessionStore`（history 无损）；复用 `LLMClient.stream` 收集 `TextDelta` 拼摘要，不新增 LLM 方法。

**Tech Stack:** Python asyncio，OpenAI-compatible LLMClient，pytest + `asyncio.run()`（不用 pytest-asyncio）。

**Worktree:** `d:/code/opensource/github/twinkle-nightly`，分支 `nightly/phase-3-6-4`。所有 git 命令 `git -C d:/code/opensource/github/twinkle-nightly ...`。测试 `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest`。

**File Structure:**
- Create: `twinkle/agentserver/context_compression.py` — 纯压缩逻辑（无 llm 依赖以外的副作用）
- Modify: `twinkle/agentserver/agent_loop.py:48-75` — `run_stream` 插压缩调用
- Modify: `twinkle/config.py:86-87` — 加 3 个配置常量
- Create: `tests/test_context_compression.py` — 单测 + 端到端

---

## Task 1: 配置常量

**Files:**
- Modify: `twinkle/config.py`（末尾追加）
- Test: `tests/test_config_context.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config_context.py
from twinkle import config


def test_context_defaults_present():
    assert isinstance(config.CONTEXT_TOKEN_THRESHOLD, int)
    assert isinstance(config.CONTEXT_KEEP_RECENT_PAIRS, int)
    assert config.CONTEXT_TOKEN_THRESHOLD > 0
    assert config.CONTEXT_KEEP_RECENT_PAIRS > 0
    assert isinstance(config.CONTEXT_SUMMARY_PROMPT, str) and config.CONTEXT_SUMMARY_PROMPT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_config_context.py -v`
Expected: FAIL — `AttributeError: module 'twinkle.config' has no attribute 'CONTEXT_TOKEN_THRESHOLD'`

- [ ] **Step 3: 实现**

在 `twinkle/config.py` 末尾追加：

```python

# --- context compression (Phase 3) ---
# When estimated tokens of the session messages exceed this threshold, the
# agent loop compresses prior history (sliding window + LLM summary) before
# feeding the LLM. Estimate is char-based (//3) — imprecise for glm but fine
# for a learning project. See context_compression.py.
CONTEXT_TOKEN_THRESHOLD = int(os.getenv("TWINKLE_CONTEXT_TOKEN_THRESHOLD", "60000"))
CONTEXT_KEEP_RECENT_PAIRS = int(os.getenv("TWINKLE_CONTEXT_KEEP_RECENT_PAIRS", "6"))
CONTEXT_SUMMARY_PROMPT = os.getenv(
    "TWINKLE_CONTEXT_SUMMARY_PROMPT",
    "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、已做决策、工具调用结果，丢弃寒暄与冗余。用中文。",
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_config_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/config.py tests/test_config_context.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: add context compression config"
```

---

## Task 2: `estimate_tokens`

**Files:**
- Create: `twinkle/agentserver/context_compression.py`
- Test: `tests/test_context_compression.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_compression.py
from twinkle.agentserver.context_compression import estimate_tokens


def test_estimate_tokens_empty():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_monotonic():
    small = [{"role": "user", "content": "hi"}]
    big = [{"role": "user", "content": "hi" * 100}]
    assert 0 < estimate_tokens(small) < estimate_tokens(big)


def test_estimate_tokens_handles_content_list_and_tool_calls():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "abc"}]},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "web_fetch", "arguments": '{"url":"x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert estimate_tokens(msgs) > 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.context_compression'`

- [ ] **Step 3: 实现**

```python
# twinkle/agentserver/context_compression.py
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/agentserver/context_compression.py tests/test_context_compression.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: estimate_tokens"
```

---

## Task 3: `_split_keep_tool_pairs`

**Files:**
- Modify: `twinkle/agentserver/context_compression.py`（追加函数）
- Test: `tests/test_context_compression.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_context_compression.py
from twinkle.agentserver.context_compression import _split_keep_tool_pairs


def test_split_returns_all_when_small():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=4)
    assert head == [{"role": "system", "content": "s"}]
    assert middle == []
    assert tail == msgs


def test_split_head_is_system_middle_tail_split():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i}"} for i in range(10)]
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=3)
    assert head == [{"role": "system", "content": "s"}]
    assert middle == [{"role": "user", "content": f"u{i}"} for i in range(1, 8)]
    assert tail == [{"role": "user", "content": "u8"}, {"role": "user", "content": "u9"}]


def test_split_does_not_break_tool_pair():
    # tail 起点落在 tool result 上 → 配对 assistant(tool_calls) 必须被拉进 tail
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i}"} for i in range(5)]
    msgs.append({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "r"})
    # tail_count=1 → tail 候选只有 tool result，但配对 assistant 必须进来
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=1)
    assert tail[0]["role"] == "assistant"
    assert tail[0].get("tool_calls") is not None
    assert tail[-1]["role"] == "tool"
    # head 是 system
    assert head == [{"role": "system", "content": "s"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ImportError: cannot import name '_split_keep_tool_pairs'`

- [ ] **Step 3: 实现**

追加到 `twinkle/agentserver/context_compression.py`：

```python
def _split_keep_tool_pairs(
    msgs: list[dict], tail_count: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split into (head, middle, tail). head = first system message (if any).
    tail = last tail_count msgs, but if the tail starts on a tool-result message,
    walk left so its pairing assistant(tool_calls) is also in tail (a tool result
    without its assistant call in front breaks the OpenAI message contract)."""
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/agentserver/context_compression.py tests/test_context_compression.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: _split_keep_tool_pairs"
```

---

## Task 4: `_render_messages_text` + `_summarize`（fake LLM）

**Files:**
- Modify: `twinkle/agentserver/context_compression.py`
- Test: `tests/test_context_compression.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_context_compression.py
import asyncio

from twinkle.agentserver.context_compression import _render_messages_text, _summarize
from twinkle.agentserver.llm_client import TextDelta, Finish


class FakeLLM:
    """Minimal stub: yields the configured summary text as TextDelta, then a Finish."""
    def __init__(self, summary_text: str = "summary"):
        self.summary_text = summary_text
        self.calls: list = []

    async def stream(self, messages, tools):
        self.calls.append((messages, tools))
        yield TextDelta(self.summary_text)
        yield Finish(finish_reason="stop",
                     assistant_message={"role": "assistant", "content": self.summary_text, "tool_calls": None})


def test_render_messages_text_includes_roles_and_tool_calls():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
    ]
    text = _render_messages_text(msgs)
    assert "[user]" in text and "hello" in text
    assert "tool_call" in text and "t" in text


def test_summarize_collects_textdeltas():
    llm = FakeLLM(summary_text="the summary")
    out = asyncio.run(_summarize(llm, "sysprompt", "middle text"))
    assert out == "the summary"
    # called with system + user, tools=[]
    msgs, tools = llm.calls[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "sysprompt"
    assert msgs[1]["role"] == "user" and "middle text" in msgs[1]["content"]
    assert tools == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ImportError: cannot import name '_render_messages_text'`

- [ ] **Step 3: 实现**

追加到 `twinkle/agentserver/context_compression.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/agentserver/context_compression.py tests/test_context_compression.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: render + summarize helpers"
```

---

## Task 5: `compress_messages`（集成）

**Files:**
- Modify: `twinkle/agentserver/context_compression.py`
- Test: `tests/test_context_compression.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_context_compression.py
from twinkle.agentserver.context_compression import compress_messages, estimate_tokens


def test_compress_noop_under_threshold_returns_copy():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out = asyncio.run(compress_messages(
        msgs, FakeLLM(), token_threshold=10_000, keep_recent_pairs=6,
        summary_system_prompt="p"))
    assert out == msgs
    assert out is not msgs  # a copy, not the same list


def test_compress_over_threshold_summarizes_and_shrinks():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn {i} FACTKEY_{i} " + "x" * 30} for i in range(20)]
    msgs += [{"role": "assistant", "content": f"ans {i} FACTKEY_{i} " + "y" * 30} for i in range(20)]
    llm = FakeLLM(summary_text="摘要含 FACTKEY_5")
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=10, keep_recent_pairs=3, summary_system_prompt="p"))
    assert estimate_tokens(out) < estimate_tokens(msgs)
    # head = system
    assert out[0]["role"] == "system" and out[0]["content"] == "s"
    # a summary system message exists
    assert any("[prior context summary]" in m.get("content", "") for m in out)
    # the last FACTKEY (in tail) survives
    assert any("FACTKEY_19" in m.get("content", "") for m in out)
    # the summary surface fact (fake) survives
    assert any("FACTKEY_5" in m.get("content", "") for m in out)


def test_compress_tool_pair_intact_in_tail():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    msgs.append({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "r" * 30})
    out = asyncio.run(compress_messages(
        msgs, FakeLLM(), token_threshold=10, keep_recent_pairs=1, summary_system_prompt="p"))
    # find the tool result in out, its pairing assistant(tool_calls) must precede it
    idx_tool = next(i for i, m in enumerate(out) if m.get("role") == "tool")
    assert out[idx_tool - 1].get("tool_calls") is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ImportError: cannot import name 'compress_messages'`

- [ ] **Step 3: 实现**

追加到 `twinkle/agentserver/context_compression.py`：

```python
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
    summary = await _summarize(llm, summary_system_prompt, _render_messages_text(middle))
    summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
    return head + [summary_msg] + tail
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/agentserver/context_compression.py tests/test_context_compression.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: compress_messages"
```

---

## Task 6: `AgentLoop.run_stream` 集成

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py`
- Test: `tests/test_agent_loop_compress.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_loop_compress.py
import asyncio

from twinkle.agentserver import agent_loop
from twinkle.agentserver.llm_client import TextDelta, Finish


class _Store:
    def __init__(self, msgs):
        self._msgs = msgs
    def get_messages(self, sid):
        return list(self._msgs)


class _Tools:
    def schemas(self):
        return []
    async def execute(self, name, args):
        return ""


class _Memory:
    def recall(self, q): pass


class _LLM:
    """Records the messages it received, then returns a short stop finish."""
    def __init__(self):
        self.seen = None
    async def stream(self, messages, tools):
        self.seen = messages
        yield TextDelta("ok")
        yield Finish(finish_reason="stop",
                      assistant_message={"role": "assistant", "content": "ok", "tool_calls": None})


def test_run_stream_compresses_before_llm(monkeypatch):
    # Force compression threshold very low so compression triggers.
    monkeypatch.setattr(agent_loop, "CONTEXT_TOKEN_THRESHOLD", 1)
    monkeypatch.setattr(agent_loop, "CONTEXT_KEEP_RECENT_PAIRS", 2)
    monkeypatch.setattr(agent_loop, "CONTEXT_SUMMARY_PROMPT", "p")

    big = [{"role": "system", "content": "s"}]
    big += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    store = _Store(big)
    real_llm = _LLM()
    # the compress step calls llm.stream too — _LLM records the LAST call, which
    # is the real agent turn. To also serve the summary call we just reuse it.
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools(), memory=_Memory())

    import twinkle.e2a.models as e2a
    env = e2a.E2AEnvelope(
        request_id="r1", session_id="s1", channel_id="web",
        method="chat.send", params={"query": "hi"}, sequence=0,
    )
    frames = []
    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)
    asyncio.run(collect())
    # the messages sent to the real LLM turn were compressed (smaller than input)
    assert real_llm.seen is not None
    from twinkle.agentserver.context_compression import estimate_tokens
    assert estimate_tokens(real_llm.seen) < estimate_tokens(big)
    assert real_llm.seen[0]["role"] == "system"  # head preserved
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_agent_loop_compress.py -v`
Expected: FAIL — `AttributeError` on `agent_loop.CONTEXT_TOKEN_THRESHOLD`（未 import）或 run_stream 未压缩（seen == 全量）

- [ ] **Step 3: 实现**

修改 `twinkle/agentserver/agent_loop.py`：

顶部 import 区加（在 `from twinkle.config import AGENT_MAX_STEPS as MAX_STEPS` 之后）：

```python
from twinkle.agentserver.context_compression import compress_messages
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    CONTEXT_KEEP_RECENT_PAIRS,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_TOKEN_THRESHOLD,
)
```
（替换原来的 `from twinkle.config import AGENT_MAX_STEPS as MAX_STEPS` 行）

`run_stream` 中，把：

```python
        msgs = self._store.get_messages(session_id)
        async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
```

改为：

```python
        msgs = self._store.get_messages(session_id)
        msgs = await compress_messages(
            msgs, self._llm,
            token_threshold=CONTEXT_TOKEN_THRESHOLD,
            keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS,
            summary_system_prompt=CONTEXT_SUMMARY_PROMPT,
        )
        async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_agent_loop_compress.py tests/test_context_compression.py -v`
Expected: PASS（注意 `_LLM` 既服务 summary 调用也服务真实 turn；`seen` 记录最后一次 = 真实 turn，已压缩）

- [ ] **Step 5: 跑现有 agent_loop 测试确保未回归**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_agent_loop.py -v`
Expected: PASS（现有测试用短消息，不触发压缩，行为不变）

- [ ] **Step 6: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add twinkle/agentserver/agent_loop.py tests/test_agent_loop_compress.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: integrate compression into run_stream"
```

---

## Task 7: 端到端 100 轮验收

**Files:**
- Test: `tests/test_context_compression.py`（追加）

- [ ] **Step 1: 写测试**

```python
def test_e2e_100_turns_token_down_facts_preserved():
    msgs = [{"role": "system", "content": "sys prompt"}]
    for i in range(100):
        msgs.append({"role": "user", "content": f"第{i}轮 FACTKEY_{i:03d} 提问 " + "甲" * 40})
        msgs.append({"role": "assistant", "content": f"第{i}轮 FACTKEY_{i:03d} 回答 " + "乙" * 40})
    # fake summary mentions a mid-history fact; tail keeps the latest
    llm = FakeLLM(summary_text="历史摘要：提及 FACTKEY_050 与 FACTKEY_070")
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=100, keep_recent_pairs=6, summary_system_prompt="p"))
    assert estimate_tokens(out) < estimate_tokens(msgs)
    # latest fact survives in tail
    assert any("FACTKEY_099" in m.get("content", "") for m in out)
    # mid fact survives via summary
    assert any("FACTKEY_050" in m.get("content", "") for m in out)
    # head system preserved
    assert out[0]["content"] == "sys prompt"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/test_context_compression.py::test_e2e_100_turns_token_down_facts_preserved -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git -C d:/code/opensource/github/twinkle-nightly add tests/test_context_compression.py
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: e2e 100-turn acceptance test"
```

---

## Task 8: 全量回归 + self code-review

- [ ] **Step 1: 跑全量测试**

Run: `cd /d/code/opensource/github/twinkle-nightly && /d/code/opensource/github/twinkle/.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全绿（含既有 test_agent_loop / test_integration 等；本 Phase 新增 4 文件）

- [ ] **Step 2: self code-review**

对 `nightly/phase-3-6-4` 相对 `main` 的 diff 跑 `/code-review` 技能（或派 subagent），修 finding 后 commit：

```bash
git -C d:/code/opensource/github/twinkle-nightly add -A
git -C d:/code/opensource/github/twinkle-nightly commit -m "[nightly] Phase 3: code-review fixes"
```

- [ ] **Step 3: 更新主 worktree PROGRESS.md**

把 `d:/code/opensource/github/twinkle/PROGRESS.md` 的 Phase 3 任务勾选 `[x]`，运行日志追加一行"Phase 3 完成，commits 见 worktree log"。
