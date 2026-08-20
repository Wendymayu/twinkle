# 模型上下文窗口感知（A+B）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **执行注意（用户偏好）：** 用户不擅自 commit。每个 Task 末尾的 commit 步骤执行前需向用户确认 commit 节奏（可批量、可逐个）。

**Goal:** 让 Twinkle 按当前模型知道上下文窗口（`resolve_context_window_limit()`），用于 413 恢复兜底（A）与预防压缩触发（B）。

**Architecture:** 新增 `model_catalog.py` 提供三级解析（config 手动覆盖 > 模型字典前缀匹配 > 128000 兜底）；一个 `context_compression.trigger_ratio: 0.8` 供 A/B 共用，删除 `overflow_recovery.threshold_ratio`。

**Tech Stack:** Python 3, pydantic, asyncio.run() 测试（无 pytest-asyncio），monkeypatch。

**Spec:** `docs/superpowers/specs/2026-08-20-model-context-window-design.md`

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `twinkle/config/model_catalog.py` | 模型→窗口字典 + normalize + resolve | 新建 |
| `twinkle/config/__init__.py` | 导出 `CONTEXT_TRIGGER_RATIO`、`resolve_context_window_limit` | 改 |
| `twinkle/config/schema.py` | `ContextCompressionConfig` 加 `trigger_ratio`；`OverflowRecoveryConfig` 删 `threshold_ratio` | 改 |
| `twinkle/resources/config.yaml` | 加 `trigger_ratio: 0.8`；删 `threshold_ratio: 0.85` | 改 |
| `twinkle/agentserver/hooks/builtin/context_compression_hook.py` | `_get_token_threshold` 加动态分支 | 改 |
| `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py` | 改用 `resolve` + `CONTEXT_TRIGGER_RATIO`；构造参数重命名 | 改 |
| `tests/test_model_catalog.py` | resolve 优先级链 | 新建 |
| `tests/test_context_compression_hook.py` | 动态阈值 | 改 |
| `tests/test_context_overflow_recovery_hook.py` | ratio 0.85→0.8、构造参数名、新增 resolved 分支测试 | 改 |
| `tests/test_compression_config.py` | trigger_ratio 默认值 | 改 |

任务顺序保证每步中间态可运行：Task1 新建独立模块 → Task2 只加字段（消费者未改，不破坏）→ Task3/4 改消费者 → Task5 删旧字段。

---

### Task 1: `model_catalog.py` — resolve 优先级链

**Files:**
- Create: `twinkle/config/model_catalog.py`
- Test: `tests/test_model_catalog.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_model_catalog.py
from twinkle.config.model_catalog import (
    MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW_TOKENS,
    normalize_model, resolve_context_window_limit,
)


def test_normalize_strips_tag_suffix():
    assert normalize_model("gpt-4o-mini:latest") == "gpt-4o-mini"
    assert normalize_model("GPT-4o-Mini") == "gpt-4o-mini"
    assert normalize_model("gpt-4o-mini") == "gpt-4o-mini"


def test_manual_override_wins():
    # config context_window_limit_tokens > 0 → 手动覆盖最高优先
    assert resolve_context_window_limit(
        model="gpt-4o-mini", manual_override=200_000) == 200_000


def test_dict_prefix_match_longest_key():
    # gpt-4o-mini-2024-07-18 同时匹配 gpt-4o 与 gpt-4o-mini，取最长 key
    assert resolve_context_window_limit(
        model="gpt-4o-mini-2024-07-18", manual_override=0) == 128_000


def test_dict_match_claude_dated():
    assert resolve_context_window_limit(
        model="claude-3-5-sonnet-20240129", manual_override=0) == 200_000


def test_unknown_model_falls_back_to_default():
    assert resolve_context_window_limit(
        model="some-unknown-model-x", manual_override=0) == DEFAULT_CONTEXT_WINDOW_TOKENS == 128_000


def test_default_token_value():
    assert DEFAULT_CONTEXT_WINDOW_TOKENS == 128_000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_model_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.config.model_catalog'`

- [ ] **Step 3: 实现 model_catalog.py**

```python
# twinkle/config/model_catalog.py
"""模型上下文窗口目录 + 解析器。

三级优先：config 手动覆盖 > 模型字典(前缀匹配) > 128000 兜底。
对齐 jiuwenswarm(全局单值) 与 openclaw(catalog 查表) 的折中：精简字典 + 兜底。
窗口值用于 A(413 恢复兜底) 与 B(预防压缩触发)。
"""
from __future__ import annotations

from twinkle.config import settings

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
}

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def normalize_model(name: str) -> str:
    """lowercase + 去掉首个 `:` 及之后(兼容 gpt-4o-mini:latest / ollama 带标签写法)。"""
    return name.lower().split(":", 1)[0]


def resolve_context_window_limit(
    *, model: str | None = None, manual_override: int | None = None
) -> int:
    """三级解析当前模型上下文窗口 token 上限。

    1. manual_override > 0(默认读 settings.overflow_recovery.context_window_limit_tokens) → 手动覆盖
    2. 模型字典前缀匹配(取最长 key) → 字典值
    3. 无匹配 → DEFAULT_CONTEXT_WINDOW_TOKENS
    """
    mo = (manual_override if manual_override is not None
          else settings.overflow_recovery.context_window_limit_tokens)
    if mo > 0:
        return mo

    m = normalize_model(model if model is not None else settings.llm.model)
    matched = [k for k in MODEL_CONTEXT_WINDOWS if m.startswith(k)]
    if matched:
        longest = max(matched, key=len)
        return MODEL_CONTEXT_WINDOWS[longest]
    return DEFAULT_CONTEXT_WINDOW_TOKENS
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_model_catalog.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: commit（执行前问用户）**

```bash
git add twinkle/config/model_catalog.py tests/test_model_catalog.py
git commit -m "feat(config): add model_catalog with resolve_context_window_limit"
```

---

### Task 2: config 加 `trigger_ratio` 字段 + 导出

**Files:**
- Modify: `twinkle/config/schema.py:82-91` (ContextCompressionConfig)
- Modify: `twinkle/config/__init__.py:73-85`
- Modify: `twinkle/resources/config.yaml:25-39`
- Test: `tests/test_compression_config.py`

- [ ] **Step 1: 写失败测试（trigger_ratio 默认 0.8）**

先看现有 `tests/test_compression_config.py` 风格，追加：

```python
def test_trigger_ratio_default():
    from twinkle.config.schema import ContextCompressionConfig
    assert ContextCompressionConfig().trigger_ratio == 0.8
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compression_config.py::test_trigger_ratio_default -v`
Expected: FAIL — `AttributeError: ... 'trigger_ratio'` 或 `ValidationError`

- [ ] **Step 3a: schema.py 加字段**

在 `ContextCompressionConfig`（`schema.py:82`）的 `token_threshold` 行后加：

```python
class ContextCompressionConfig(_StrictModel):
    token_threshold: int = 60000
    trigger_ratio: float = 0.8  # B: token_threshold=0 时按 窗口×trigger_ratio 动态触发(0.8=80%);A/B 共用
    keep_recent_pairs: int = 6
    # ... 其余不变
```

- [ ] **Step 3b: __init__.py 导出**

在 `# --- context compression (Phase 3) ---` 段（`__init__.py:74`）加：

```python
CONTEXT_TOKEN_THRESHOLD = settings.context_compression.token_threshold
CONTEXT_TRIGGER_RATIO = settings.context_compression.trigger_ratio  # A/B 共用窗口比例
```

- [ ] **Step 3c: config.yaml 加值**

在 `context_compression:` 段（`config.yaml:25`）`token_threshold` 行后加：

```yaml
context_compression:
  token_threshold: 60000                  # >0=手动绝对覆盖;0=按 窗口×trigger_ratio 动态(char//3 估算)
  trigger_ratio: 0.8                      # 窗口×比例 动态触发阈值;A(413恢复)与B(预防压缩)共用
  # ... 其余不变
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_compression_config.py -v`
Expected: PASS

- [ ] **Step 5: commit（执行前问用户）**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml tests/test_compression_config.py
git commit -m "feat(config): add context_compression.trigger_ratio (0.8, shared A/B)"
```

---

### Task 3: compression hook 动态阈值

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/context_compression_hook.py:40-42`
- Test: `tests/test_context_compression_hook.py`

- [ ] **Step 1: 写失败测试（token_threshold=0 → 动态 resolved×ratio）**

在 `tests/test_context_compression_hook.py` 末尾加：

```python
def test_dynamic_threshold_when_token_threshold_zero(monkeypatch):
    """token_threshold=0 时用 resolved×trigger_ratio 动态阈值(替代固定 60000)。"""
    import twinkle.config
    import twinkle.agentserver.hooks.builtin.context_compression_hook as h
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 0)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TRIGGER_RATIO", 0.8)
    monkeypatch.setattr(h, "resolve_context_window_limit", lambda: 128_000)
    assert h._get_token_threshold() == int(128_000 * 0.8)  # 102400


def test_absolute_threshold_still_wins(monkeypatch):
    """token_threshold>0 仍优先(向后兼容手动绝对值)。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 999)
    assert h._get_token_threshold() == 999  # 注:h 已在上个测试 import,或重新 import
```

注：`h` 在第二个测试需 import；实现时两个测试同文件顶部 `import twinkle.agentserver.hooks.builtin.context_compression_hook as h`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_compression_hook.py::test_dynamic_threshold_when_token_threshold_zero -v`
Expected: FAIL — `_get_token_threshold` 仍返回 0（CONTEXT_TOKEN_THRESHOLD 被 patch 成 0），且未调 resolve

- [ ] **Step 3: 改 `_get_token_threshold`**

`context_compression_hook.py:40-42` 改为：

```python
def _get_token_threshold():
    from twinkle.config import CONTEXT_TOKEN_THRESHOLD, CONTEXT_TRIGGER_RATIO
    if CONTEXT_TOKEN_THRESHOLD > 0:
        return CONTEXT_TOKEN_THRESHOLD  # 手动绝对覆盖(向后兼容)
    from twinkle.config.model_catalog import resolve_context_window_limit
    return int(resolve_context_window_limit() * CONTEXT_TRIGGER_RATIO)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_compression_hook.py -v`
Expected: PASS（含原有 4 个 + 新 2 个）

注意：原有 `test_uses_config_defaults_when_no_override` monkeypatch `CONTEXT_TOKEN_THRESHOLD=1`（>0）→ 走绝对值分支，仍过，无需改。

- [ ] **Step 5: commit（执行前问用户）**

```bash
git add twinkle/agentserver/hooks/builtin/context_compression_hook.py tests/test_context_compression_hook.py
git commit -m "feat(compression): dynamic threshold = window×trigger_ratio when token_threshold=0"
```

---

### Task 4: overflow hook 改用 resolve + trigger_ratio

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`（__init__、on_model_exception、_get_config_context_limit、_get_threshold_ratio→_get_trigger_ratio）
- Test: `tests/test_context_overflow_recovery_hook.py`（3 处 ratio、参数名、注释 + 1 新测试）

- [ ] **Step 1: 写失败测试（解析不到 limit → 用 resolved×0.8，不再盲压 0）**

在 `tests/test_context_overflow_recovery_hook.py` 末尾加：

```python
def test_uses_resolved_window_when_limit_not_parsed(monkeypatch):
    """413 没带 limit(N>M) → 用 resolved×0.8 兜底(替代旧的盲压 0)。"""
    import twinkle.agentserver.hooks.builtin.context_overflow_recovery_hook as h
    monkeypatch.setattr(h, "resolve_context_window_limit", lambda: 1000)
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3, aggressive_keep_recent=2)
    big = _big_messages()  # ~1400 估算 token > 800(resolved 1000×0.8) → 触发压缩
    ctx = _Ctx(big, exception=_Exc413("context_length_exceeded"))  # 无 N>M,解析不到 limit
    asyncio.run(hook.on_model_exception(ctx))
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert ctx._retry_request is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_overflow_recovery_hook.py::test_uses_resolved_window_when_limit_not_parsed -v`
Expected: FAIL — `_get_config_context_limit` 仍读旧 `settings.overflow_recovery.context_window_limit_tokens`(=0) → threshold_override=0 → big 1400 > 0 压缩... 实际会 PASS（盲压 0 也压缩）。
> 注：此测试在改前可能恰好通过（盲压 0 也触发压缩）。真正的"不再盲压 0"靠 Step 3 改 `_get_config_context_limit` 调 resolve 后，monkeypatch resolve=1000 → threshold=800 < 1400 仍压缩。为暴露"盲压 0 已消失"，可加断言 threshold_override 非 0——但它是局部变量。接受此测试作为"resolved 分支可达"的回归保护即可。

- [ ] **Step 3a: hook 改 import + __init__ 参数重命名**

`context_overflow_recovery_hook.py` 顶部 import 段加（在 `from twinkle.agentserver.compression import compress_messages` 附近）：

```python
from twinkle.config.model_catalog import resolve_context_window_limit
```

`__init__`（line 81-94）参数 `threshold_ratio` → `trigger_ratio`：

```python
    def __init__(
        self,
        llm: "LLMClient",
        *,
        max_recovery_attempts: int | None = None,
        aggressive_keep_recent: int | None = None,
        trigger_ratio: float | None = None,
    ) -> None:
        self._llm = llm
        self._max_recovery_attempts = max_recovery_attempts
        self._aggressive_keep_recent = aggressive_keep_recent
        self._trigger_ratio = trigger_ratio
        self._overflow_counts: dict[str, int] = {}
```

- [ ] **Step 3b: on_model_exception 用新名 + resolve**

`on_model_exception`（line 119-136）中：

```python
        aggressive_keep = self._aggressive_keep_recent or _get_aggressive_keep_recent()
        ratio = self._trigger_ratio or _get_trigger_ratio()
        threshold_override = None
        if limit_tokens is not None:
            threshold_override = int(limit_tokens * ratio)
        else:
            threshold_override = int(resolve_context_window_limit() * ratio)
        # 溢出恢复强制压缩:threshold 设为 resolved×ratio 确保 should_compress 触发
        # (真实 overflow 时 messages > 窗口 > threshold;测试用 monkeypatch resolve 放小窗口)
```

删去原 `_get_config_context_limit()` 调用与 `if threshold_override is None: threshold_override = 0` 分支（解析不到时已由 resolve 兜底，不再盲压 0）。

- [ ] **Step 3c: _get_config_context_limit → 调 resolve；_get_threshold_ratio → _get_trigger_ratio**

文件末尾（line 178-202）：

```python
def _get_trigger_ratio() -> float:
    from twinkle.config import CONTEXT_TRIGGER_RATIO
    return CONTEXT_TRIGGER_RATIO


def _get_config_context_limit() -> int:
    return resolve_context_window_limit()
```

> `_get_config_context_limit` 现仅调 resolve（保留函数名以最小改动；亦可 inline）。删除原 `_get_threshold_ratio` 函数（已由 `_get_trigger_ratio` 取代）。

- [ ] **Step 4: 改现有测试 3 处 ratio + 参数名 + 注释**

`tests/test_context_overflow_recovery_hook.py`：

- line 122-135 `test_compresses_and_requests_retry_on_413`：exc 改为带 limit（让解析到 limit 分支触发压缩，因 big~1400 需小 threshold）：

```python
def test_compresses_and_requests_retry_on_413():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, trigger_ratio=0.8,
    )
    big = _big_messages()
    ctx = _Ctx(big, exception=_ExcNoStatus("prompt is too long: 10000 tokens > 1000"))
    asyncio.run(hook.on_model_exception(ctx))
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert ctx._retry_request is not None
```

- line 148-164 `test_circuit_break_after_max_attempts`：参数名 `threshold_ratio=0.85` → `trigger_ratio=0.8`（3 次 413，circuit break 不依赖压缩，exc 可保留 `"overflow"`）：

```python
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=2,
        aggressive_keep_recent=2, trigger_ratio=0.8,
    )
```

- line 180-194 `test_uses_parsed_limit_for_threshold`：参数名 + 值 + 注释：

```python
def test_uses_parsed_limit_for_threshold():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, trigger_ratio=0.8,
    )
    big = _big_messages()
    # "prompt is too long: 10000 tokens > 1000" → limit=1000 → threshold_override=int(1000*0.8)=800 < ~1400
    ctx = _Ctx(big, exception=_ExcNoStatus("prompt is too long: 10000 tokens > 1000"))
    asyncio.run(hook.on_model_exception(ctx))
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert ctx._retry_request is not None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_context_overflow_recovery_hook.py -v`
Expected: PASS（含原 16 个改后 + 新 1 个）

- [ ] **Step 6: commit（执行前问用户）**

```bash
git add twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py tests/test_context_overflow_recovery_hook.py
git commit -m "feat(overflow): use resolve×trigger_ratio fallback (no more blind-0 compress)"
```

---

### Task 5: 删 `overflow_recovery.threshold_ratio` + 全测试验证

**Files:**
- Modify: `twinkle/config/schema.py:205-209` (OverflowRecoveryConfig)
- Modify: `twinkle/resources/config.yaml:107-111`

- [ ] **Step 1: 删 schema 字段**

`schema.py:205-209` `OverflowRecoveryConfig` 删 `threshold_ratio` 行：

```python
class OverflowRecoveryConfig(_StrictModel):
    max_recovery_attempts: int = 3
    aggressive_keep_recent: int = 3
    context_window_limit_tokens: int = 0    # >0 = 手动覆盖窗口(优先于字典);0 = 字典/128000 兜底
```

- [ ] **Step 2: 删 config.yaml 字段**

`config.yaml:107-111` `overflow_recovery` 段删 `threshold_ratio: 0.85` 行：

```yaml
overflow_recovery:
  max_recovery_attempts: 3
  aggressive_keep_recent: 3
  context_window_limit_tokens: 0    # 0=字典/128000 兜底;>0=手动覆盖窗口
```

- [ ] **Step 3: 跑全测试验证无残留引用**

Run: `python -m pytest tests/ -q`
Expected: PASS（全绿）。若有 `AttributeError: ... threshold_ratio` → 残留引用，grep 修。

确认无残留：`grep -rn "threshold_ratio" twinkle/ --include="*.py" | grep -v __pycache__`
Expected: 无输出（构造参数已重命名为 trigger_ratio，config 字段已删）。

- [ ] **Step 4: commit（执行前问用户）**

```bash
git add twinkle/config/schema.py twinkle/resources/config.yaml
git commit -m "refactor(config): remove overflow_recovery.threshold_ratio (merged into trigger_ratio)"
```

---

## Self-Review

**1. Spec coverage:**
- resolve 优先级链（config>字典>128000）→ Task 1 ✅
- A: overflow hook 解析不到用 resolved×0.8（不盲压 0）→ Task 4 ✅
- B: 预防压缩 resolved×0.8 → Task 3 ✅
- 一个 trigger_ratio A/B 共用 → Task 2/3/4 ✅
- 删 overflow_recovery.threshold_ratio → Task 5 ✅
- 413 被动解析保留 → Task 4 未动 `_parse_token_limits` ✅
- 不改 estimate_tokens/前端 → 非目标，无 Task ✅

**2. Placeholder scan:** 无 TBD/TODO；每步含完整代码与命令。✅

**3. Type consistency:** `resolve_context_window_limit(*, model, manual_override)` 签名 Task 1 定义，Task 4/3 调用一致；`_get_trigger_ratio`、`CONTEXT_TRIGGER_RATIO`、`self._trigger_ratio` 跨 Task 2/3/4 命名一致。✅

**已知行为变化（非 bug）：** overflow 解析不到 limit 时 threshold 从 0（盲压/总压）改为 resolved×0.8。真实 overflow（messages > 窗口）仍触发压缩；仅测试假数据需 monkeypatch resolve 放小窗口（Task 4 Step 1/4 已处理）。
