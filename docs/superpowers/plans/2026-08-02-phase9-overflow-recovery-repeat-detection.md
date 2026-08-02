# Phase 9 — Overflow Recovery + Repeat Tool Call Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add context overflow recovery (413 auto-retry) and repeat tool call detection (loop auto-remediation) to the agent loop.

**Architecture:** Two independent AgentHook subclasses plug into the existing hook system. `ContextOverflowRecoveryHook` (priority 60) handles `on_model_exception` for 413/context_length_exceeded errors, force-compresses with aggressive parameters, and requests retry. `RepeatToolCallDetectorHook` (priority 88) tracks tool calls in a sliding window, classifies into 4 severity tiers, and injects remediation system messages on loops. A small change to `decorator.py` passes tool results to `after_tool_call` hooks. New config sections in schema.py and config.yaml.

**Tech Stack:** Python, asyncio, pytest, hashlib, re (stdlib only — no new deps)

## Global Constraints

- No new dependencies — use stdlib only (hashlib, re, time, collections, json, enum).
- Follow existing hook patterns: `AgentHook` subclass, `priority` class attr, lazy config reads via module-level `_get_*` functions.
- HookManager executes callbacks in priority-descending order (higher runs first).
- `compress_messages` is the single compression function — reuse it, don't duplicate.
- `RetryHook` (priority 50) does NOT handle 413 — `ContextOverflowRecoveryHook` (priority 60) runs first and handles it.
- All tests use `asyncio.run()` — no `pytest-asyncio` (per CLAUDE.md).
- Config schema uses `_StrictModel` (rejects unknown keys) with `extra="forbid"`.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py` | 413 detection + forced compression + retry + circuit-break |
| Create | `twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py` | Sliding window + stable hash + 4-tier classification + remediation injection |
| Create | `tests/test_context_overflow_recovery_hook.py` | Tests for overflow recovery hook |
| Create | `tests/test_repeat_tool_call_detector_hook.py` | Tests for repeat tool call detector |
| Modify | `twinkle/agentserver/hooks/builtin/__init__.py` | Export new hooks |
| Modify | `twinkle/agentserver/hooks/decorator.py:64` | Store tool result in `ctx.extra["_tool_result"]` before after-event |
| Modify | `twinkle/agentserver/server.py:79,85` | Auto-wire new hooks in `build_agent_loop` |
| Modify | `twinkle/config/schema.py` | Add `OverflowRecoveryConfig` + `RepeatToolDetectionConfig` |
| Modify | `twinkle/config/__init__.py` | Export new config constants |
| Modify | `twinkle/resources/config.yaml` | Add `overflow_recovery` + `repeat_tool_detection` sections |

---

### Task 1: Config schema + YAML + exports

**Files:**
- Modify: `twinkle/config/schema.py:144-168` (after `SubagentConfig`)
- Modify: `twinkle/config/__init__.py:83` (after subagent constants)
- Modify: `twinkle/resources/config.yaml:76-79` (after subagent section)
- Test: `tests/test_config.py` (existing — verify no regression)

**Interfaces:**
- Consumes: `_StrictModel` base class, `TwinkleConfig` model
- Produces: `settings.overflow_recovery.max_recovery_attempts`, `settings.overflow_recovery.threshold_ratio`, `settings.overflow_recovery.aggressive_keep_recent`, `settings.overflow_recovery.context_window_limit_tokens`, `settings.repeat_tool_detection.history_size`, `settings.repeat_tool_detection.repeat_warn`, `settings.repeat_tool_detection.pingpong_warn`, `settings.repeat_tool_detection.loop_block`, `settings.repeat_tool_detection.global_stop`, `settings.repeat_tool_detection.remediation_max_per_minute`

- [ ] **Step 1: Add config schema classes**

In `twinkle/config/schema.py`, after `SubagentConfig` (line 153) and before `TwinkleConfig` (line 155), add:

```python
class OverflowRecoveryConfig(_StrictModel):
    max_recovery_attempts: int = 3          # consecutive overflow recovery max attempts
    threshold_ratio: float = 0.85           # target ratio of model window after recovery
    aggressive_keep_recent: int = 3         # keep_recent_pairs reduced to this on overflow
    context_window_limit_tokens: int = 0    # 0 = parse from 413 error; >0 = manual override


class RepeatToolDetectionConfig(_StrictModel):
    history_size: int = 30                  # sliding window size
    repeat_warn: int = 10                   # LOW threshold
    pingpong_warn: int = 10                 # MEDIUM threshold
    loop_block: int = 20                    # HIGH threshold
    global_stop: int = 30                   # CRITICAL threshold
    remediation_max_per_minute: int = 5     # remediation injection rate limit
```

In `TwinkleConfig`, add two new fields after `subagent`:

```python
    overflow_recovery: OverflowRecoveryConfig = OverflowRecoveryConfig()
    repeat_tool_detection: RepeatToolDetectionConfig = RepeatToolDetectionConfig()
```

- [ ] **Step 2: Add YAML sections**

In `twinkle/resources/config.yaml`, after the `subagent:` section (line 79), add:

```yaml
overflow_recovery:
  max_recovery_attempts: 3
  threshold_ratio: 0.85
  aggressive_keep_recent: 3
  context_window_limit_tokens: 0    # 0 = parse from 413 error; >0 = manual override

repeat_tool_detection:
  history_size: 30
  repeat_warn: 10
  pingpong_warn: 10
  loop_block: 20
  global_stop: 30
  remediation_max_per_minute: 5
```

- [ ] **Step 3: Add config exports**

In `twinkle/config/__init__.py`, after the subagent constants (line 83), add:

```python
# --- overflow recovery (Phase 9) ---
OVERFLOW_MAX_RECOVERY_ATTEMPTS = settings.overflow_recovery.max_recovery_attempts
OVERFLOW_THRESHOLD_RATIO = settings.overflow_recovery.threshold_ratio
OVERFLOW_AGGRESSIVE_KEEP_RECENT = settings.overflow_recovery.aggressive_keep_recent
OVERFLOW_CONTEXT_WINDOW_LIMIT = settings.overflow_recovery.context_window_limit_tokens

# --- repeat tool detection (Phase 9) ---
REPEAT_TOOL_HISTORY_SIZE = settings.repeat_tool_detection.history_size
REPEAT_TOOL_REPEAT_WARN = settings.repeat_tool_detection.repeat_warn
REPEAT_TOOL_PINGPONG_WARN = settings.repeat_tool_detection.pingpong_warn
REPEAT_TOOL_LOOP_BLOCK = settings.repeat_tool_detection.loop_block
REPEAT_TOOL_GLOBAL_STOP = settings.repeat_tool_detection.global_stop
REPEAT_TOOL_REMEDIATION_MAX_PER_MINUTE = settings.repeat_tool_detection.remediation_max_per_minute
```

- [ ] **Step 4: Run existing config tests to verify no regression**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml
git commit -m "feat(config): add overflow_recovery + repeat_tool_detection config sections"
```

---

### Task 2: decorator.py — store tool result in ctx.extra

**Files:**
- Modify: `twinkle/agentserver/hooks/decorator.py:63-65`
- Test: `tests/test_hook_decorator.py` (existing — verify no regression)

**Interfaces:**
- Consumes: `ctx.extra` dict on `HookContext`
- Produces: `ctx.extra["_tool_result"]` set before `after` event fires — consumed by `RepeatToolCallDetectorHook.after_tool_call`

- [ ] **Step 1: Write the failing test**

In `tests/test_hook_decorator.py`, add a test that verifies `_tool_result` is available in the after-event hook:

```python
def test_after_event_receives_tool_result():
    """decorator stores method return value in ctx.extra['_tool_result'] before after-event."""
    from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookEvent, ToolCallInputs

    results = {}

    class SpyHook(AgentHook):
        priority = 50
        async def after_tool_call(self, ctx: HookContext) -> None:
            results["tool_result"] = ctx.extra.get("_tool_result")

    class FakeLoop:
        def __init__(self):
            self._hook_manager = HookManager()
            self._hook_manager.register_hook(SpyHook())

    loop = FakeLoop()
    ctx = HookContext(
        agent=loop, event=HookEvent.BEFORE_TOOL_CALL,
        inputs=ToolCallInputs(name="test", args={}, tool_call_id="tc1"),
        session_id=None, request_id=None, extra={},
    )

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL)
    async def tool_method(self, ctx):
        return "tool-output-42"

    import asyncio
    asyncio.run(tool_method(loop, ctx))
    assert results["tool_result"] == "tool-output-42"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_decorator.py::test_after_event_receives_tool_result -v`
Expected: FAIL — `results["tool_result"]` is None (decorator doesn't store it yet)

- [ ] **Step 3: Implement the change**

In `twinkle/agentserver/hooks/decorator.py`, line 63-65, change:

```python
                    result = await method(self, ctx, *args, **kwargs)
                    # 4. Trigger after event on success
                    await hook_manager.execute(after, ctx)
                    return result
```

to:

```python
                    result = await method(self, ctx, *args, **kwargs)
                    # Store result for after-event hooks (e.g., RepeatToolCallDetectorHook)
                    ctx.extra["_tool_result"] = result
                    # 4. Trigger after event on success
                    await hook_manager.execute(after, ctx)
                    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook_decorator.py::test_after_event_receives_tool_result -v`
Expected: PASS

- [ ] **Step 5: Run existing decorator tests for regression**

Run: `python -m pytest tests/test_hook_decorator.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/decorator.py tests/test_hook_decorator.py
git commit -m "feat(hooks): store tool result in ctx.extra before after-event"
```

---

### Task 3: ContextOverflowRecoveryHook — implementation + tests

**Files:**
- Create: `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`
- Create: `tests/test_context_overflow_recovery_hook.py`

**Interfaces:**
- Consumes: `AgentHook`, `HookContext`, `HookEvent` from `hooks.base`; `compress_messages`, `estimate_tokens` from `compression`; `LLMClient` from `llm_client`; config constants from `twinkle.config`
- Produces: `ContextOverflowRecoveryHook` class (priority 60), `_is_context_overflow_error` function, `_parse_token_limits` function

- [ ] **Step 1: Write the failing tests**

Create `tests/test_context_overflow_recovery_hook.py`:

```python
"""Tests for ContextOverflowRecoveryHook — 413 detection, token parsing,
forced compression + retry, circuit-break, count reset."""

import asyncio
from unittest.mock import AsyncMock

from twinkle.agentserver.compression import estimate_tokens
from twinkle.agentserver.hooks.base import HookContext, ModelCallInputs
from twinkle.agentserver.hooks.builtin.context_overflow_recovery_hook import (
    ContextOverflowRecoveryHook,
    _is_context_overflow_error,
    _parse_token_limits,
)


class _FakeLLM:
    """Fake LLM for compress_messages — yields fixed summary."""
    async def stream(self, messages, tools):
        from twinkle.agentserver.llm_client import TextDelta
        yield TextDelta("摘要")


class _Ctx:
    """Minimal ctx stub: hook touches ctx.inputs.messages, ctx.exception, ctx.extra."""
    def __init__(self, messages, exception=None):
        self.inputs = ModelCallInputs(messages=messages, tools=[])
        self.exception = exception
        self.extra = {}
        self._retry_request = None
        self._force_finish_request = None

    def request_retry(self, delay=0):
        self._retry_request = delay

    def request_force_finish(self, result=None):
        self._force_finish_request = result


# --- _is_context_overflow_error tests ---

class _Exc413(Exception):
    status_code = 413

class _Exc400(Exception):
    status_code = 400

class _ExcNoStatus(Exception):
    pass


def test_detects_413_status_code():
    assert _is_context_overflow_error(_Exc413("some error")) is True


def test_detects_400_with_context_length_exceeded():
    assert _is_context_overflow_error(_Exc400("context_length_exceeded")) is True


def test_detects_400_with_prompt_too_long():
    assert _is_context_overflow_error(_Exc400("prompt is too long: 10000 tokens > 8000")) is True


def test_detects_400_with_input_too_long():
    assert _is_context_overflow_error(_Exc400("input too long for model")) is True


def test_detects_400_without_overflow_keyword():
    assert _is_context_overflow_error(_Exc400("invalid parameter")) is False


def test_detects_no_status_with_overflow_keyword():
    assert _is_context_overflow_error(_ExcNoStatus("context_length_exceeded")) is True


def test_detects_no_status_without_keyword():
    assert _is_context_overflow_error(_ExcNoStatus("rate limit exceeded")) is False


def test_detects_anthropic_prompt_too_long():
    assert _is_context_overflow_error(_ExcNoStatus("prompt is too long: 10000 tokens > 8000")) is True


def test_ignores_rate_limit_error():
    import openai
    try:
        raise openai.RateLimitError("rate limited", response=None, body=None)
    except openai.RateLimitError as exc:
        assert _is_context_overflow_error(exc) is False


# --- _parse_token_limits tests ---

def test_parse_anthropic_format():
    actual, limit = _parse_token_limits(Exception("prompt is too long: 10000 tokens > 8000"))
    assert actual == 10000
    assert limit == 8000


def test_parse_openai_format():
    actual, limit = _parse_token_limits(Exception("This model's maximum context length is 128000 tokens"))
    assert actual is None
    assert limit == 128000


def test_parse_no_match():
    actual, limit = _parse_token_limits(Exception("some unknown error"))
    assert actual is None
    assert limit is None


# --- Hook behavior tests ---

def _big_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    return msgs


def test_compresses_and_requests_retry_on_413():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    big = _big_messages()
    ctx = _Ctx(big, exception=_Exc413("overflow"))

    asyncio.run(hook.on_model_exception(ctx))

    # Messages should be compressed (fewer tokens)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    # Retry was requested
    assert ctx._retry_request is not None


def test_no_retry_on_non_overflow_error():
    hook = ContextOverflowRecoveryHook(llm=_FakeLLM())
    ctx = _Ctx([{"role": "system", "content": "s"}], exception=_ExcNoStatus("rate limit"))

    asyncio.run(hook.on_model_exception(ctx))

    # No retry requested
    assert ctx._retry_request is None


def test_circuit_break_after_max_attempts():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=2,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    # Simulate 2 consecutive overflow errors
    ctx1 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx1))
    ctx2 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx2))
    # 3rd should trigger circuit break
    ctx3 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx3))

    # Circuit break: no retry, injects [CONTEXT_OVERFLOW] message
    assert ctx3._retry_request is None
    assert any("[CONTEXT_OVERFLOW]" in m.get("content", "") for m in ctx3.inputs.messages)


def test_resets_count_on_success():
    hook = ContextOverflowRecoveryHook(llm=_FakeLLM(), max_recovery_attempts=3)
    # Simulate 1 overflow
    ctx1 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx1))
    assert hook._consecutive_overflow_count == 1

    # Successful model call resets count
    ctx2 = _Ctx([{"role": "system", "content": "s"}], exception=None)
    asyncio.run(hook.after_model_call(ctx2))
    assert hook._consecutive_overflow_count == 0


def test_uses_parsed_limit_for_threshold():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    big = _big_messages()
    # OpenAI format: "maximum context length is 128000"
    ctx = _Ctx(big, exception=_ExcNoStatus("maximum context length is 128000"))

    asyncio.run(hook.on_model_exception(ctx))

    # Compression should have been applied (result is shorter)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert ctx._retry_request is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_context_overflow_recovery_hook.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ContextOverflowRecoveryHook**

Create `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`:

```python
"""ContextOverflowRecoveryHook — on_model_exception 溢出恢复。

被动恢复：LLM 抛 413 / context_length_exceeded 时，强制更激进压缩后重试。
连续失败熔断：超过 max_recovery_attempts 后注入熔断消息让 LLM 产出最终回答。
成功后重置计数器。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from twinkle.agentserver.compression import compress_messages, estimate_tokens
from twinkle.agentserver.hooks.base import AgentHook, HookContext

if TYPE_CHECKING:
    from twinkle.agentserver.llm_client import LLMClient

log = logging.getLogger("twinkle.hooks.overflow_recovery")


def _parse_token_limits(exc: Exception) -> tuple[int | None, int | None]:
    """从 413 错误解析 actual_tokens 和 limit_tokens。

    Returns: (actual_tokens, limit_tokens) — None 表示未解析到。
    """
    msg = str(exc)

    # Anthropic: "prompt is too long: N tokens > M"
    m = re.search(r'(\d+)\s*tokens?\s*>\s*(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # OpenAI: "maximum context length is N"
    m = re.search(r'maximum context length is\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return None, int(m.group(1))

    return None, None


_OVERFLOW_KEYWORDS = (
    "prompt is too long",       # Anthropic
    "input too long",           # Anthropic 新格式
    "context_length_exceeded",  # OpenAI 标准 error code
    "maximum context length",   # OpenAI
    "context length exceeded",  # 通用
)


def _is_context_overflow_error(exc: Exception) -> bool:
    """判断异常是否为上下文溢出错误。

    3 层判定：
    1. status_code==413 → 直接判定
    2. status_code==400 + 溢出关键词 → 判定
    3. status_code is None + 溢出关键词 → 兜底判定
    """
    status_code = getattr(exc, "status_code", None)
    msg_lower = str(exc).lower()
    has_keyword = any(kw in msg_lower for kw in _OVERFLOW_KEYWORDS)

    if status_code == 413:
        return True
    if status_code == 400 and has_keyword:
        return True
    if status_code is None and has_keyword:
        return True
    return False


class ContextOverflowRecoveryHook(AgentHook):
    """Context overflow recovery — reactive 413 handling.

    Priority 60: 在 RetryHook(50) 之前，先处理溢出恢复。
    RetryHook 不处理 413（不属于 transient），所以两者不冲突。
    """

    priority = 60

    def __init__(
        self,
        llm: "LLMClient",
        *,
        max_recovery_attempts: int | None = None,
        aggressive_keep_recent: int | None = None,
        threshold_ratio: float | None = None,
    ) -> None:
        self._llm = llm
        self._max_recovery_attempts = max_recovery_attempts
        self._aggressive_keep_recent = aggressive_keep_recent
        self._threshold_ratio = threshold_ratio
        self._consecutive_overflow_count: int = 0

    async def on_model_exception(self, ctx: HookContext) -> None:
        exc = ctx.exception
        if exc is None or not _is_context_overflow_error(exc):
            return

        self._consecutive_overflow_count += 1
        actual_tokens, limit_tokens = _parse_token_limits(exc)
        max_attempts = self._max_recovery_attempts or _get_max_recovery_attempts()

        log.warning(
            "[ContextOverflowRecovery] Context overflow detected "
            "(attempt %d/%d) actual_tokens=%s limit_tokens=%s",
            self._consecutive_overflow_count, max_attempts,
            actual_tokens, limit_tokens,
        )

        if self._consecutive_overflow_count > max_attempts:
            await self._circuit_break(ctx)
            return

        # 计算激进压缩参数
        aggressive_keep = self._aggressive_keep_recent or _get_aggressive_keep_recent()
        ratio = self._threshold_ratio or _get_threshold_ratio()

        # 从 413 解析出 limit_tokens 时，动态算 threshold_override
        threshold_override = None
        if limit_tokens is not None:
            threshold_override = int(limit_tokens * ratio)
        else:
            config_limit = _get_config_context_limit()
            if config_limit > 0:
                threshold_override = int(config_limit * ratio)

        # 激进压缩：更小的 keep_recent_pairs + 更低的 threshold
        try:
            compressed = await compress_messages(
                ctx.inputs.messages, self._llm,
                token_threshold=threshold_override or _get_fallback_threshold(),
                keep_recent_pairs=aggressive_keep,
                summary_system_prompt=_get_summary_prompt(),
            )
            ctx.inputs.messages = compressed
            log.info(
                "[ContextOverflowRecovery] Aggressive compression applied, requesting retry",
            )
        except Exception:
            log.exception("[ContextOverflowRecovery] Aggressive compression failed; retrying anyway")

        ctx.request_retry(delay=0)

    async def after_model_call(self, ctx: HookContext) -> None:
        if ctx.exception is None and self._consecutive_overflow_count > 0:
            log.info(
                "[ContextOverflowRecovery] LLM call succeeded after %d overflow recovery attempt(s)",
                self._consecutive_overflow_count,
            )
        if ctx.exception is None:
            self._consecutive_overflow_count = 0

    async def _circuit_break(self, ctx: HookContext) -> None:
        """熔断：注入消息让 LLM 产出最终回答，而非抛异常挂死。"""
        log.error(
            "[ContextOverflowRecovery] Circuit breaker triggered after %d "
            "consecutive context overflow errors",
            self._consecutive_overflow_count,
        )
        ctx.inputs.messages = list(ctx.inputs.messages) + [{
            "role": "system",
            "content": (
                "[CONTEXT_OVERFLOW] 上下文持续溢出，自动压缩恢复失败。"
                "请用当前已有信息总结回答用户，建议用户开始新会话。"
            ),
        }]
        self._consecutive_overflow_count = 0


# --- Config lazy reads ---

def _get_max_recovery_attempts() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.max_recovery_attempts


def _get_aggressive_keep_recent() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.aggressive_keep_recent


def _get_threshold_ratio() -> float:
    from twinkle.config import settings
    return settings.overflow_recovery.threshold_ratio


def _get_config_context_limit() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.context_window_limit_tokens


def _get_fallback_threshold() -> int:
    from twinkle.config import CONTEXT_TOKEN_THRESHOLD
    return CONTEXT_TOKEN_THRESHOLD


def _get_summary_prompt() -> str:
    from twinkle.config import CONTEXT_SUMMARY_PROMPT
    return CONTEXT_SUMMARY_PROMPT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_context_overflow_recovery_hook.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite for regression**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py tests/test_context_overflow_recovery_hook.py
git commit -m "feat(hooks): add ContextOverflowRecoveryHook — 413 auto-retry with forced compression"
```

---

### Task 4: RepeatToolCallDetectorHook — implementation + tests

**Files:**
- Create: `twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py`
- Create: `tests/test_repeat_tool_call_detector_hook.py`

**Interfaces:**
- Consumes: `AgentHook`, `HookContext`, `ToolCallInputs` from `hooks.base`; `ctx.extra["_tool_result"]` from decorator (Task 2); config constants from `twinkle.config`
- Produces: `RepeatToolCallDetectorHook` class (priority 88), `stable_call_hash` function, `stable_result_hash` function, `Severity` enum

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repeat_tool_call_detector_hook.py`:

```python
"""Tests for RepeatToolCallDetectorHook — stable hash, 4-tier detection,
remediation injection, rate limiting, edge-triggered behavior."""

import asyncio
import time

from twinkle.agentserver.hooks.base import HookContext, ModelCallInputs, ToolCallInputs
from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import (
    RepeatToolCallDetectorHook,
    Severity,
    stable_call_hash,
    stable_result_hash,
)


# --- stable hash tests ---

def test_stable_call_hash_order_independent():
    """Parameter order should not affect hash."""
    h1 = stable_call_hash("read_file", {"path": "a.txt", "offset": 0})
    h2 = stable_call_hash("read_file", {"offset": 0, "path": "a.txt"})
    assert h1 == h2


def test_stable_call_hash_different_args():
    """Different args should produce different hashes."""
    h1 = stable_call_hash("read_file", {"path": "a.txt"})
    h2 = stable_call_hash("read_file", {"path": "b.txt"})
    assert h1 != h2


def test_stable_result_hash_same_content():
    """Same content should produce same hash."""
    h1 = stable_result_hash("result content")
    h2 = stable_result_hash("result content")
    assert h1 == h2


def test_stable_result_hash_different_content():
    """Different content should produce different hashes."""
    h1 = stable_result_hash("result A")
    h2 = stable_result_hash("result B")
    assert h1 != h2


# --- Helper to simulate tool calls ---

def _make_tool_ctx(name, args, result=""):
    """Create a HookContext with ToolCallInputs."""
    return HookContext(
        agent=None, event=None,
        inputs=ToolCallInputs(name=name, args=args, tool_call_id="tc1"),
        session_id=None, request_id=None,
        extra={"_tool_result": result},
    )


def _make_model_ctx(messages):
    """Create a HookContext with ModelCallInputs."""
    return HookContext(
        agent=None, event=None,
        inputs=ModelCallInputs(messages=messages, tools=[]),
        session_id=None, request_id=None,
        extra={},
    )


async def _simulate_tool_call_sequence(hook, calls):
    """Simulate a sequence of tool calls. Each call is (name, args, result)."""
    for name, args, result in calls:
        # before_tool_call
        ctx = _make_tool_ctx(name, args)
        await hook.before_tool_call(ctx)
        # after_tool_call
        ctx.extra["_tool_result"] = result
        await hook.after_tool_call(ctx)


# --- Detection tests ---

def test_detects_repeat_calls_low():
    """Same tool+args appearing >= repeat_warn times → LOW."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.LOW


def test_detects_pingpong_medium():
    """A-B-A-B alternation >= pingpong_warn → MEDIUM."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # A-B-A-B pattern (4 alternations)
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.MEDIUM


def test_detects_trailing_identical_high():
    """Trailing identical (call+outcome) >= loop_block → HIGH."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.HIGH


def test_detects_critical_loop():
    """Trailing identical >= global_stop → CRITICAL."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=3)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.CRITICAL


def test_no_detection_under_threshold():
    """Below all thresholds → no detection."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 2
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity is None


def test_edge_triggered_only_escalates():
    """Severity only rises, never falls — edge-triggered."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # 3 repeats → LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.LOW

    # 1 more different call — severity should not drop
    calls2 = [("write_file", {"path": "b.txt"}, "ok")]
    asyncio.run(_simulate_tool_call_sequence(hook, calls2))
    # fired_severity stays LOW (not reset, not escalated)
    assert hook._fired_severity == Severity.LOW


# --- Remediation injection tests ---

def test_injects_remediation_message_at_medium():
    """MEDIUM+ severity triggers remediation message injection in before_model_call."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # Trigger MEDIUM
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    # Now call before_model_call
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # Check remediation message was injected
    assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_no_injection_below_medium():
    """LOW severity does not trigger remediation injection."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # Trigger LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # No remediation message
    assert not any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_remediation_rate_limit():
    """Remediation injection is rate-limited to max_per_minute."""
    hook = RepeatToolCallDetectorHook(
        repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30,
        remediation_max_per_minute=2,
    )
    # Trigger MEDIUM
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    # First 2 injections should succeed
    for _ in range(2):
        msgs = [{"role": "system", "content": "s"}]
        ctx = _make_model_ctx(msgs)
        asyncio.run(hook.before_model_call(ctx))
        assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)

    # 3rd should be rate-limited
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))
    # Count how many [DETECTION] messages — should be 0 (rate-limited)
    detection_count = sum(1 for m in ctx.inputs.messages if "[DETECTION]" in m.get("content", ""))
    assert detection_count == 0


def test_different_results_not_counted_as_loop():
    """Same tool+args but different results = progress, not a loop."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    # Same call, different results each time
    calls = [
        ("read_file", {"path": "a.txt"}, "result_1"),
        ("read_file", {"path": "a.txt"}, "result_2"),
        ("read_file", {"path": "a.txt"}, "result_3"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    # trailing_identical should be 0 (different outcomes), so no HIGH
    assert hook._fired_severity is None or hook._fired_severity <= Severity.LOW
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_repeat_tool_call_detector_hook.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement RepeatToolCallDetectorHook**

Create `twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py`:

```python
"""RepeatToolCallDetectorHook — 滑动窗口 + stable hash 检测重复工具调用。

4 级严重度（LOW→CRITICAL），edge-triggered，超过阈值自动注入纠偏 system 消息。
限频防风暴：同一 session 内 N 秒最多 M 次注入。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from enum import IntEnum

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.repeat_tool_detection")


class Severity(IntEnum):
    """4 级严重度，rank 越高越严重。"""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


def stable_call_hash(name: str, args: dict) -> str:
    """Stable hash of tool name + sorted args — 参数顺序不影响检测。"""
    payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def stable_result_hash(result: str) -> str:
    """Stable hash of tool result — 区分'重复调用但结果在变' vs '结果相同'。"""
    return hashlib.sha256(result.encode()).hexdigest()[:16]


class RepeatToolCallDetectorHook(AgentHook):
    """Repeat / loop tool-call detector + auto-remediator.

    Priority 88: 在 SkillHook(90) 之后、MemoryHook(80) 之前。
    需要在压缩后看到消息，但在 memory 注入前注入纠偏消息。

    事件：
    - before_tool_call: 记录 call_key
    - after_tool_call: 记录 outcome_key + 分类检测
    - on_tool_exception: 记录 error 作为 outcome_key + 分类检测
    - before_model_call: 检测到循环时注入纠偏 system 消息
    """

    priority = 88

    def __init__(
        self,
        *,
        history_size: int | None = None,
        repeat_warn: int | None = None,
        pingpong_warn: int | None = None,
        loop_block: int | None = None,
        global_stop: int | None = None,
        remediation_max_per_minute: int | None = None,
    ) -> None:
        self._history_size = history_size
        self._repeat_warn = repeat_warn
        self._pingpong_warn = pingpong_warn
        self._loop_block = loop_block
        self._global_stop = global_stop
        self._remediation_max_per_minute = remediation_max_per_minute

        # 运行时状态
        self._history: deque[tuple[str, str]] = deque(
            maxlen=history_size or _get_history_size()
        )
        self._pending_call_key: str | None = None
        self._fired_severity: Severity | None = None
        self._remediation_timestamps: list[float] = []

    async def before_tool_call(self, ctx: HookContext) -> None:
        self._pending_call_key = stable_call_hash(
            ctx.inputs.name, ctx.inputs.args  # type: ignore[attr-defined]
        )

    async def after_tool_call(self, ctx: HookContext) -> None:
        result = ctx.extra.get("_tool_result", "")
        self._record_and_classify(result)

    async def on_tool_exception(self, ctx: HookContext) -> None:
        outcome = str(ctx.exception) if ctx.exception else "error"
        self._record_and_classify(outcome)

    async def before_model_call(self, ctx: HookContext) -> None:
        """在 before_model_call 注入纠偏消息（如有检测到循环）。"""
        if self._fired_severity is None or self._fired_severity < Severity.MEDIUM:
            return
        if not self._check_remediation_budget():
            return
        severity_label = self._fired_severity.name
        ctx.inputs.messages = list(ctx.inputs.messages) + [{
            "role": "system",
            "content": (
                f"[DETECTION] 检测到重复工具调用模式（严重度: {severity_label}）。"
                "请换一种策略、尝试不同的参数、或向用户确认需求。"
                "不要重复执行相同的工具调用。"
            ),
        }]
        self._remediation_timestamps.append(time.monotonic())
        log.info(
            "[RepeatToolDetection] Injected remediation message (severity=%s)",
            severity_label,
        )

    # --- 内部方法 ---

    def _record_and_classify(self, outcome: str) -> None:
        """记录完成调用并运行分类检测。"""
        if self._pending_call_key is None:
            return
        call_key = self._pending_call_key
        self._pending_call_key = None
        outcome_key = stable_result_hash(outcome[:1000])
        self._history.append((call_key, outcome_key))

        severity = self._classify(call_key)
        if severity is None:
            return
        # Edge-triggered: 只在严重度上升时触发
        if self._fired_severity is not None and severity <= self._fired_severity:
            return
        self._fired_severity = severity
        log.warning(
            "[RepeatToolDetection] Anomaly detected: severity=%s, call_key=%s",
            severity.name, call_key[:8],
        )

    def _classify(self, call_key: str) -> Severity | None:
        """4 级分类检测，返回最高严重度。"""
        repeat_warn = self._repeat_warn or _get_repeat_warn()
        pingpong_warn = self._pingpong_warn or _get_pingpong_warn()
        loop_block = self._loop_block or _get_loop_block()
        global_stop = self._global_stop or _get_global_stop()

        # CRITICAL / HIGH: 尾部连续相同 (call+outcome)
        trailing = self._trailing_identical()
        if trailing >= global_stop:
            return Severity.CRITICAL
        if trailing >= loop_block:
            return Severity.HIGH

        # MEDIUM: A-B-A-B 交替
        alternation = self._trailing_alternation()
        if alternation >= pingpong_warn:
            return Severity.MEDIUM

        # LOW: 同一 call_key 在窗口内重复
        repeats = sum(1 for ck, _ in self._history if ck == call_key)
        if repeats >= repeat_warn:
            return Severity.LOW

        return None

    def _trailing_identical(self) -> int:
        """尾部连续相同 (call_key, outcome_key) 的计数。"""
        if not self._history:
            return 0
        last = self._history[-1]
        count = 0
        for record in reversed(self._history):
            if record == last:
                count += 1
            else:
                break
        return count

    def _trailing_alternation(self) -> int:
        """尾部 A-B-A-B 交替模式的计数。"""
        if len(self._history) < 2:
            return 0
        sequence = list(reversed(self._history))
        first = sequence[0]
        second = sequence[1]
        if first == second or first[0] == second[0]:
            return 0
        count = 0
        for idx, record in enumerate(sequence):
            expected = first if idx % 2 == 0 else second
            if record == expected:
                count += 1
            else:
                break
        return count

    def _check_remediation_budget(self) -> bool:
        """限频：1 分钟内最多 N 次注入。"""
        max_per_minute = self._remediation_max_per_minute or _get_remediation_max_per_minute()
        now = time.monotonic()
        self._remediation_timestamps = [
            ts for ts in self._remediation_timestamps if now - ts < 60
        ]
        return len(self._remediation_timestamps) < max_per_minute


# --- Config lazy reads ---

def _get_history_size() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.history_size


def _get_repeat_warn() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.repeat_warn


def _get_pingpong_warn() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.pingpong_warn


def _get_loop_block() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.loop_block


def _get_global_stop() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.global_stop


def _get_remediation_max_per_minute() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.remediation_max_per_minute
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_repeat_tool_call_detector_hook.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite for regression**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py tests/test_repeat_tool_call_detector_hook.py
git commit -m "feat(hooks): add RepeatToolCallDetectorHook — sliding window + stable hash loop detection"
```

---

### Task 5: Wire up — exports, auto-wire, integration

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/__init__.py`
- Modify: `twinkle/agentserver/server.py:79,85`
- Test: `python -m pytest tests/ -v` (full regression)

**Interfaces:**
- Consumes: `ContextOverflowRecoveryHook` from Task 3, `RepeatToolCallDetectorHook` from Task 4
- Produces: Both hooks registered in `build_agent_loop` and exported from `hooks/builtin/__init__.py`

- [ ] **Step 1: Update hooks/builtin/__init__.py**

Add two new imports:

```python
from twinkle.agentserver.hooks.builtin.context_overflow_recovery_hook import ContextOverflowRecoveryHook
from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import RepeatToolCallDetectorHook
```

Update `__all__` to include both:

```python
__all__ = [
    "ContextCompressionHook", "ContextOverflowRecoveryHook",
    "LoggingHook", "MemoryHook", "PermissionHook",
    "RepeatToolCallDetectorHook", "RetryHook", "SkillHook", "SubagentContextHook",
]
```

- [ ] **Step 2: Update server.py build_agent_loop**

In `twinkle/agentserver/server.py`, update the import line (around line 79) from:

```python
from twinkle.agentserver.hooks.builtin import SubagentContextHook, ContextCompressionHook
```

to:

```python
from twinkle.agentserver.hooks.builtin import (
    SubagentContextHook, ContextCompressionHook,
    ContextOverflowRecoveryHook, RepeatToolCallDetectorHook,
)
```

Update the auto-wire loop (around line 85) from:

```python
for hook in list(hooks or []) + [SubagentContextHook(executor), ContextCompressionHook(llm=llm)]:
```

to:

```python
for hook in list(hooks or []) + [
    SubagentContextHook(executor),
    ContextCompressionHook(llm=llm),
    ContextOverflowRecoveryHook(llm=llm),
    RepeatToolCallDetectorHook(),
]:
    loop.register_hook(hook)
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/__init__.py twinkle/agentserver/server.py
git commit -m "feat(server): auto-wire ContextOverflowRecoveryHook + RepeatToolCallDetectorHook"
```

---

### Task 6: Update roadmap — mark Phase 9 as completed

**Files:**
- Modify: `roadmap.md`

- [ ] **Step 1: Update roadmap.md**

Change Phase 9 status from planned to completed:

```markdown
### Phase 9 — 上下文溢出恢复 + 重复调用检测  `[已完成]`
```

Add "已落地" summary after the content block, matching the pattern of other completed phases.

Update milestone M13 row to add ✅.

- [ ] **Step 2: Commit and push**

```bash
git add roadmap.md
git commit -m "docs: mark Phase 9 as completed in roadmap"
```
