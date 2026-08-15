# Compression 对齐 jiuwenswarm A 档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 compression 模块加两层无 LLM 前置压缩（MicroCompact + ToolResultBudget + protect_latest）、`compress_messages` 内部前置 `precompress_messages`、摘要 prompt 结构化 4 节，对齐 jiuwenswarm 压缩骨干。

**Architecture:** 在 `compression/__init__.py` 内加 `precompress_messages`（链B顺序 ToolResultBudget→MicroCompact，无 LLM，GET 每步重算不改 history），`compress_messages` 在 `should_compress` 前调它先降 token（可能省一次 LLM 摘要）；`_summarize` 按 `summary_prompt_mode` 选 `structured`（硬编码 4 节常量）/ `free`（config `summary_prompt`）作 system prompt。config 加 `micro_compact`/`tool_result_budget`/`summary_prompt_mode` 子 model。零冲突：不动 server/hooks/`memory_flush_hook` 依赖的函数签名/instrumentor/memory。

**Tech Stack:** Python、pydantic（`_StrictModel` extra=forbid）、`asyncio.run` 测试（无 pytest-asyncio）、OTel instrumentor（monkey-patch）。

**Spec:** [docs/superpowers/specs/2026-08-15-compression-align-jiuwenswarm-a.md](../specs/2026-08-15-compression-align-jiuwenswarm-a.md)

**执行前：** 建议开 worktree（干净 HEAD，隔离 memory 会话的脏改动）— 用 `superpowers:using-git-worktrees` skill。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `twinkle/config/schema.py` | pydantic config model | 加 `MicroCompactConfig`/`ToolResultBudgetConfig`；`ContextCompressionConfig` 加 `summary_prompt_mode`/`micro_compact`/`tool_result_budget` |
| `twinkle/resources/config.yaml` | 用户可改配置源 | `context_compression` 段加 `summary_prompt_mode`/`micro_compact`/`tool_result_budget` |
| `twinkle/config/__init__.py` | 常量导出 | 导出 9 个新常量 |
| `twinkle/agentserver/compression/__init__.py` | 压缩核心 | 加 `_tool_result_budget`/`_micro_compact`/`precompress_messages`/`_STRUCTURED_SUMMARY_PROMPT`；改 `_summarize`（mode 分支）+ `compress_messages`（内部前置） |
| `tests/test_compression_config.py` | config 默认值 | 新建 |
| `tests/test_precompress.py` | 两层前置单测 + 整合 | 新建 |
| `tests/test_context_compression.py` | 摘要 mode + 现有回归 | 改（更新 `_summarize` 测试 + 加 mode 测试） |

**不动**：`server.py`、`hooks/*`、`memory_flush_hook.py`、`instrumentors/compression.py`、`memory/*`、`do_compress` 签名、`should_compress`/`split_messages_head_middle_tail`/`_render_messages_text` 签名。

---

## Task 1: config 基础（schema + yaml + 常量导出）

**Files:**
- Modify: `twinkle/config/schema.py`
- Modify: `twinkle/resources/config.yaml:25-28`
- Modify: `twinkle/config/__init__.py:69-72`
- Create: `tests/test_compression_config.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_compression_config.py`:

```python
from twinkle.config import (
    settings,
    SUMMARY_PROMPT_MODE,
    MICRO_COMPACT_TRIGGER_THRESHOLD,
    MICRO_COMPACT_KEEP_RECENT_PER_TOOL,
    MICRO_COMPACT_COMPACTABLE_TOOL_NAMES,
    MICRO_COMPACT_CLEARED_MARKER,
    TOOL_RESULT_BUDGET_TOKENS_THRESHOLD,
    TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD,
    TOOL_RESULT_BUDGET_TRIM_SIZE,
    TOOL_RESULT_BUDGET_PROTECT_LATEST,
)


def test_compression_config_defaults():
    assert SUMMARY_PROMPT_MODE == "structured"
    assert MICRO_COMPACT_TRIGGER_THRESHOLD == 5
    assert MICRO_COMPACT_KEEP_RECENT_PER_TOOL == 3
    assert MICRO_COMPACT_COMPACTABLE_TOOL_NAMES == [
        "read_file", "grep", "glob", "command_exec", "web_fetch", "web_search"]
    assert MICRO_COMPACT_CLEARED_MARKER == "[Old tool result content cleared]"
    assert TOOL_RESULT_BUDGET_TOKENS_THRESHOLD == 9000
    assert TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD == 3000
    assert TOOL_RESULT_BUDGET_TRIM_SIZE == 3000
    assert TOOL_RESULT_BUDGET_PROTECT_LATEST == 1


def test_compression_config_nested_models_loaded():
    assert settings.context_compression.micro_compact.keep_recent_per_tool == 3
    assert settings.context_compression.tool_result_budget.protect_latest == 1
    assert settings.context_compression.summary_prompt_mode == "structured"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compression_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'SUMMARY_PROMPT_MODE'`（常量未导出）

- [ ] **Step 3: 实现 schema.py**

在 `twinkle/config/schema.py` 的 `ContextCompressionConfig` **之前**插入两个子 model，并扩展 `ContextCompressionConfig`。

把现有：
```python
class ContextCompressionConfig(_StrictModel):
    token_threshold: int = 60000
    keep_recent_pairs: int = 6
    summary_prompt: str = (
        "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、"
        "已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
    )
```
替换为：
```python
class MicroCompactConfig(_StrictModel):
    trigger_threshold: int = 5           # 可清条数(总数-keep) > trigger 才清
    keep_recent_per_tool: int = 3       # 每工具留最近 N 条原文
    compactable_tool_names: list[str] = [
        "read_file", "grep", "glob", "command_exec", "web_fetch", "web_search"]
    cleared_marker: str = "[Old tool result content cleared]"


class ToolResultBudgetConfig(_StrictModel):
    tokens_threshold: int = 9000        # 所有 tool 结果总量超此才触发
    large_message_threshold: int = 3000  # 单条 token 超此才 eligible
    trim_size: int = 3000               # 预览留多少字符
    protect_latest: int = 1             # 最新 N 条 tool result 永不 offload


class ContextCompressionConfig(_StrictModel):
    token_threshold: int = 60000
    keep_recent_pairs: int = 6
    summary_prompt: str = (
        "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、"
        "已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
    )
    summary_prompt_mode: Literal["structured", "free"] = "structured"
    micro_compact: MicroCompactConfig = MicroCompactConfig()
    tool_result_budget: ToolResultBudgetConfig = ToolResultBudgetConfig()
```

> `Literal` 已在 schema.py 顶部导入（`from typing import Literal`）。子 model 在 `ContextCompressionConfig` 前定义（引用顺序）。

- [ ] **Step 4: 实现 config.yaml**

把 `twinkle/resources/config.yaml` 的 `context_compression` 段（行 25-28）：
```yaml
context_compression:
  token_threshold: 60000                  # 估算 token(char//3,不精确)超此即压缩历史
  keep_recent_pairs: 6                    # 保留最近 N 个 user/assistant 对
  summary_prompt: "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
```
替换为：
```yaml
context_compression:
  token_threshold: 60000                  # 估算 token(char//3,不精确)超此即压缩历史
  keep_recent_pairs: 6                    # 保留最近 N 个 user/assistant 对
  summary_prompt: "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
  summary_prompt_mode: structured         # structured(硬编码4节)/ free(用上面的 summary_prompt)
  micro_compact:                          # 块1: 同工具旧结果堆叠→marker(无 LLM)
    trigger_threshold: 5                  # 可清条数(总数-keep) > trigger 才清
    keep_recent_per_tool: 3               # 每工具留最近 N 条原文
    compactable_tool_names: [read_file, grep, glob, command_exec, web_fetch, web_search]
    cleared_marker: "[Old tool result content cleared]"
  tool_result_budget:                     # 块1: 大 tool 结果→预览(无 LLM)
    tokens_threshold: 9000                # 所有 tool 结果总量超此才触发
    large_message_threshold: 3000         # 单条 token 超此才 eligible
    trim_size: 3000                       # 预览留多少字符
    protect_latest: 1                     # 最新 N 条 tool result 永不 offload
```

- [ ] **Step 5: 实现 __init__.py 常量导出**

在 `twinkle/config/__init__.py` 的 `CONTEXT_SUMMARY_PROMPT = settings.context_compression.summary_prompt` 那行**之后**追加：

```python
SUMMARY_PROMPT_MODE = settings.context_compression.summary_prompt_mode
MICRO_COMPACT_TRIGGER_THRESHOLD = settings.context_compression.micro_compact.trigger_threshold
MICRO_COMPACT_KEEP_RECENT_PER_TOOL = settings.context_compression.micro_compact.keep_recent_per_tool
MICRO_COMPACT_COMPACTABLE_TOOL_NAMES = settings.context_compression.micro_compact.compactable_tool_names
MICRO_COMPACT_CLEARED_MARKER = settings.context_compression.micro_compact.cleared_marker
TOOL_RESULT_BUDGET_TOKENS_THRESHOLD = settings.context_compression.tool_result_budget.tokens_threshold
TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD = settings.context_compression.tool_result_budget.large_message_threshold
TOOL_RESULT_BUDGET_TRIM_SIZE = settings.context_compression.tool_result_budget.trim_size
TOOL_RESULT_BUDGET_PROTECT_LATEST = settings.context_compression.tool_result_budget.protect_latest
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_compression_config.py -v`
Expected: PASS — 2 tests passed

- [ ] **Step 7: 提交**

```bash
git add twinkle/config/schema.py twinkle/resources/config.yaml twinkle/config/__init__.py tests/test_compression_config.py
git commit -m "feat(compression): config for micro_compact/tool_result_budget/summary_prompt_mode"
```

---

## Task 2: ToolResultBudget 层（块1a）

**Files:**
- Modify: `twinkle/agentserver/compression/__init__.py`（加 config 导入 + `_tool_result_budget`）
- Create: `tests/test_precompress.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_precompress.py`:

```python
from twinkle.agentserver.compression import _tool_result_budget


def _tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_call(call_id, name="read_file"):
    return {"role": "assistant", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": "{}"}}]}


def test_tool_result_budget_noop_under_threshold():
    msgs = [{"role": "system", "content": "s"},
            _assistant_call("c1"), _tool_msg("c1", "x" * 100)]
    out = _tool_result_budget(msgs)
    assert out == msgs
    assert out is not msgs


def test_tool_result_budget_trims_largest_when_over_threshold():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(3):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, "大结果" * 5000))  # ~20000 字符=6666 token
    out = _tool_result_budget(msgs)
    trimmed = [m for m in out
               if m.get("role") == "tool" and "[...trimmed" in m.get("content", "")]
    assert len(trimmed) == 1
    assert len(trimmed[0]["content"]) < 3200


def test_tool_result_budget_protects_latest_tool_result():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(3):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, "独占内容_" + str(i) + "_" + "X" * 20000))
    out = _tool_result_budget(msgs)
    # 最新一条(c2,含"独占内容_2")必须豁免——未被 trim
    latest = [m for m in out
              if m.get("role") == "tool" and "独占内容_2" in m.get("content", "")]
    assert len(latest) == 1
    assert "[...trimmed" not in latest[0]["content"]
    # 至少一条更旧的被 trim
    assert any("[...trimmed" in m.get("content", "")
               for m in out if m.get("role") == "tool")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_precompress.py -v`
Expected: FAIL — `ImportError: cannot import name '_tool_result_budget'`

- [ ] **Step 3: 实现 _tool_result_budget**

在 `twinkle/agentserver/compression/__init__.py` 顶部 `from twinkle.agentserver.llm_client import ...` 之后追加 config 导入：

```python
from twinkle.config import (
    SUMMARY_PROMPT_MODE,
    MICRO_COMPACT_TRIGGER_THRESHOLD,
    MICRO_COMPACT_KEEP_RECENT_PER_TOOL,
    MICRO_COMPACT_COMPACTABLE_TOOL_NAMES,
    MICRO_COMPACT_CLEARED_MARKER,
    TOOL_RESULT_BUDGET_TOKENS_THRESHOLD,
    TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD,
    TOOL_RESULT_BUDGET_TRIM_SIZE,
    TOOL_RESULT_BUDGET_PROTECT_LATEST,
)
```

在 `estimate_tokens` 函数之后插入 `_tool_result_budget`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_precompress.py -v`
Expected: PASS — 3 tests passed

- [ ] **Step 5: 提交**

```bash
git add twinkle/agentserver/compression/__init__.py tests/test_precompress.py
git commit -m "feat(compression): ToolResultBudget layer (large tool result → preview)"
```

---

## Task 3: MicroCompact 层（块1b）

**Files:**
- Modify: `twinkle/agentserver/compression/__init__.py`（加 `_micro_compact`）
- Modify: `tests/test_precompress.py`（追加测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_precompress.py` 末尾追加：

```python
from twinkle.agentserver.compression import _micro_compact


def test_micro_compact_clears_old_same_tool_results():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    contents = [m["content"] for m in tools]
    markers = [c for c in contents if c == "[Old tool result content cleared]"]
    # 10 条:可清 7 > 5(trigger) → 清 7,留最近 3
    assert len(markers) == 7
    assert contents[-3:] == ["result_7", "result_8", "result_9"]


def test_micro_compact_not_triggered_at_boundary_8():
    # 8 条:可清 8-3=5,5 不 >5 → 不清
    msgs = [{"role": "system", "content": "s"}]
    for i in range(8):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    assert all(m["content"].startswith("result_") for m in tools)
    assert not any(m["content"] == "[Old tool result content cleared]" for m in tools)


def test_micro_compact_clears_at_boundary_9():
    # 9 条:可清 6 > 5 → 清 6,留 3
    msgs = [{"role": "system", "content": "s"}]
    for i in range(9):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    markers = [m for m in out
               if m.get("role") == "tool" and m["content"] == "[Old tool result content cleared]"]
    assert len(markers) == 6


def test_micro_compact_skips_non_compactable_tools():
    # list_skill 不在 compactable 名单 → 即使积 10 条也不清
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid, name="list_skill"))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    assert all(m["content"].startswith("result_") for m in tools)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_precompress.py -v`
Expected: FAIL — `ImportError: cannot import name '_micro_compact'`

- [ ] **Step 3: 实现 _micro_compact**

在 `twinkle/agentserver/compression/__init__.py` 的 `_tool_result_budget` 之后插入：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_precompress.py -v`
Expected: PASS — 7 tests passed（3 from Task 2 + 4 here）

- [ ] **Step 5: 提交**

```bash
git add twinkle/agentserver/compression/__init__.py tests/test_precompress.py
git commit -m "feat(compression): MicroCompact layer (same-tool old results → marker)"
```

---

## Task 4: precompress_messages 整合 + compress_messages 内部前置（块2）

**Files:**
- Modify: `twinkle/agentserver/compression/__init__.py`（加 `precompress_messages` + 改 `compress_messages`）
- Modify: `tests/test_precompress.py`（追加整合测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_precompress.py` 末尾追加：

```python
import asyncio
from twinkle.agentserver.compression import precompress_messages, compress_messages
from twinkle.agentserver.llm_client import TextDelta, Finish


class _RecordingLLM:
    """Counts stream() calls — 0 means LLM summary was skipped."""
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        yield TextDelta("摘要")
        yield Finish(finish_reason="stop",
                     assistant_message={"role": "assistant", "content": "摘要", "tool_calls": None})


def test_precompress_runs_budget_then_micro_compact():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append(_tool_msg(cid, "X" * 30000 if i == 0 else f"r{i}"))
    out = precompress_messages(msgs)
    # 最早的大 result(c0)被 ToolResultBudget trim
    assert any("[...trimmed" in m.get("content", "")
               for m in out if m.get("role") == "tool")
    # 其余旧 read_file 结果被 MicroCompact 清成 marker(留最近 3)
    assert any(m["content"] == "[Old tool result content cleared]"
               for m in out if m.get("role") == "tool")


def test_compress_messages_precompress_saves_llm_when_below_threshold():
    # 24 条 read_file,每条 ~2666 token,总量 64000 > 60000(压前会触发压缩)
    # precompress 清 21 条成 marker后 token 骤降 → should_compress=false → 不调 LLM
    msgs = [{"role": "system", "content": "s"}]
    for i in range(24):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append(_tool_msg(cid, "块" * 8000))  # 8000 字符=2666 token
    llm = _RecordingLLM()
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=60000, keep_recent_pairs=3,
        summary_system_prompt="p"))
    assert llm.calls == 0  # precompress 后降到阈值下 → 省 LLM 摘要
    tools = [m for m in out if m.get("role") == "tool"]
    assert any("块" in m["content"] for m in tools[-3:])  # 最近 3 原文保留
    assert any(m["content"] == "[Old tool result content cleared]" for m in tools)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_precompress.py::test_precompress_runs_budget_then_micro_compact tests/test_precompress.py::test_compress_messages_precompress_saves_llm_when_below_threshold -v`
Expected: FAIL — `ImportError: cannot import name 'precompress_messages'`

- [ ] **Step 3: 实现 precompress_messages + 改 compress_messages**

在 `twinkle/agentserver/compression/__init__.py` 的 `_micro_compact` 之后插入 `precompress_messages`：

```python
def precompress_messages(msgs: list[dict]) -> list[dict]:
    """块2: 无 LLM 两层前置压缩。链B顺序 ToolResultBudget → MicroCompact。
    只改"发 LLM 这份",history.json 原文不动。不改输入,返回新 list。"""
    msgs = _tool_result_budget(msgs)
    msgs = _micro_compact(msgs)
    return msgs
```

把现有 `compress_messages`：
```python
async def compress_messages(
    msgs: list[dict],
    llm: "LLMClient",
    *,
    token_threshold: int,
    keep_recent_pairs: int,
    summary_system_prompt: str,
) -> list[dict]:
    """薄壳:判定 → 委派。公开 API,行为与重构前完全一致。

    No-op(copy) 当 should_compress 为 False。do_compress 经模块 globals 解析,
    被 instrumentor patch 后,本函数的调用会到达 wrapper(同模块解析,非 import 绑定)。
    """
    if not should_compress(msgs, token_threshold=token_threshold,
                           keep_recent_pairs=keep_recent_pairs):
        return list(msgs)
    return await do_compress(msgs, llm, keep_recent_pairs=keep_recent_pairs,
                             summary_system_prompt=summary_system_prompt)
```
替换为：
```python
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
```

> `precompress_messages` 定义在 `compress_messages` 之前（模块内顺序），`compress_messages` 经模块 globals 解析它。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_precompress.py -v`
Expected: PASS — 9 tests passed

- [ ] **Step 5: 跑现有压缩测试确认未破坏**

Run: `python -m pytest tests/test_context_compression.py -v`
Expected: PASS — 现有测试全过（这些用例无 tool 消息或 tool 结果小，precompress 不动它们）

- [ ] **Step 6: 提交**

```bash
git add twinkle/agentserver/compression/__init__.py tests/test_precompress.py
git commit -m "feat(compression): precompress_messages前置 + compress_messages内部调用(省LLM)"
```

---

## Task 5: 结构化摘要 prompt + mode 开关（块3）

**Files:**
- Modify: `twinkle/agentserver/compression/__init__.py`（加 `_STRUCTURED_SUMMARY_PROMPT` + 改 `_summarize`）
- Modify: `tests/test_context_compression.py`（更新 `_summarize` 测试 + 加 mode 测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_context_compression.py` 顶部 import 区追加：
```python
from twinkle.agentserver.compression import _STRUCTURED_SUMMARY_PROMPT
```

把现有 `test_summarize_collects_textdeltas`：
```python
def test_summarize_collects_textdeltas():
    llm = FakeLLM(summary_text="the summary")
    out = asyncio.run(_summarize(llm, "sysprompt", "middle text"))
    assert out == "the summary"
    msgs, tools = llm.calls[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "sysprompt"
    assert msgs[1]["role"] == "user" and "middle text" in msgs[1]["content"]
    assert tools == []
```
替换为（加 `monkeypatch` 把 mode 切到 free，验传入的 prompt 被用）：
```python
def test_summarize_collects_textdeltas(monkeypatch):
    monkeypatch.setattr("twinkle.agentserver.compression.SUMMARY_PROMPT_MODE", "free")
    llm = FakeLLM(summary_text="the summary")
    out = asyncio.run(_summarize(llm, "sysprompt", "middle text"))
    assert out == "the summary"
    msgs, tools = llm.calls[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "sysprompt"
    assert msgs[1]["role"] == "user" and "middle text" in msgs[1]["content"]
    assert tools == []


def test_summarize_uses_structured_prompt_in_structured_mode(monkeypatch):
    monkeypatch.setattr("twinkle.agentserver.compression.SUMMARY_PROMPT_MODE", "structured")
    llm = FakeLLM(summary_text="结构化摘要")
    asyncio.run(_summarize(llm, "free form prompt", "middle"))
    msgs, _ = llm.calls[0]
    # structured 模式用硬编码常量,非传入的 free form prompt
    assert msgs[0]["content"] == _STRUCTURED_SUMMARY_PROMPT


def test_structured_summary_prompt_has_four_sections():
    assert "关键事实与决定" in _STRUCTURED_SUMMARY_PROMPT
    assert "已用工具与文件" in _STRUCTURED_SUMMARY_PROMPT
    assert "待办与当前任务" in _STRUCTURED_SUMMARY_PROMPT
    assert "错误与修复" in _STRUCTURED_SUMMARY_PROMPT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ImportError: cannot import name '_STRUCTURED_SUMMARY_PROMPT'`

- [ ] **Step 3: 实现 _STRUCTURED_SUMMARY_PROMPT + 改 _summarize**

在 `twinkle/agentserver/compression/__init__.py` 的 `_render_messages_text` 之后、`_summarize` 之前插入常量：

```python
_STRUCTURED_SUMMARY_PROMPT = (
    "你是对话上下文压缩器。把下面即将被压缩丢弃的对话中段压成结构化摘要，"
    "供接续的 AI 继续工作。按以下固定 4 节输出，每节有内容才写，无内容写\"(无)\"：\n\n"
    "## 关键事实与决定\n- [定下的关键事实、用户偏好、已做决策]\n\n"
    "## 已用工具与文件\n- [已调用过的工具及涉及/确认过的文件路径、函数名]\n\n"
    "## 待办与当前任务\n- [尚未完成的任务、当前正在做的事、用户的原始目标]\n\n"
    "## 错误与修复\n- [遇到过的错误及如何修复的，便于避免重复踩坑]\n\n"
    "保留确切的文件路径、函数名、错误信息原文。不要寒暄、不要复述过程。用中文。"
)
```

把现有 `_summarize`：
```python
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
替换为（mode 分支：structured 用常量，free 用传入的）：
```python
async def _summarize(llm: LLMClient, summary_system_prompt: str, middle_text: str) -> str:
    """Call llm.stream (tools=[]) and concatenate all TextDelta fragments.
    summary_prompt_mode: structured → 用硬编码 4 节常量；free → 用传入的 summary_system_prompt。"""
    sys_prompt = (_STRUCTURED_SUMMARY_PROMPT if SUMMARY_PROMPT_MODE == "structured"
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_compression.py -v`
Expected: PASS — 全部（含新增 3 个 mode/section 测试 + 更新的 `test_summarize_collects_textdeltas` + 现有 e2e/degrade 测试）

- [ ] **Step 5: 提交**

```bash
git add twinkle/agentserver/compression/__init__.py tests/test_context_compression.py
git commit -m "feat(compression): structured 4-section summary prompt + mode switch"
```

---

## Task 6: 全链回归（不破坏 memory_flush / observability / e2e）

**Files:**
- 无代码改动（验证零冲突）；仅当出现非预期失败才修

- [ ] **Step 1: 跑压缩相关全套**

Run:
```bash
python -m pytest tests/test_compression_config.py tests/test_precompress.py tests/test_context_compression.py tests/test_memory_flush_hook.py tests/test_memory_flush_wiring.py tests/test_context_compression_hook.py tests/test_agent_loop_compress.py tests/test_observability_compression_evolution.py -v
```
Expected: all PASS

> 依据：`memory_flush_hook` 依赖 `should_compress`/`split_messages_head_middle_tail`/`_render_messages_text`，签名未动 → 仍过。`instrumentors/compression.py` patch `do_compress`，签名未动 → 仍过。e2e 用 user/assistant 无 tool → precompress 不动 → 仍过。

- [ ] **Step 2: 若有失败则定位修复**

常见可能（按 spec §6 都已规避，列出便于排查）：
- `memory_flush_hook` 报 `AttributeError`：检查 `should_compress`/`split`/`_render` 签名是否被误改 → 应未改。
- observability span 缺失：`do_compress` 签名是否被改 → 应未改。
- e2e token 断言变：precompress 是否动了无 tool 的 msgs → 应不动（`_tool_result_budget`/`_micro_compact` 无 tool 时直接 copy）。

若需修，修后重跑 Step 1。

- [ ] **Step 3: 提交（仅当有修复改动）**

```bash
git add -A
git commit -m "fix(compression): regression fixes from precompress integration"
```
（无改动则跳过此步。）

---

## Self-Review Notes

**Spec 覆盖**：块1 两层前置（Task 2 ToolResultBudget + Task 3 MicroCompact + `protect_latest` 在 Task 2）→ §3.1/3.2/3.3 ✓；块2 GET 不照搬 ADD + `compress_messages` 内部前置（Task 4）→ §4 ✓；块3 结构化 4 节 + mode 开关（Task 5）→ §5 ✓；config（Task 1）→ §3/§5 阈值 ✓；不破坏（Task 6）→ §6 取舍 ✓。§6 刻意取舍落地：无损 history（precompress 不写回，只改发 LLM 这份）、字符 //3 估算（保留 `estimate_tokens`）、`MemoryFlushHook` 依赖签名不变、`compress_messages` 公开签名不变。

**Placeholder scan**：无 TBD/TODO；每个代码步骤含完整可粘贴代码；测试含真实断言。

**Type consistency**：`_tool_result_budget`/`_micro_compact`/`precompress_messages`/`_STRUCTURED_SUMMARY_PROMPT` 命名前后一致；config 常量 `MICRO_COMPACT_*`/`TOOL_RESULT_BUDGET_*`/`SUMMARY_PROMPT_MODE` 在 schema/`__init__`/compression 导入三处一致；`_summarize` 签名不变（仍收 `summary_system_prompt`，内部按 mode 选）。
