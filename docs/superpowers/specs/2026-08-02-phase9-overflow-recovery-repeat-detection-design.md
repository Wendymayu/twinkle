# Phase 9 — 上下文溢出恢复 + 重复工具调用检测

> 日期: 2026-08-02
> 关联: Phase 3 上下文压缩（`docs/superpowers/specs/2026-07-23-phase3-context-compression-design.md`）、Hook 设计（`docs/superpowers/specs/2026-07-23-hook-mechanism-design.md`）、ContextCompressionHook（`docs/superpowers/specs/2026-08-01-context-compression-hook-design.md`）
> 状态: 已批准，待实现

## 1. 背景与动机

Phase 3 的 `ContextCompressionHook` 是**主动压缩**——每步 `before_model_call` 估算 token 超 `token_threshold` 时压缩 middle。但存在两个缺口：

1. **413 挂死**：估算不精确（`char//3` 是粗估）或模型窗口比预期小时，LLM 返回 `context_length_exceeded` / 413，`RetryHook` 不重试（413 不属于 transient），异常直接传播 → `e2a.error` → 用户看到错误，agent 无法自救。
2. **循环调用**：agent 陷入重复工具调用（同名 + 同参数、或 A-B-A-B 交替），无自动纠偏，浪费 token 和时间直到 `max_steps` 耗尽。

Phase 9 填补这两个缺口：**被动溢出恢复**（413 → 强制压缩 → 重试）+ **循环检测纠偏**（重复模式 → 注入纠偏消息）。

## 2. 参考实现对照（jiuwenswarm）

### ContextOverflowRecoveryRail

`jiuwenclaw/agentserver/deep_agent/rails/context_overflow_recovery_rail.py`（`enterprise_dev` 分支）：

- **Priority 100**，`on_model_exception` + `before_model_call` + `after_model_call` 三事件。
- **检测**：`_is_context_overflow_error` — 3 层判定：`status_code==413` → `status_code==400 + 溢出关键词` → `status_code is None + 溢出关键词`。关键词覆盖 Anthropic（`prompt is too long` / `input too long`）、OpenAI（`context_length_exceeded` / `maximum context length`）、华为（`maximum input length` / `must less than the maximum input`）。
- **Token 解析**：`_parse_token_limits` — 正则从异常消息提取 `actual_tokens` 和 `limit_tokens`，支持 Anthropic（`N tokens > M`）、华为（`prompt length N must less than maximum input length M`）、OpenAI（`maximum context length is N`）三种格式。
- **恢复**：`_set_force_compact_flag` 设置 `FullCompactProcessor.force_compact=True` + `threshold_override = limit_tokens × 0.85`，然后 `ctx.request_retry()`。
- **熔断**：`max_recovery_attempts=2`，连续溢出超过后 `_circuit_break` 发 error stream 事件给用户。
- **成功重置**：`after_model_call` 中 `consecutive_overflow_count = 0`。

**关键差异**：jiuwenswarm 有 `FullCompactProcessor`（多级压缩链 + force_compact flag + threshold_override 设置），Twinkle 的 `compress_messages` 是单级压缩。Twinkle 的等价操作是：直接调用 `compress_messages` 传入更激进的参数，赋回 `ctx.inputs.messages`。

### RepeatToolCallDetector

`openjiuwen/agent_teams/reliability/detectors/repeat_tool.py`（`develop` 分支）：

- **滑动窗口**：`deque(maxlen=30)` 记录 `(call_key, outcome_key)` 对。
- **stable hash**：`stable_call_hash` — SHA-256(name + sorted args)；`stable_result_hash` — SHA-256(result)。结果也参与 hash，区分"重复调用但结果在变"（有进展）vs"重复调用且结果相同"（无进展）。
- **4 级检测**（edge-triggered，只升不降）：
  - LOW：同一 `call_key` 在窗口内 ≥ `repeat_warn` 次
  - MEDIUM：A-B-A-B 交替 ≥ `pingpong_warn` 次
  - HIGH：尾部连续相同 (call+outcome) ≥ `loop_block` 次
  - CRITICAL：尾部连续相同 ≥ `global_stop` 次
- **LocalAutoRemediator**：限频（5 次/60s）+ 严重度到策略映射（LOW=OBSERVE_ONLY, MEDIUM=LOCAL_STEER, HIGH=LOCAL_STEER, CRITICAL=LOCAL_STEER+ESCALATE_USER）。

**关键差异**：jiuwenswarm 的 detector 是独立组件 + `ReliabilityRail` 桥接 + `LocalAutoRemediator` 分离。Twinkle 将 detector 和 remediator 内化到一个 Hook 中，因为单 agent 不需要跨进程的 reliability monitor。

## 3. 目标与非目标

**目标**

- 413 / `context_length_exceeded` 时自动压缩重试，agent 不挂死。
- 重复工具调用循环时自动注入纠偏消息，agent 跳出循环。
- 两个能力独立，复用现有 Hook 系统，不依赖 Task Loop。

**非目标（YAGNI，留待后续）**

- 不做 `FullCompactProcessor` 多级压缩链（Twinkle 的 `compress_messages` 是单级，够用）。
- 不做 offload + reload / protected 清单 / 结构化摘要 prompt。
- 不做跨进程的 `ReliabilityMonitor` / `AnomalyReporter` / team-level detector。
- 不做 `before_model_call` 的 proactive bridge（jiuwenswarm 的 `consume_deferred_overflow_recovery`），Twinkle 的 `ContextCompressionHook` 主动压缩已覆盖。

## 4. 方案

### 4.1 ContextOverflowRecoveryHook

**文件**：`twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`

```python
"""ContextOverflowRecoveryHook — on_model_exception 溢出恢复。

被动恢复：LLM 抛 413 / context_length_exceeded 时，强制更激进压缩后重试。
连续失败熔断：超过 max_recovery_attempts 后注入熔断消息让 LLM 产出最终回答。
成功后重置计数器。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from typing import TYPE_CHECKING

from twinkle.agentserver.compression import compress_messages, estimate_tokens
from twinkle.agentserver.hooks.base import AgentHook, HookContext

if TYPE_CHECKING:
    from twinkle.agentserver.llm_client import LLMClient

log = logging.getLogger("twinkle.hooks.overflow_recovery")

# 413 恢复时 threshold_override 占模型窗口的比例
RECOVERY_THRESHOLD_RATIO = 0.85


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
        # 否则从 config 读 context_window_limit_tokens（如有）
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
                "[ContextOverflowRecovery] Aggressive compression applied: "
                "tokens %d → %d, requesting retry",
                estimate_tokens(ctx.inputs.messages) if not compressed else 0,
                estimate_tokens(compressed),
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
        # 注入一条 system 消息提示 LLM 产出最终回答
        ctx.inputs.messages = list(ctx.inputs.messages) + [{
            "role": "system",
            "content": (
                "[CONTEXT_OVERFLOW] 上下文持续溢出，自动压缩恢复失败。"
                "请用当前已有信息总结回答用户，建议用户开始新会话。"
            ),
        }]
        self._consecutive_overflow_count = 0


# --- 413 检测 ---

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
    2. status_code==400 + 溢出关键词 → 判定（400 状态码有多种错误类型，需结合关键词）
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

### 4.2 RepeatToolCallDetectorHook

**文件**：`twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py`

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

from twinkle.agentserver.hooks.base import AgentHook, HookContext, ToolCallInputs

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

    Priority 88: 在 ContextCompressionHook(95) 之后、SkillHook(90) 之前。
    需要在压缩后看到消息，但在 skill 注入前注入纠偏消息。
    （HookManager 按优先级降序执行，88 > 85 > 80，在 SkillHook(90) 和 MemoryHook(80) 之间。）

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

        # 运行时状态（per-session 生命周期，由 AgentLoop 管理）
        self._history: deque[tuple[str, str]] = deque(maxlen=history_size or 30)
        self._pending_call_key: str | None = None
        self._fired_severity: Severity | None = None
        self._remediation_timestamps: list[float] = []  # 限频窗口

    async def before_tool_call(self, ctx: HookContext) -> None:
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        self._pending_call_key = stable_call_hash(inputs.name, inputs.args)

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
        # 限频检查
        if not self._check_remediation_budget():
            return
        # 注入纠偏 system 消息
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
        outcome_key = stable_result_hash(outcome) if len(outcome) < 1000 else stable_result_hash(outcome[:1000])
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
        # 清理过期时间戳
        self._remediation_timestamps = [
            ts for ts in self._remediation_timestamps if now - ts < 60
        ]
        return len(self._remediation_timestamps) < max_per_minute


# --- Config lazy reads ---

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

### 4.3 配置

**`twinkle/config/schema.py`** 新增两个配置类：

```python
class OverflowRecoveryConfig(_StrictModel):
    max_recovery_attempts: int = 3          # 连续溢出恢复最大次数
    threshold_ratio: float = 0.85           # 恢复后目标占模型窗口比例
    aggressive_keep_recent: int = 3         # 溢出恢复时 keep_recent_pairs 减到此值
    context_window_limit_tokens: int = 0    # 0 = 不设（从 413 解析）；>0 = 手动指定模型窗口


class RepeatToolDetectionConfig(_StrictModel):
    history_size: int = 30                  # 滑动窗口大小
    repeat_warn: int = 10                   # LOW 阈值
    pingpong_warn: int = 10                 # MEDIUM 阈值
    loop_block: int = 20                    # HIGH 阈值
    global_stop: int = 30                   # CRITICAL 阈值
    remediation_max_per_minute: int = 5     # 纠偏注入限频
```

**`twinkle/resources/config.yaml`** 新增：

```yaml
overflow_recovery:
  max_recovery_attempts: 3
  threshold_ratio: 0.85
  aggressive_keep_recent: 3
  context_window_limit_tokens: 0    # 0 = 从 413 错误解析; >0 = 手动指定

repeat_tool_detection:
  history_size: 30
  repeat_warn: 10
  pingpong_warn: 10
  loop_block: 20
  global_stop: 30
  remediation_max_per_minute: 5
```

### 4.4 导出 + 注册

**`hooks/builtin/__init__.py`** 追加：

```python
from twinkle.agentserver.hooks.builtin.context_overflow_recovery_hook import ContextOverflowRecoveryHook
from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import RepeatToolCallDetectorHook
```

**`server.py`** `build_agent_loop` 追加 auto-wire：

```python
from twinkle.agentserver.hooks.builtin import (
    SubagentContextHook, ContextCompressionHook,
    ContextOverflowRecoveryHook, RepeatToolCallDetectorHook,
)
for hook in list(hooks or []) + [
    SubagentContextHook(executor),
    ContextCompressionHook(llm=llm),
    ContextOverflowRecoveryHook(llm=llm),
    RepeatToolCallDetectorHook(),
]:
    loop.register_hook(hook)
```

### 4.5 decorator.py 小改动

`@hook` 装饰器在 `AFTER_TOOL_CALL` 事件触发时，方法返回值（tool result 字符串）未存入 `ctx`。`RepeatToolCallDetectorHook.after_tool_call` 需要读取 result 来计算 `outcome_key`。

**改动**：在 `decorator.py` 的 `wrapper` 中，方法成功后、触发 after 事件前，存入 `ctx.extra["_tool_result"]`：

```python
# 当前代码（decorator.py:63-65）：
result = await method(self, ctx, *args, **kwargs)
# 4. Trigger after event on success
await hook_manager.execute(after, ctx)
return result

# 改为：
result = await method(self, ctx, *args, **kwargs)
# Store result for after-event hooks (e.g., RepeatToolCallDetectorHook)
ctx.extra["_tool_result"] = result
# 4. Trigger after event on success
await hook_manager.execute(after, ctx)
return result
```

一行改动，向后兼容：现有 `after_tool_call` hook 不读 `ctx.extra["_tool_result"]`，不受影响。

### 4.6 RetryHook 不变

`RetryHook` 的 `TRANSIENT_EXCEPTIONS` 不包含 413 / `BadRequestError`。`ContextOverflowRecoveryHook` 的 priority 60 高于 `RetryHook` 的 50，所以 `on_model_exception` 时：

1. `ContextOverflowRecoveryHook`（60）先运行 → 检测到 413 → 压缩 + `request_retry()`
2. `RetryHook`（50）后运行 → 检测到 413 不是 transient → no-op

两者不冲突。如果异常不是 413，`ContextOverflowRecoveryHook` no-op，`RetryHook` 正常处理 transient。

## 5. 行为验证

### 5.1 溢出恢复

```
LLM 抛 413 → ContextOverflowRecoveryHook.on_model_exception 检测到
→ _consecutive_overflow_count=1
→ _parse_token_limits 解析出 limit_tokens=128000
→ threshold_override = 128000 × 0.85 = 108800
→ compress_messages(msgs, llm, token_threshold=108800, keep_recent_pairs=3)
→ ctx.inputs.messages = compressed
→ ctx.request_retry(delay=0)
→ AgentLoop 重试 LLM call
→ 成功 → after_model_call 重置计数
```

### 5.2 熔断

```
LLM 抛 413 → 恢复重试 → LLM 再次抛 413 → 恢复重试 → LLM 再次抛 413
→ _consecutive_overflow_count=4 > max_recovery_attempts=3
→ _circuit_break: 注入 [CONTEXT_OVERFLOW] system 消息
→ LLM 产出最终回答（建议用户开始新会话）
```

### 5.3 循环检测

```
agent 连续调用 file_read(path="a.txt") 10 次 → LOW
→ 不注入纠偏（MEDIUM 以下不注入）
→ 继续调用，交替 file_read("a.txt") + file_read("b.txt") 10 次 → MEDIUM
→ before_model_call 注入 [DETECTION] system 消息
→ LLM 换策略
```

### 5.4 Hook 执行顺序

```
on_model_exception:
  ContextOverflowRecoveryHook(60) → RetryHook(50)

before_model_call:
  ContextCompressionHook(95) → SkillHook(90) → RepeatToolCallDetectorHook(88) → MemoryHook(80) → LoggingHook(10)

before_tool_call:
  PermissionHook(100) → RepeatToolCallDetectorHook(88)

after_tool_call:
  RepeatToolCallDetectorHook(88) → LoggingHook(10)
```

**注意**：`RepeatToolCallDetectorHook` 的 priority=88 在 `SkillHook(90)` 之后、`MemoryHook(80)` 之前。`before_model_call` 中纠偏消息在 skill 注入之后被添加，但在 memory 注入之前。由于 `_merge_system_messages` 会按内容前缀重新排序合并，注入顺序不影响最终 prompt 结构。

## 6. 测试

### 6.1 `tests/test_context_overflow_recovery_hook.py`

- `test_detects_413_status_code`：`status_code=413` 的异常被检测
- `test_detects_400_with_context_length_exceeded`：`status_code=400` + 关键词被检测
- `test_detects_anthropic_prompt_too_long`：Anthropic 格式被检测
- `test_ignores_non_overflow_error`：`RateLimitError` 不被检测
- `test_compresses_and_requests_retry`：413 后压缩 + `request_retry`
- `test_circuit_break_after_max_attempts`：连续 3 次后熔断
- `test_resets_count_on_success`：`after_model_call` 成功后重置计数
- `test_parse_token_limits_anthropic`：Anthropic 格式解析
- `test_parse_token_limits_openai`：OpenAI 格式解析

### 6.2 `tests/test_repeat_tool_call_detector_hook.py`

- `test_detects_repeat_calls`：同一工具调用 ≥ 10 次 → LOW
- `test_detects_pingpong_pattern`：A-B-A-B 交替 ≥ 10 次 → MEDIUM
- `test_detects_trailing_identical`：尾部连续相同 ≥ 20 次 → HIGH
- `test_detects_critical_loop`：尾部连续相同 ≥ 30 次 → CRITICAL
- `test_edge_triggered_only_escalates`：严重度只升不降
- `test_injects_remediation_message`：MEDIUM+ 注入纠偏消息
- `test_no_injection_below_medium`：LOW 不注入
- `test_remediation_rate_limit`：60 秒内最多 5 次注入
- `test_stable_call_hash_order_independent`：参数顺序不影响 hash

## 7. 后续优化方向（非本次）

- **窗口预算**：`threshold_override` 按模型窗口动态算（已在本设计中实现基础版），后续可加多模型自适应。
- **offload + reload**：长会话早期被压事实可检索召回（非丢弃）。
- **protected 清单**：关键 system / 工具结果不被压缩。
- **跨进程 reliability monitor**：team 场景下多 agent 共享检测状态。
- **PingPongDetector**：team 级 A-B 消息交替检测。
