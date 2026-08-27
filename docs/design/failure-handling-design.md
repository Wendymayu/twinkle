# 失败处理设计

## 一句话

Twinkle 把失败分两类：**工具失败是软失败**——工具抛异常（不吞、不重试），`@hook` 触发 `ON_TOOL_EXCEPTION` 仅供观测（审计 + 重复检测），异常再上抛到 agent loop 调用处，由 `format_tool_error()` 统一收口成 `[tool error]` 串回灌模型，ReAct 循环继续；**模型失败是硬失败**——异常上抛终止本次 run，瞬时异常（网络/超时/限流/5xx）由 `RetryHook` 重试一次，上下文溢出（413）由 `ContextOverflowRecoveryHook` 压缩后重试、连失败则熔断，仍失败由 `server.py` 兜底发 `e2a.error` 帧。循环无步数上限，靠 `RepeatToolCallDetectorHook` 的 CRITICAL 检测 `force_finish` 止损。`AuditHook` 始终在线，记每次工具调用的 success/denied/error。

工具层重试已于 2026-08-27 移除（瞬时异常重试会重复执行有副作用的方法体、无幂等保护），与 jiuwenswarm 一致（其 `@rail` 有重试机制但无 rail 激活）；模型层重试保留。MCP 传输层 `reconnect_attempts` 是 ws 连接重连、不重新执行方法，不在此列。

---

## 为什么需要失败处理

ReAct 循环每一步都可能失败：工具抛异常、模型网络错、权限拒绝、命令超时、子 agent 卡死。一种处理方式打不了天下：

1. **工具失败若直接终止**——agent 一次报错就死，无法自我修正。模型其实很擅长「看到错误 → 调整 → 重试」，前提是错误信息要喂回给它。
2. **模型失败若不终止**——坏上下文 / 死循环会反复触发同一异常，烧 token。
3. **失败要回两个地方**——回灌给模型（让它换路）和回给用户（让它知情），两者受众不同、走不同通道。
4. **崩溃要兜底**——任何一层抛未捕获异常都不该让进程或连接死掉，要降级成可读的失败回复。
5. **循环要能停**——无步数上限的循环若不设防，一个空转的 agent 会跑到天荒地老。

所以三条底线：**不崩循环**（工具失败不炸 ReAct）、**可恢复**（失败信息回灌让模型自愈；模型瞬时异常自动重试）、**可观测 + 可止损**（失败以帧/事件/审计送达，死循环 CRITICAL 硬停）。

---

## 核心二分：软失败 vs 硬失败

一条失败落到哪个分支，决定循环是否继续、回复什么：

| 失败类型 | 走哪 | 循环 | 回复 |
|---|---|---|---|
| 工具抛异常 | `_tool_call` 的 `@hook` 触发 `ON_TOOL_EXCEPTION`（仅观测）→ 异常上抛 → agent loop `except Exception` → `format_tool_error()` | **继续** | `[tool error] …` 回灌模型 |
| 工具内部抛 `ToolError`（command_exec / file_tools / web 校验等） | 同上（`ToolError` 是普通异常，`format_tool_error` 渲染时不带 kind） | **继续** | `[tool error] …` 回灌模型 |
| 工具 HTTP 错误（web_fetch / web_search 的 `raise_for_status`） | `httpx.HTTPStatusError` 冒泡到 `@hook`（非瞬时，不重试）→ 同上 | **继续** | `[tool error] HTTPStatusError: …` |
| 权限 DENY | `PermissionHook` → `request_force_finish(deny_message)` → `@hook` 跳过方法体，deny 串当 `tool_result` | **继续** | `[ERROR]: …`（引擎构造，唯一未收口前缀） |
| 权限 ASK 被用户拒绝 | `format_tool_error("tool denied by user: …")` | **继续** | `[tool error] tool denied by user: …` |
| 子 agent 失败 / 超时 | `SubagentExecutor` 永不抛，包成 `SubagentResult(success=False)` → `_wrap` 转串 | **继续** | 失败/超时串 + stop hint 回灌父 |
| 模型瞬时异常 | `except Exception` → `ON_MODEL_EXCEPTION` → `RetryHook` `request_retry` → sleep + 重试 | **重试 1 次** | 成功则正常流；仍失败→终止 |
| 模型上下文溢出（413） | `ON_MODEL_EXCEPTION` → `ContextOverflowRecoveryHook` 激进压缩 → `request_retry` | **压缩后重试** | 成功则正常；连失败→熔断 |
| 模型非瞬时异常 | `ON_MODEL_EXCEPTION` → 不重试 → `raise` | **终止** | server 兜底 `e2a.error str(exc)` |
| 死循环（CRITICAL 重复检测） | `before_model_call` → `RepeatToolCallDetectorHook` `request_force_finish` | **软终止** | `e2a.complete`（带「无进展，已停」说明） |
| 溢出恢复熔断 | `on_model_exception` → `ContextOverflowRecoveryHook` `request_force_finish` | **软终止** | `e2a.complete`（带「持续溢出」说明） |
| `HookInterrupt`（无 approval_id） | `run()` / 工具段 `yield e2a.error` + return | **终止** | `e2a.error "execution interrupted"` |

一句话：**工具侧一切失败都是软的——错误变 `tool_result`，循环继续；模型侧失败是硬的——异常上抛，循环终止（瞬时/溢出先重试/恢复）**。重试与恢复不改变这个二分：成功就当没失败，仍失败才走各自的软/硬路径。

注意两种「终止形状」不同：**`e2a.error`**（未捕获异常，`status=failed`）是崩溃；**`e2a.complete`**（`force_finish`，`status=succeeded`）是带说明的优雅停止——死循环硬停与溢出熔断走后者，让用户看到一句解释而非一个裸异常。

---

## 工具失败处理

### 抛异常，不吞、不重试

工具执行唯一入口是 `ToolManager.execute`（`tools/manager.py`）。未知工具抛 `ToolError(kind="validation")`，已知工具的异常原样上抛——**不在 manager 层吞**。`execute` 契约是「成功返回 str，失败抛异常」。

工具层**不重试**（2026-08-27 移除）。理由：瞬时网络异常重试会重新执行有副作用的方法体（写文件、跑命令、发请求），Twinkle 没有幂等键保护，重试 = 重复副作用风险。这与 jiuwenswarm 一致——它的 `@rail` 提供重试机制但没有任何 rail 激活它，工具层同样不重试。模型层调用是幂等的（重发 chat completion 无副作用），故模型重试保留。

### `errors.py`：失败收口

对齐 openclaw 的契约——「失败时抛异常，而非把错误编码进 `content`」。`tools/errors.py` 是 tool-error content 的唯一收口：

- `ToolError(message, kind=...)`：工具内部失败时抛。`kind`（`validation`/`denied`/`failed`/`unavailable`）留在异常对象上，**不渲染进 content**——供 `AuditHook` 记 `outcome=kind`，也是未来 session-store `is_error` 元数据（B 计划）的零成本交接点。
- `format_tool_error(source)`：把任意失败渲染成统一的 `[tool error] …`，前缀复用 `TOOL_ERROR_PREFIX`（`observability/attributes.py`），使生产方与可观测消费方（instrumentor 的 `startswith` 检查）不漂移。`ToolError`→`[tool error] {msg}`；其他异常→`[tool error] {ExcType}: {msg}`；字符串→`[tool error] {str}`（denied-by-user 在 loop 里直接构造字符串走这条）。

`command_exec`、`file_tools`、`web_fetch`/`web_search` 现在全部用 `raise ToolError(...)` 表达失败（不再各自返回 `[ERROR]:` 串）。唯一仍走 `[ERROR]:` 的是权限 DENY——deny_message 由权限引擎在 `permissions/policy.py` 构造（`[ERROR]: command rejected for safety (…)` / `[ERROR]: denied by rule …`），经 `force_finish` 当 `tool_result` 回灌，未过 `format_tool_error` 漏斗。这是已知的不一致点。

### `ON_TOOL_EXCEPTION`：仅观测，不重试

`_tool_call` 用 `@hook(..., on_exception=ON_TOOL_EXCEPTION)` 装饰。工具抛异常时，`@hook` 装饰器的 except 块触发 `ON_TOOL_EXCEPTION`，然后**异常原样上抛**（`decorator.py` 已移除内置重试循环，`on_exception` 只观测、不重试）。两个观测者：

- `AuditHook`（priority 95）记一行 `outcome=ToolError.kind` 或 `error`（详见 §始终在线的工具执行审计）。
- `RepeatToolCallDetectorHook`（priority 88）把异常 outcome 记进滑动窗口参与循环检测。

异常随后冒泡到 agent loop 调用处的 `except Exception` → `format_tool_error(exc)` → `tool_result` → 续循环。不变式「工具异常不击穿 ReAct」由这个调用处兜底保证。

### 失败回灌

无论成功失败，结果都走同一条回灌路径：`_tool_call` 在 agent loop 的 `try` 里调用，`except HookInterrupt` 分流审批挂起/恢复/拒绝，`except Exception` 兜底 `format_tool_error(exc)`。结果作为 `tool` 消息（`tool_call_id` + content）append 进 session，外层 `_step` 循环 `continue` 重调模型。模型下一轮看到含错误描述的 `tool_result`，自行换参数、换工具或放弃回答——这是 ReAct「自我修正」的根基：**工具失败不是终点，是给模型的新信息**。

`asyncio.CancelledError` 是 `BaseException`，不会被 `except Exception` 误吞（与模型重试循环一致）。

### `command_exec` 非零退出码不算失败

`grep` 没匹配返回 1、`test` 失败返回非零，是命令表达「没找到 / 不成立」的常态。把它当错误会让模型误以为命令「坏了」。故非零退出码返回 JSON `{exit_code, stdout, stderr, …}`，退出码/stdout/stderr 全给模型自行解读——把「失败」的定义权交给语义而非进程退出码。后台进程退出非零同理（`Process exited with code N`）。其余 command_exec 失败（空命令 / 危险命令 / 越界 / 超时 / 执行异常）一律 `raise ToolError`。

### 子 agent 失败：同属软失败

`spawn_subagent` 是工具，失败也回灌父 agent，但封装方式不同——`SubagentExecutor` **永不抛异常**，软/硬/abort 超时与子的 `e2a.error` 帧全包成 `SubagentResult(success=False, error=…)`，再经 `_wrap()` 转字符串（含 stop hint「别再委派同一任务」）作为 `tool_result` 回灌，父循环**继续**。子结果截断到 `max_result_chars=8000` 防父上下文爆炸。子 loop 的 hook 列表与主 loop 同构（含 `RetryHook`/`AuditHook`/`RepeatToolCallDetectorHook`），故子 loop 内的模型瞬时异常也重试一次、工具异常也走 `format_tool_error` 回灌。子 agent 无步数上限，靠 CRITICAL 死循环检测 + `hard_timeout=3000s` 兜底。

---

## 模型失败处理

### `LLMClient`：不兜底，但有 read 超时

`LLMClient.stream` 是唯一模型调用入口，基于 `openai.AsyncOpenAI` 流式 completions。两个事实：**没有 try/except**（网络错、鉴权错、限流、上下文超限、空响应全直接抛）；**用 SDK `timeout`（默认 120s）做 per-chunk read 超时**——不用 `asyncio.wait_for` 包整条流（那会杀合法长响应），只在「无数据到达 N 秒」时触发 `APITimeoutError`（瞬时 → 重试），治「模型 hang 住」。所有异常原样传播到重试循环。

### 重试 + 恢复：两个 `on_model_exception` hook

模型失败在 `_run_react_loop` 的重试循环（`for retry_attempt in range(_MAX_HOOK_RETRIES + 1)`）里被 `except Exception` 捕获：设 `ctx.exception`、触发 `ON_MODEL_EXCEPTION`、再检查 `force_finish`（溢出熔断出口）与 `retry`（重试出口）。两个 hook 实现了 `on_model_exception`：

- `ContextOverflowRecoveryHook`（priority 60，先于 RetryHook）——**413 恢复**。判定上下文溢出错误（413 / `context_length_exceeded` / 关键词）后，按解析到的 token limit × `trigger_ratio`（解析不到则字典窗口兜底）激进压缩 `ctx.inputs.messages`，`request_retry(delay=0)` 让重试循环用更短消息重调。连失败超 `max_recovery_attempts=3` 则 `_circuit_break` → `request_force_finish(「持续溢出，请新会话」)` 软终止。`after_model_call` 成功后重置计数。这补上了曾经「上下文压缩重试未落地」的缺口。
- `RetryHook`（priority 50）——**瞬时异常重试一次**。`is_transient` 命中 `APIConnectionError`/`APITimeoutError`/`RateLimitError`/`InternalServerError`/`asyncio.TimeoutError`/`httpx.TransportError` 且 `retry_attempt < 1` → `request_retry(delay=1.0)`。413 非瞬时，RetryHook 不动它，交给上面的恢复 hook。重试次数由 `RetryHook.max_retries=1` 控制（`_MAX_HOOK_RETRIES=3` 只是上限护栏）。

非瞬时（鉴权 / 参数错）或重试耗尽 → `raise` 上抛。

### 一个 quirk：`ON_MODEL_EXCEPTION` 可能触发两次

重试循环的内层 except 先触发一次 `ON_MODEL_EXCEPTION`（可能 `request_retry` 被消费、重试）；若仍失败 `raise`，传到 `run()` 外层 except 又触发一次。外层无重试循环、`request` 不会被消费，故无害但冗余（外层可不触发，或 hook 按异常去重）。

### 模型失败 = 硬失败：终止并回退

异常从 `_run_react_loop` 上抛到 `run()` 再 raise → `server.run_task` 兜底：记日志、发一个 `e2a.error` 最终帧（`body.error = str(exc)`，原样透传）。**没有自动回退给用户重试的机制**——失败就是失败，等下一条请求。

---

## 失败回复机制

### 两种终止形状

| 形状 | `response_kind` | `status` | 触发 | body |
|---|---|---|---|---|
| 崩溃 | `e2a.error` | `failed` | 未捕获异常（模型硬失败 / interrupt / 解析错 / 在途冲突） | `{"error": str(exc)}` |
| 优雅停止 | `e2a.complete` | `succeeded` | `force_finish`（死循环 CRITICAL / 溢出熔断） | `{"result": {"content": 说明串}}` |

`E2AResponse` 没有独立 `error` 字段——错误文本放 `body["error"]`，靠 `response_kind="e2a.error"` + `status="failed"` + `is_final=True` 标识。`e2a.error` 产生点（全在 `agent.py` / `server.py`）：`run()` 捕 `HookInterrupt`（`execution interrupted`）、工具段 `HookInterrupt` 无 `approval_id`（`tool execution interrupted`）、`server.run_task` 捕 agent 异常（`str(exc)`）、envelope 解析失败（`str(exc)`）、同 session 已有请求在途（`a request is already in progress…`）。**步数耗尽已移除**——循环无界，不再有 `max_steps` 帧产生点。

---

## 循环防护

ReAct 循环无步数上限（`itertools.count()` 无界；`agent.max_steps` 与 `subagent.max_steps` 均已 DEPRECATED，代码忽略）。防护靠三层：

1. **`RepeatToolCallDetectorHook`——死循环 CRITICAL 硬停**。滑动窗口（30）+ 稳定哈希（tool name + 排序 args）检测重复 call。4 档严重度（LOW/MEDIUM/HIGH/CRITICAL），边沿触发：尾部相同 call+outcome ≥ `global_stop=30` → CRITICAL → `before_model_call` 里 `request_force_finish` 软终止（带「无进展，已停」说明）；`loop_block=20` → HIGH、`pingpong_warn=10` → MEDIUM（A-B-A-B 交替）注入纠偏 system 消息（每分钟限 `remediation_max_per_minute=5` 次）；`repeat_warn=10` → LOW 仅记日志。CRITICAL 绕过限流器（卡死必停）。这是对 jiuwenswarm `CircuitBreakerRail` 的精简对位——只做「重复无进展」这一种，不做其转圈 / 未知工具 / ping-pong 全套。
2. **`ContextOverflowRecoveryHook` 熔断**——溢出恢复连失败超 `max_recovery_attempts=3` 次 → `force_finish` 软终止，不再死调必然再 413 的 LLM。
3. **子 agent `hard_timeout=3000s`**——`asyncio.wait_for` 包整个 child run；`soft_timeout=600s` 无活动重置计时；`abort_timeout=30s` 收尾清理宽限。子 agent 无步数上限，靠 CRITICAL + hard_timeout 双兜底。

**已知缺口**：主循环无 wall-clock 超时、无 token 预算（jiuwenswarm / openclaw / Twinkle 三者都无）。一个持续「换新工具但不收敛」的 agent 不会被 CRITICAL 命中，会一直跑到用户中断——这是无步数上限换来的代价，待补主循环超时。

---

## Hook 在失败处理中的角色

Hook 机制见 [`hook-design.md`](./hook-design.md)，这里只讲和失败的关系。

| Hook | priority | 事件 | 角色 |
|---|---|---|---|
| `PermissionHook` | 100 | `before_tool_call` | DENY→`force_finish`（deny 串当 tool_result）；ASK→`HookInterrupt` 挂起审批。阻止失败工具执行 |
| `AuditHook` | 95 | `before/after_tool_call`、`on_tool_exception` | 始终在线审计每次工具调用 outcome=success/denied/error（详见下节） |
| `RepeatToolCallDetectorHook` | 88 | `before/after_tool_call`、`on_tool_exception`、`before_model_call` | 重复检测；CRITICAL `force_finish` 硬停 |
| `ContextOverflowRecoveryHook` | 60 | `on_model_exception`、`after_model_call` | 413 压缩重试 + 熔断 |
| `RetryHook` | 50 | `on_model_exception` | 模型瞬时异常重试一次（**仅模型**，工具重试已移除） |
| `LoggingHook` | 10 | 多个 | 观察者，纯通知 |

控制流信号：`RetryRequest(delay)`（hook 请求重试，`request_retry`/`consume_retry_request`）；`ForceFinishRequest(result)`（跳过本步 / 终止，`request_force_finish`/`consume_force_finish_request`，PermissionHook DENY / RepeatDetector CRITICAL / OverflowRecovery 熔断用它）；`HookInterrupt`（立即中断等人审批，PermissionHook ASK 用它）。`HookManager.execute` 容错（fail-soft）：单 hook 崩溃只记日志不阻断其他 hook，只有 `HookInterrupt` 传播。

`@hook` 装饰器**已无内置重试循环**：方法体失败触发 `on_exception`（仅观测）后异常直接 raise。模型路径的重试由 `_run_react_loop` 手写循环承担（async generator 与 `@hook` 不兼容），工具路径不重试。

---

## 始终在线的工具执行审计

`AuditHook`（`hooks/builtin/audit_hook.py`，priority 95）与 `permissions.enabled` 解耦——关权限也记。三回调覆盖全部 outcome：

- `before_tool_call`：若 `ctx.is_force_finish_requested()`（高 priority hook 即 PermissionHook DENY 已请求跳过）→ 记 `outcome=denied`。**读控制流状态而非某个 hook 的私有标记**，不与 PermissionHook 耦合。
- `after_tool_call`：成功 → `outcome=success`，result 取 `ctx.extra["_tool_result"]`（`@hook` 装饰器在方法体成功后存入）。
- `on_tool_exception`：异常 → `outcome=ToolError.kind`（`denied`/`validation`/`failed`/`unavailable`）或 `error`。

配置懒读 `settings.audit.tool_execution`（`enabled` / `file` / `max_arg_chars=2000` / `max_result_chars=2000`），构造处 `AuditHook()` 无参即可。fail-soft：写失败只告警。不脱敏，只截断（审计文件在可信本地 workspace）。主 / 子 / team 三 agent 统一装（`server.main()`、`SubagentExecutor._hook_list`、`TeamManager` 各注册一次）。

---

## 配置 / 超时 / 上限

配置真源：`twinkle/resources/config.yaml` + 校验 `config/schema.py`。仅列与失败处理直接相关的项：

| 配置项 | 默认 | 作用 |
|---|---|---|
| `llm.timeout` | 120.0 | LLM per-chunk read 超时；hang→`APITimeoutError`（瞬时，重试） |
| `agent.max_steps` | 1000 | **DEPRECATED**，代码忽略（循环无界） |
| `context_compression.token_threshold` | 0（动态） | 预防性压缩阈值（主动压缩，与溢出恢复互补） |
| `overflow_recovery.max_recovery_attempts` | 3 | 413 连续恢复上限，超则熔断 |
| `overflow_recovery.aggressive_keep_recent` | 3 | 溢出压缩时保留最近 N 对 |
| `overflow_recovery.context_window_limit_tokens` | 0 | 0=字典/128k 兜底；>0 手动覆盖窗口 |
| `repeat_tool_detection.*` | 30/10/10/20/30/5 | history / repeat_warn / pingpong_warn / loop_block / global_stop / remediation_per_min |
| `audit.tool_execution.enabled` | true | 始终在线审计开关（与 permissions.enabled 解耦） |
| `audit.tool_execution.max_{arg,result}_chars` | 2000 / 2000 | 审计行截断 |
| `permissions.enabled` | false | 权限总开关（关=全 ALLOW 无审批；command_exec 仍走 builtin_rules） |
| `subagent.hard_timeout` | 3000.0 | 子 agent 绝对超时（对齐 jiuwenswarm 3000） |
| `subagent.soft_timeout` | 600.0 | 子 agent 无活动超时（对齐 jiuwenswarm 600） |
| `subagent.abort_timeout` | 30.0 | 取消卡死子的收尾窗口 |
| `subagent.max_steps` | 50 | **DEPRECATED**，代码忽略（子循环无界） |
| `subagent.max_result_chars` | 8000 | 子结果截断 |

硬编码（不进 config）：`_MAX_HOOK_RETRIES=3`（`agent.py`，模型重试上限护栏；`RetryHook` 实际只重试 1 次）；`RetryHook.max_retries=1` / `delay=1.0s`（构造参数可调）；`command_exec` 超时 300（clamp [1,3600]）、`max_output_chars=20000`；`file_tools._WRITE_MAX_BYTES=5MiB`；`web_fetch` / `web_search` httpx 超时 15–30s；AgentClient ping 30 / 300。

---

## 对照参考实现

聚焦失败这条线（回调框架大对比见 [`hook-design.md`](./hook-design.md)）：

| | jiuwenswarm | openclaw | Twinkle |
|---|---|---|---|
| 工具失败回灌形态 | 结构化 `ToolMessage` + `AbilityExecutionError`（带 tool_message） | 契约禁止错误编码进 content，用 `isError` 字段 + 集中 `createErrorToolResult` | 裸字符串但已收口：`ToolError` + `format_tool_error` → 统一 `[tool error]` 前缀（对齐 openclaw「抛异常不编码进 content」） |
| 工具层重试 | 不重试（@rail 有机制、无 rail 激活） | — | **不重试**（2026-08-27 移除，对齐 jiuwenswarm；无幂等保护，重试有重复副作用风险） |
| 模型错误归一 | `MODEL_CALL_FAILED`(181001) → `ModelError(recoverable)` | — | 裸 `str(exc)`，不归一；但 413 由 `ContextOverflowRecoveryHook` 特化恢复 |
| 模型重试 | SDK `max_retries=3`；agent 层 `ModelBackupRail` 未注册 | — | `LLMClient(timeout=120)` + `RetryHook`（瞬时重试一次，`main()` 传入） |
| 模型失败回退用户 | piggyback `answer` 事件（`result_type=error`），走正常 content 通道必达 | — | 专有 `e2a.error` 帧 + Gateway 专门分支，`[error] …` 送达 |
| 循环卡死熔断 | `CircuitBreakerRail`（无进展 / 未知工具 / ping-pong / 重复全套 force_finish） | post-compaction 守卫 + idle-breaker + 分层流级超时 | `RepeatToolCallDetectorHook` CRITICAL 硬停（只做重复无进展一种）+ 溢出熔断 + 子 hard_timeout；主循环无 wall-clock / 无 token 预算（三者都无） |
| 工具结果给客户端 | `_infer_tool_result_error` 推断 is_error / success / status | `isError` 字段 + `details.status` 闭集 | 不标（tool_result 只回模型）；`ToolError.kind` 留在异常上供审计，不进 content |
| 特殊错误文案 | 图片不支持友好中文；断路器 / command_exec i18n 文案表 | — | 无 i18n，硬编码短句 + `str(exc)` |
| `command_exec` 非零退出码 | 不算失败，返回 JSON，`ToolResultErrorDetector` 推断 | — | 不算失败，返回 JSON（同） |
| 工具中断（人工审批） | `ToolInterruptException` → `ask_user_question` + 权限 rail | — | `HookInterrupt` → `e2a.ask` + `PermissionHook`（语义同名异） |

三者对失败主链判断一致：工具失败回灌续循环、模型失败终止回退，都「瞬时可重试」。差异在结构化程度与熔断：jiuwenswarm 最结构化、有全套熔断；openclaw 契约最规整；Twinkle 精简——失败收口已对齐 openclaw（抛异常不编码进 content），工具层重试已对齐 jiuwenswarm（都不重试），熔断只做重复无进展一种。

---

## 设计决策回顾

### 为什么工具软失败、模型硬失败

工具失败是局部、可恢复的——换参数、换工具、放弃回答，模型看到错误就能调整。模型失败是全局、可能死循环的——坏上下文 / 鉴权错重试也是同样的错。二分把「可自愈」留给循环、「不可自愈」交给终止。瞬时 / 溢出恢复不改变二分：成功就当没失败，仍失败才走各自软 / 硬路径。

### 为什么工具层不重试、模型层重试（2026-08-27 反转）

曾经 `@hook` 装饰器对工具也有内置重试循环（瞬时异常重试一次）。**移除**原因：工具方法体有副作用（写文件、跑命令、发请求），Twinkle 无幂等键，重试 = 重复副作用风险；而模型调用幂等（重发 chat completion 无副作用），重试安全。jiuwenswarm 工具层同样不重试（`@rail` 有机制但无 rail 激活），印证这是正确取舍。代价：网络抖动导致的工具失败不再自动重试，靠模型看到 `[tool error]` 后自行决定重试——更安全（不会重复扣款 / 重复写）但少了一次自动兜底。MCP 传输层 `reconnect_attempts` 是 ws 连接重连、不重新执行方法，不在此列。

### 为什么用 `errors.py` 收口而非裸串 / 结构化对象

对齐 openclaw「失败抛异常、不编码进 content」。OpenAI tool 协议的 `tool` 消息 content 本就是字符串，结构化对象还得让模型学会读 `isError` 字段——不如直接喂人类可读错误描述。但裸串会前缀漂移（曾经 `[error]` / `[tool error]` / `[ERROR]:` 三种），故用一个收口函数 `format_tool_error` + 共享 `TOOL_ERROR_PREFIX`，生产方与可观测消费方不漂移。`ToolError.kind` 留在异常上（不进 content）给审计用，是「不渲染进 content」与「留结构化标志给机器」的折中。唯一漏斗外的是权限 DENY 的 `[ERROR]:`（引擎构造、走 force_finish）——已知待收口点。

### 为什么 `e2a.error` 用 `body["error"]` 而非独立字段

`E2AResponse` 用一个 `body: dict` 承载所有 kind 载荷（chunk 放 result.content、error 放 error、ask 放 approval_id），少一种序列化形态。代价是 Gateway 必须按 kind 取不同 key——曾漏给 `e2a.error` 写专门分支，导致取错 key 拿空串、错误文本丢失。现以补专门分支（`"[error] " + body.error`）解决。jiuwenswarm 复用 `answer` 事件虽丑但文本走正常 content 路径必达、反而没这坑——「专有类型更干净」与「复用通道更稳健」的权衡，Twinkle 选专有类型并补齐翻译。

### 为什么死循环用 `force_finish`（`e2a.complete`）而非 `e2a.error`

死循环不是崩溃，是 agent 陷入无进展——用 `force_finish` 产 `e2a.complete`（succeeded）+ 一句「已停，请重述任务」说明，比一个裸 `e2a.error` 更友好：用户看到的是正常的结束帧带解释，而非失败。同理溢出熔断。崩溃（未捕获异常）才走 `e2a.error`。把「主动止损」与「意外崩溃」分开回复。

### 为什么 `command_exec` 非零退出码不算失败

`grep` 没匹配返回 1、`test` 失败返回非零，是命令正常表达「没找到 / 不成立」。当错误会让模型误以为命令「坏了」而放弃。退出码 / stdout / stderr 全给模型自行解读——把「失败」定义权交给语义而非进程退出码。

### 取舍：无主循环 wall-clock 超时 / 无 token 预算

步数上限已移除（换无界循环 + CRITICAL 重复检测），但主循环仍无 wall-clock 超时与 token 预算——一个持续换新工具却不收敛的 agent 不会被 CRITICAL 命中。jiuwenswarm / openclaw 也都无整任务 token 预算。这是已知待补点（加主循环 wall-clock 或 token 预算）。退避固定 1s、重试次数固定 1 次（未进 config，构造参数可调）是更次要的取舍。
