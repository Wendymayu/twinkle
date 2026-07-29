# 失败处理设计与实现

## 一句话概括

Twinkle 把失败分成两类：**工具失败是软失败**——异常上抛到 `@hook` 触发 `ON_TOOL_EXCEPTION`，瞬时异常由 `RetryHook` 重试一次，仍失败或非瞬时则被 `agent_loop` 调用处兜底成 `[tool error]` 字符串回灌模型，ReAct 循环继续；**模型失败是硬失败**——异常上抛终止本次 agent loop，瞬时异常（网络/超时/限流/5xx）由 `RetryHook` 重试一次，仍失败则 `server.py` 兜底发 `e2a.error` 帧。`RetryHook` 由 `main()` 传入（无外部依赖，与 `PermissionHook`/`SkillHook` 等同组；`build_agent_loop` 只自动装配 `SubagentContextHook`），子 agent 在 `_hook_list` 默认列表里也装。失败回复统一走 `E2AResponse` 的 `e2a.error` 帧，Gateway 给它专门分支，错误文本以 `[error] …` 送达浏览器。

---

## 为什么需要失败处理

ReAct 循环每一步都可能失败：工具抛异常、模型网络错、权限拒绝、命令超时、子 agent 卡死。一种处理方式打不了天下：

1. **工具失败若直接终止**——agent 一次 `command_exec` 报错就死，无法自我修正（换参数、换工具、换思路）。模型其实很擅长「看到错误 → 调整 → 重试」，前提是错误信息要喂回给它。
2. **模型失败若不终止**——坏上下文 / 死循环会反复触发同一异常，烧 token 到天荒地老。
3. **失败要回两个地方**——回灌给模型（让它换路）和回给用户（让它知情），两者受众不同、走不同通道。
4. **崩溃要兜底**——任何一层抛未捕获异常都不该让进程或连接死掉，要降级成一条可读的失败回复。

所以 Twinkle 的失败处理有三条底线：**不崩循环**（工具失败不炸 ReAct）、**可恢复**（失败信息回灌让模型自愈；瞬时异常还会自动重试一次）、**可观测**（失败以帧/事件形式送达，事后可查）。

---

## 设计来源

对照 jiuwenswarm 的失败处理链路，Twinkle 做了**同构但裁剪**的实现。下表只列失败这条线上的概念映射，回调框架本身的大对比见 [`hook-design.md`](./hook-design.md)。

| jiuwenswarm | Twinkle | 说明 |
|---|---|---|
| `AbilityManager.execute` + `asyncio.gather(return_exceptions=True)` → `ToolMessage("Ability execution error: …")` 回灌 | `ToolManager.execute` 抛异常 → `@hook` 触发 `ON_TOOL_EXCEPTION` → `RetryHook` 重试瞬时 → `agent_loop` 兜底 `[tool error]` 串回灌 | 同为「工具软失败回灌模型」；jiuwen 用结构化 `ToolMessage` + `AbilityExecutionError`（带 `tool_message` 字段），Twinkle 用裸字符串 |
| `MODEL_CALL_FAILED` 状态码 + `ModelError(recoverable=True)` | 裸 `str(exc)` | jiuwen 把网络 / 鉴权 / 限流 / 上下文超限全归一成一个错误码，Twinkle 原样透传 |
| `answer` 事件 + `result_type="error"` 回退给用户 | `e2a.error` 帧回退给用户 | jiuwen 复用回答通道（文本必经正常 content 路径送达）；Twinkle 用专有 `response_kind` + Gateway 专门分支保文本送达 |
| `AsyncOpenAI(max_retries=3, timeout=60)` 显式配置 + `ModelBackupRail`（存在但未注册） | `LLMClient(timeout=120)` + `RetryHook`（瞬时异常重试一次，`main()` 传入） | jiuwen 显式喂 SDK 重试参数、退避靠 SDK，agent 层故障转移 rail 未启用；Twinkle agent 层有内置瞬时重试 |
| `CircuitBreakerRail`（转圈 / 重复失败自动止损） | 仅 `max_steps` 硬上限 | jiuwen 有单次 invoke 内的循环卡死熔断，Twinkle 砍了 |
| `_infer_tool_result_error`（给客户端标 `is_error`/`success`/`status`） | 无（`tool_result` 只回模型） | jiuwen 给客户端结构化错误标志，Twinkle 不标 |

砍掉熔断 / 错误码归一 / 工具结果错误推断的原因：Twinkle 是学习型重实现，优先把「工具软失败 → 回灌 → 续循环」与「模型硬失败 → 终止 → 回退用户」两条主链跑通。这些能力在没有规模化、多模型故障转移、客户端结构化错误展示场景前是纯成本。

---

## 核心二分：软失败 vs 硬失败

这是全文的总纲。一条失败落到哪个分支，决定了循环是否继续、回复什么：

| 失败类型 | 走哪个分支 | 循环是否继续 | 回复什么 |
|---|---|---|---|
| 工具抛瞬时异常（`httpx`/超时） | `ToolManager.execute` 抛 → `@hook` `ON_TOOL_EXCEPTION` → `RetryHook` 重试一次 | **重试 1 次** | 成功则正常 `tool_result`；仍失败→走下一行 |
| 工具抛非瞬时异常 | `@hook` `ON_TOOL_EXCEPTION` → `RetryHook` 不重试 → `agent_loop` 兜底 | **继续**（`[tool error]` 回灌，`_reask=True`） | `[tool error] …` 回灌模型 |
| 工具自处理错误（`command_exec`/`file_tools` 的 `[ERROR]:`） | 工具内部返回字符串，不抛异常 | **继续** | 错误串回灌模型 |
| 权限 DENY | `PermissionHook` → `request_force_finish(deny_message)` | **继续**（跳过执行，deny 串当 `tool_result`） | deny 串回灌模型 |
| 权限 ASK 被用户拒绝 | `result = "[tool denied by user: …]"` | **继续** | 拒绝串回灌模型 |
| 子 agent 失败 / 超时 | `SubagentResult(success=False)` → `_wrap` 转串 | **继续** | 失败串回灌父 agent |
| 模型调用失败（瞬时） | `except Exception` → `ON_MODEL_EXCEPTION` → `RetryHook` `request_retry` → `sleep+continue` | **重试 1 次** | 成功则正常流式；仍失败→走下一行 |
| 模型调用失败（非瞬时） | `ON_MODEL_EXCEPTION` → `RetryHook` 不重试 → `raise` | **终止** | server 兜底 `e2a.error str(exc)` |
| 步数耗尽 `max_steps` | 循环外 `yield e2a.error` | **终止** | `e2a.error "agent loop exceeded max_steps=N"` |
| `HookInterrupt` 无 `approval_id` | `run_stream` / 工具段 `yield e2a.error` + return | **终止** | `e2a.error "execution interrupted"` / `"tool execution interrupted"` |

一句话：**工具侧一切失败都是软的——错误变 `tool_result`，循环继续（瞬时异常还会先重试一次）；模型侧失败是硬的——异常上抛，循环终止（瞬时异常先重试一次）**。

---

## 工具调用失败处理

### 单一拦截层：ToolManager.execute（抛异常，不吞）

工具执行的唯一入口是 `ToolManager.execute`，被 `AgentLoop._hooked_tool_call` 调用：

```python
# tools/manager.py:44-51
async def execute(self, name: str, args: dict) -> str:
    t = self._tools.get(name)
    if t is None:
        return f"[error] unknown tool: {name}"   # not-found 是结果不是异常
    # 工具异常抛出（不吞），交给 @hook 触发 ON_TOOL_EXCEPTION + RetryHook 重试。
    # 兜底成 [tool error] 串的职责上移到 agent_loop 调用处，保循环不崩。
    return await t.invoke(args)
```

`LocalFunction.invoke` 只是 `await self.func(**args)`，所以工具函数抛的任何异常都直接冒到 `@hook` 装饰器的 except 块。**`execute` 不再吞异常**——这曾使 `ON_TOOL_EXCEPTION` 成为死代码（异常在 manager 层就被吃成串，永不冒泡）。现在异常上抛，`ON_TOOL_EXCEPTION` 真正触发，`RetryHook` 得以介入重试。不变式「工具异常不击穿 ReAct 循环」由 `agent_loop` 调用处的 `except Exception` 兜底保证（见 §失败回灌）。

### 结果形态：纯字符串，三种不统一的前缀

工具失败的结果**不是结构化对象、没有 `is_error` 标志**，就是一段字符串。但前缀不统一：

| 来源 | 前缀 | 例子 |
|---|---|---|
| `agent_loop` 兜底（未知工具） | `[error]` | `[error] unknown tool: foo` |
| `agent_loop` 兜底（工具抛异常，重试耗尽/非瞬时） | `[tool error]` | `[tool error] ValueError: invalid args` |
| `command_exec` / `file_tools` 自处理 | `[ERROR]:` | `[ERROR]: command timed out after 300s.` |
| `web_search` 自处理（空查询） | `[error]` | `[error] empty query` |
| 空结果占位（非错误） | 无前缀 | `(empty page)` / `(no results)` |

**一个细节**：`web_fetch` / `web_search` 对 HTTP 非 2xx 调 `resp.raise_for_status()`（`web_fetch.py:67` / `web_search.py:85`），**不自己兜**——`httpx.HTTPStatusError` / `RequestError` 冒泡到 `@hook`。但 `HTTPStatusError` 不是瞬时异常（不在 `RetryHook` 的重试集里），所以不重试，直接由 `agent_loop` 兜底成 `[tool error] HTTPStatusError: …`。而 `httpx.TransportError`（连接/读超时）是瞬时的，会重试一次。`command_exec` / `file_tools` 则把所有失败自己包成 `[ERROR]:` 字符串（不抛），不走重试。于是同一个 agent 里，工具失败串长得不一样，模型得自行适应。这是个已知的不一致。

### 工具重试：瞬时异常重试一次

`_hooked_tool_call` 用 `@hook(..., on_exception=HookEvent.ON_TOOL_EXCEPTION)` 装饰（`agent_loop.py:500-501`）。工具抛异常时，`@hook` 的 except 块（`decorator.py:71-82`）触发 `ON_TOOL_EXCEPTION`，`RetryHook.on_tool_exception` 判断：

- 瞬时异常（`httpx.TransportError` / `asyncio.TimeoutError`）且 `retry_attempt < 1` → `ctx.request_retry(delay=1.0)`，`@hook` 睡 1s 后重试方法体（再调一次 `execute`）。
- 非瞬时或二次失败 → 不请求重试，`@hook` 重新抛出，由 `agent_loop` 调用处兜底。

即工具瞬时异常重试一次（共 2 次尝试），仍失败则 `[tool error]` 回灌模型。

### 失败回灌：错误串 → tool_result → 续循环

无论成功还是失败，结果都走同一条回灌路径。`agent_loop` 调用 `_hooked_tool_call` 时兜底：

```python
# agent_loop.py:302-331（节选）
try:
    result = await self._hooked_tool_call(ctx)   # @hook 内已做重试
except HookInterrupt as hi:
    ...  # 审批挂起/恢复或拒绝
except Exception as exc:
    # 重试耗尽 / 非瞬时：兜底成串，循环继续，不崩
    result = f"[tool error] {type(exc).__name__}: {exc}"
# …
await self._session_store.append(
    session_id, {"role": "tool", "tool_call_id": tc["id"], "content": result},
    request_id=envelope.request_id, event_type="chat.tool_result",
)
_reask = True   # 外层 _step 循环 continue 重调模型
```

`asyncio.CancelledError` 是 `BaseException`，不会被 `except Exception` 误吞（与模型重试循环一致）。模型下一轮看到这条 `tool_result`（含错误描述），自行决定下一步——换参数重试、换工具、放弃并回答。这是 ReAct「自我修正」能力的根基：**工具失败不是终点，是给模型的新信息**。

### 权限三档：DENY / ASK 也是软失败

权限拦截发生在工具执行**之前**（`PermissionHook.before_tool_call`），三种决策里两种是软失败（详见 [`permission-approval-design.md`](./permission-approval-design.md)）：

- **DENY** → `ctx.request_force_finish(deny_message)`，`@hook` 装饰器跳过方法体直接返回，`deny_message`（如 `[ERROR]: command rejected for safety ({reason}).`）直接当 `tool_result` 回灌——循环继续。
- **ASK 被拒** → 用户在审批卡点选「拒绝」，`result = f"[tool denied by user: {tool}] {reason}"`（`agent_loop.py:330-331`）回灌——循环继续。
- **ASK 放行** → 重新执行 `_hooked_tool_call`（同样有 `except Exception` 兜底），走正常工具路径。

权限失败从不终止循环，只是把一条「拒绝」信息喂给模型，让它换个做法。

### command_exec 的失败矩阵

`command_exec` 把所有失败自己包成字符串返回，对上层来说「永远成功」（不抛、不重试）：

| 失败场景 | 返回 | 行号 |
|---|---|---|
| 空命令 | `[ERROR]: command cannot be empty.` | `command_exec.py:124` |
| 危险命令（blocklist） | `[ERROR]: command rejected for safety ({reason}).` | `command_exec.py:128` |
| workdir 越界 | `[ERROR]: workdir is outside the project workspace.` | `command_exec.py:133` |
| 超时 | `[ERROR]: command timed out after {N}s.` | `command_exec.py:172` |
| 其他执行异常 | `[ERROR]: command execution failed: {exc}` | `command_exec.py:174` |
| 后台启动失败 | `[ERROR]: command failed to start: {exc}` / `background command failed: {err}` | `command_exec.py:153-155` |
| **非零退出码** | **不算错误**——返回 JSON `{exit_code, stdout, stderr, …}`，模型自行判断 | `command_exec.py:176-186` |

「非零退出码不算失败」是刻意的：命令跑完返回非零是常态（`grep` 没匹配、`test` 失败），把它当错误会误导模型。退出码、stdout、stderr 都给模型，让它自己解读。

超时上限 `timeout_seconds` 默认 300，clamp 到 `[1, 3600]`（`command_exec.py:139`）；`max_output_chars` 默认 20000。

### 子 agent 失败：同属软失败

`spawn_subagent` 是个工具，它的失败也回灌父 agent，但封装方式不同——`SubagentExecutor` **永不抛异常**，一律包成 `SubagentResult`：

```python
# tools/builtin/subagent/executor.py:144-172
async def execute_subagent(self, task, parent_session_id, parent_request_id) -> SubagentResult:
    child_task = asyncio.create_task(self._drive_child(loop, envelope))
    try:
        final = await asyncio.wait_for(child_task, timeout=self._config.hard_timeout)
        return SubagentResult(success=True, result=final)
    except SoftTimeoutError as exc:
        return SubagentResult(success=False, error=f"soft timeout: {exc}")
    except asyncio.TimeoutError:
        return SubagentResult(success=False, error=f"hard timeout after {N}s")
    except Exception as exc:
        return SubagentResult(success=False, error=f"{type(exc).__name__}: {exc}")
```

`_drive_child` 内部把子的 `e2a.error` 帧转成 `RuntimeError`、子的异常转 `raise frame`、无活动超时抛 `SoftTimeoutError`（`executor.py:120-131`），但这些异常**在 `execute_subagent` 这层全被吃掉**，包成 `SubagentResult(success=False)`。再经 `tools.py:_wrap()` 转字符串：

```python
# tools/builtin/subagent/tools.py:27-30
def _wrap(result: SubagentResult) -> str:
    if result.success:
        return (result.result or "") + _SUBAGENT_STOP_HINT
    return (result.error or "subagent failed") + _SUBAGENT_STOP_HINT
```

这个字符串（含 stop hint「别再委派同一任务」）作为 `tool_result` 回灌父 agent，父循环**继续**。子结果还会被截断到 `max_result_chars=8000`（`executor.py:133-134`），防父上下文爆炸。子 agent 的 loop 也装了 `RetryHook`（默认 `_hook_list`），所以子 loop 内的模型/工具瞬时异常也会重试一次。语义和普通工具软失败完全一致。

### ON_TOOL_EXCEPTION：现已激活

`_hooked_tool_call` 用 `@hook(..., on_exception=HookEvent.ON_TOOL_EXCEPTION)` 装饰（`agent_loop.py:500-501`）。曾经这是死代码——因为 `ToolManager.execute` 在内部 try/except 兜底了所有异常，方法体永不抛，`@hook` 的 except 块永不触发。**现已修复**：`execute` 不再吞异常，工具异常上抛到 `@hook` 的 except，`ON_TOOL_EXCEPTION` 对所有工具异常都会触发；`RetryHook` 仅对瞬时异常请求重试一次，非瞬时则放行让 `agent_loop` 兜底。

---

## 模型调用失败处理

### LLMClient：不兜底，但有超时

`LLMClient.stream` 是唯一的模型调用入口，基于 `openai.AsyncOpenAI` 流式 chat completions：

```python
# llm_client.py:32-46
def __init__(self, base_url, api_key, model, client=None, timeout=None):
    self._model = model
    # timeout -> AsyncOpenAI read timeout：模型 hang 住（无 chunk 到达 N 秒）
    # 抛 APITimeoutError（瞬时 -> RetryHook 重试），而非永久阻塞。None = SDK 默认。
    self._client = client or AsyncOpenAI(
        base_url=base_url, api_key=api_key, timeout=timeout)
# …
async def stream(self, messages, tools) -> AsyncIterator[TextDelta | Finish]:
    stream = await self._client.chat.completions.create(model=…, messages=…, stream=True, …)
    async for chunk in stream:
        …  # 累积 text + tool_calls，yield TextDelta / Finish
```

两个关键事实：**没有 try/except**（网络错、鉴权错、限流、上下文超限、空响应全直接抛），**用 SDK `timeout`（默认 120s，见配置）做 per-chunk read timeout**——不用 `asyncio.wait_for` 包整条流（那会杀掉合法的长响应），read timeout 只在「无数据到达 N 秒」时触发 `APITimeoutError`，正好治「模型 hang 住」。所有异常原样传播到 `agent_loop._inner_run_stream` 的重试循环。

### 重试循环：瞬时异常重试一次（RetryHook）

```python
# agent_loop.py:126, 264-384
_MAX_HOOK_RETRIES = 3

for retry_attempt in range(_MAX_HOOK_RETRIES + 1):   # 共 4 次尝试
    ctx.retry_attempt = retry_attempt
    ctx.exception = None
    try:
        async for ev in self._llm.stream(messages=ctx.inputs.messages, tools=ctx.inputs.tools):
            …  # TextDelta → e2a.chunk；Finish → tool_calls 或最终回答
    except asyncio.CancelledError:
        raise                    # 绝不干扰取消
    except HookInterrupt:
        raise                    # 中断立即传播
    except Exception as exc:
        ctx.exception = exc
        await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
        retry_req = ctx.consume_retry_request()
        if retry_req is not None and retry_attempt < _MAX_HOOK_RETRIES:
            if retry_req.delay > 0:
                await asyncio.sleep(retry_req.delay)   # 退避（此前模型路径漏了这步）
            continue             # hook 请求重试
        raise                    # 无重试或超限 → 上抛
```

`RetryHook`（`hooks/builtin/retry_hook.py`，`main()` 传入，无外部依赖）实现 `on_model_exception`：瞬时异常（`openai.APIConnectionError`/`APITimeoutError`/`RateLimitError`/`InternalServerError` + `asyncio.TimeoutError` + `httpx.TransportError`）且 `retry_attempt < 1` → `ctx.request_retry(delay=1.0)`，重试循环睡 1s 后重试。所以**默认行为：模型瞬时异常重试一次，非瞬时（鉴权/参数错/上下文超限）直接上抛终止**。重试次数由 `RetryHook` 的 `max_retries=1` 控制（不是 `_MAX_HOOK_RETRIES=3`，那只是上限护栏）。

### 一个 quirk：ON_MODEL_EXCEPTION 可能触发两次

模型失败上抛时，`_inner_run_stream` 的内层 except 先触发一次 `ON_MODEL_EXCEPTION`（`:378`）然后 `raise`；异常传到 `run_stream` 的外层 except，**又触发一次** `ON_MODEL_EXCEPTION`（`:188`）再 `raise`：

```python
# agent_loop.py:186-189  (run_stream 外层)
except Exception as exc:
    ctx.exception = exc
    await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
    raise
```

`RetryHook` 实现了 `on_model_exception`，所以会触发两次。内层那次（`retry_attempt=0`）可能 `request_retry` 并被重试循环消费；外层那次 `retry_attempt` 已 `>=1`，`RetryHook` 不再请求重试，且 `run_stream` 外层无重试循环、`request` 不会被消费——无害但可优化（外层可不再触发，或 `RetryHook` 按异常去重）。

### 预期用途：上下文压缩重试（仍未落地）

`agent_loop.py:268-270` 的注释说明了 `ON_MODEL_EXCEPTION` 的另一设计意图：**上下文超限恢复**——hook 检测到 token 溢出异常，用压缩后的消息替换 `ctx.inputs.messages`，调 `ctx.request_retry()`，重试时用更短的消息。重试循环注释明确「用 `ctx.inputs.messages` 而非本地 `msgs`，让压缩 hook 的替换在重试时生效」。`RetryHook` 只覆盖了瞬时重试这一条；上下文压缩重试目前没有实现——上下文压缩走的是另一条**主动**路径（每步调模型前 `compress_messages`，`agent_loop.py:231-237`），不是失败后的被动重试。

### 流式断流与防御性收尾

`async for chunk in stream` 若中途网络断开，抛异常走同一条 `except Exception` 路径（瞬时 → 重试一次）。另外有一条优雅降级（`agent_loop.py:369-371`）：流正常结束但没产出 `Finish` 事件（不该发生），触发 `AFTER_MODEL_CALL` 并 break 到下一步，不让循环卡死。

### 模型失败 = 硬失败：终止并回退

异常从 `_inner_run_stream` 上抛到 `run_stream`（`:186-189` 再 raise）→ 到 `server.py` 的 `run_task`：

```python
# server.py:109-117
async def run_task(envelope: E2AEnvelope) -> None:
    try:
        async for frame in loop.run_stream(envelope):
            await send(frame)
    except Exception as exc:
        log.exception("agent loop failed for %s: %s", envelope.request_id, exc)
        await send(E2AResponse(
            request_id=envelope.request_id, is_final=True, status="failed",
            response_kind="e2a.error", body={"error": str(exc)}))
```

模型调用失败（瞬时重试仍失败，或非瞬时）→ 本次 agent loop 终止，发一个 `e2a.error` 最终帧，错误以 `str(exc)` 原样透传。**没有回退给用户让它重试的机制**——失败就是失败，等下一条请求。

---

## 失败回复机制

### E2AResponse 的错误帧

```python
# e2a/models.py:36-47
class E2AResponse(BaseModel):
    protocol_version: str = E2A_PROTOCOL_VERSION
    request_id: str
    sequence: int = 0
    is_final: bool = False
    status: str = "in_progress"   # in_progress | succeeded | failed
    response_kind: str = "e2a.chunk"
        # e2a.chunk | e2a.complete | e2a.error | e2a.todo_update | e2a.result | e2a.ask
    body: dict[str, Any] = Field(default_factory=dict)
    is_stream: bool = True
```

**没有独立的 `error` 字段**——错误文本放在 `body["error"]`，靠 `response_kind="e2a.error"` + `status="failed"` + `is_final=True` 标识这是一条失败回复。

### e2a.error 产生点

| 位置 | 触发条件 | body |
|---|---|---|
| `agent_loop.py:178-185` | `run_stream` 捕获 `HookInterrupt` | `{"error": "execution interrupted"}` |
| `agent_loop.py:306-310` | 工具段 `HookInterrupt` 无 `approval_id` | `{"error": "tool execution interrupted"}` |
| `agent_loop.py:389-396` | 步数耗尽 | `{"error": f"agent loop exceeded max_steps={N}"}` |
| `server.py:115-117` | `run_task` 捕获 agent loop 异常 | `{"error": str(exc)}` |
| `server.py:124-125` | envelope JSON 解析失败 | `{"error": str(exc)}` |
| `server.py:137-140` | 同 session 已有请求在进行 | `{"error": "a request is already in progress for this session"}` |

### Gateway 翻译：错误文本送达

`MessageHandler._process_stream` 按 `response_kind` 分发，给 `e2a.error` 专门分支：

```python
# gateway/message_handler.py:42-97（节选）
async for resp in self._agent_client.send_request_stream(envelope):
    if resp.response_kind == "e2a.todo_update":   …  # → TODO_UPDATE
    elif resp.response_kind == "e2a.ask":         …  # → APPROVAL_ASK
    elif resp.response_kind == "e2a.result":       …  # → RESULT，payload=body
    elif resp.response_kind == "e2a.error":            # ← 专门分支，保文本送达
        out = Message(…, event_type=EventType.CHAT_FINAL,
                      content=f"[error] {resp.body.get('error', '')}",
                      payload=dict(resp.body))
    else:                                              # chunk/complete
        content = (resp.body.get("result") or {}).get("content", "")
        out = Message(…, event_type=CHAT_FINAL if is_final else CHAT_DELTA, content=content)
    await self.enqueue_outbound(out)
# except Exception as exc:  → CHAT_FINAL, content=f"[error] {exc}"（ws 断连）
```

`e2a.error` 帧走专门分支，`content = "[error] " + body.error`，作为 `CHAT_FINAL` 送达浏览器。曾经这里没有专门分支，`e2a.error` 落到 `else` 按 `body.result.content` 取内容（`e2a.error` 的 body 是 `{"error":…}` 无 `result` 键）→ content 为空 → 浏览器收到空 `chat.final`，错误文本丢失。现已修复。

三条错误回复路径在 Gateway 的命运：

| 错误来源 | Gateway 翻译 | 浏览器收到 |
|---|---|---|
| AgentServer 的 `e2a.error` 帧 | `e2a.error` 分支 → `CHAT_FINAL`，`content="[error] …"` | chat.final 带 `[error] …`（文本保留） |
| `AgentClient` 流本身抛异常（ws 断连 / AgentServer 崩溃） | except 分支 → `CHAT_FINAL`，`content=f"[error] {exc}"` | chat.final 带 `[error] …`（文本保留） |
| session RPC 的 `e2a.result`(status=failed) | `e2a.result` 分支 → `RESULT`，`payload=body` | result 事件，payload 含 error（保留） |

### 无面向用户的文案模板

所有失败回复都是直接透传底层文本：

- `str(exc)` —— `server.py:117`、`message_handler.py:95`
- 硬编码短句 —— `"execution interrupted"`、`"tool execution interrupted"`、`f"agent loop exceeded max_steps={N}"`
- 工具错误串 —— `[tool error] …`、`[ERROR]: command rejected for safety (…)`、`[tool denied by user: …]`

`WebChannel.send` 只把 `Message.content` 塞进 `payload.content` 广播，不做任何文本加工。没有 i18n、没有分级文案，用户看到的就是裸异常字符串（前缀 `[error]`）。

### AgentClient 断连 fail-fast

`AgentClient.send_request_stream` 在 `await q.get()` 上等待 AgentServer 的帧。若 AgentServer 崩溃 / 关闭 ws 导致 `_recv_loop`（`agent_client.py:57-71`）结束，`_recv_loop` 的 `finally` 调 `_fail_pending`：向所有 pending 请求的 queue 推一个 `ConnectionError("agent server disconnected")`。`send_request_stream` 检测到 `isinstance(data, BaseException)` 立即抛出（不再喂给 `model_validate`），进而 `MessageHandler` 的 except 把它转成 `[error] …` chat.final。曾经这里是「`await q.get()` 无超时，AgentServer 崩溃则永久挂起」——现已 fail-fast。

不加 wall-clock 请求超时（那会误杀多步长 agent run）；现有 `ping_interval=30 / ping_timeout=300`（`agent_client.py:40-41`）仍兜底静默死连接，`fail-fast` 兜底主动断连。

---

## Hook 在失败处理中的角色

Hook 机制本身见 [`hook-design.md`](./hook-design.md)，这里只讲它和失败的关系。

| Hook 事件 | 触发位置 | 在失败处理中的角色 |
|---|---|---|
| `ON_MODEL_EXCEPTION` | `agent_loop.py:378`（内层）、`:188`（外层） | **模型失败重试入口**。`RetryHook` 实现它：瞬时异常 + `retry_attempt<1` → `request_retry(1.0)`；非瞬时/二次不重试 |
| `ON_TOOL_EXCEPTION` | `decorator.py:73-75`（via `@hook`） | **现已激活**。`ToolManager.execute` 不再吞异常，工具异常触发该事件；`RetryHook` 重试瞬时一次，非瞬时放行让 `agent_loop` 兜底 |
| `before_tool_call` | `decorator.py:51` | `PermissionHook` 在此拦截（DENY→force_finish，ASK→HookInterrupt）。不处理失败但能**阻止**失败工具执行 |
| `before_model_call` | `agent_loop.py:241` | 可调 `ctx.request_force_finish(result)` 跳过本轮模型调用（`agent_loop.py:250-260`） |
| `after_model_call` | `agent_loop.py:353/365/370` | 纯通知，不参与失败处理 |

控制流信号（详见 [`hook-design.md`](./hook-design.md) §控制流信号）：

- `RetryRequest(delay)` —— hook 请求重试，`ctx.request_retry()` 设置，`ctx.consume_retry_request()` 消费。`RetryHook` 用它。
- `ForceFinishRequest(result)` —— hook 请求跳过本步、直接返回指定结果，`PermissionHook` DENY 用它。
- `HookInterrupt` —— 立即中断、等人审批，`Exception` 子类，`PermissionHook` ASK 用它。

`HookManager.execute` 是**容错的**（fail-soft，见 [`hook-design.md`](./hook-design.md) §HookManager）：单个 hook 回调崩溃只 `log.exception` 不阻断其他 hook，只有 `HookInterrupt` 传播。这保证「一个旁观 hook 崩了不该炸主流程」。

`@hook` 装饰器内置重试循环（`decorator.py:59-82`，`_MAX_RETRY_ATTEMPTS=3`）：方法体失败触发 `on_exception` 事件，`RetryHook` 可请求重试（带 `delay` sleep）。这条重试路径现在对工具失败真正生效（`ON_TOOL_EXCEPTION` 已激活）。

---

## 配置 / 超时 / 上限

配置真源：`twinkle/resources/config.yaml` + 校验模型 `config/schema.py`。

### 失败处理相关配置项

| 配置项 | 默认值 | 作用 | 位置 |
|---|---|---|---|
| `agent.max_steps` | 1000 | ReAct 循环硬上限，超限 `yield e2a.error` | `schema.py:62` / `config.yaml:23` |
| `llm.timeout` | 120.0 | LLM per-chunk read 超时秒；hang 住→`APITimeoutError`（瞬时，重试） | `schema.py:59` / `config.yaml:22` |
| `context_compression.token_threshold` | 60000 | 估算 token（char//3）超此即压缩历史 | `schema.py:66` / `config.yaml:25` |
| `context_compression.keep_recent_pairs` | 6 | 压缩时保留最近 N 个 user/assistant 对 | `schema.py:67` / `config.yaml:26` |
| `permissions.enabled` | false | 权限总开关（关 = 全 ALLOW 无审计；`command_exec` 仍走 builtin_rules） | `schema.py:109` / `config.yaml:47` |
| `permissions.tools.command_exec` | require-approval | `command_exec` 需审批（引擎归一为 ASK） | `schema.py:113` / `config.yaml:51` |
| `subagent.max_steps` | 50 | 子 agent ReAct 上限（紧于 1000） | `schema.py:131` / `config.yaml:66` |
| `subagent.hard_timeout` | 300.0s | 子 agent 绝对超时（`asyncio.wait_for` 包整个 child run） | `schema.py:132` / `config.yaml:67` |
| `subagent.soft_timeout` | 120.0s | 无流式活动超时（reset 计时器） | `schema.py:133` / `config.yaml:68` |
| `subagent.abort_timeout` | 30.0s | 取消卡死子的等待窗口 | `schema.py:134` / `config.yaml:69` |
| `subagent.max_result_chars` | 8000 | 子结果截断上限（防父上下文爆炸） | `schema.py:136` / `config.yaml:71` |

### 硬编码超时 / 上限（不在 config.yaml）

| 常量 | 值 | 位置 | 作用 |
|---|---|---|---|
| `_MAX_HOOK_RETRIES` | 3 | `agent_loop.py:126` | 模型调用 hook 重试上限护栏（共 4 次尝试；`RetryHook` 实际只重试 1 次） |
| `_MAX_RETRY_ATTEMPTS` | 3 | `decorator.py:25` | `@hook` 装饰方法重试上限护栏（共 4 次；`RetryHook` 实际只重试 1 次） |
| `RetryHook.max_retries` / `delay` | 1 / 1.0s | `hooks/builtin/retry_hook.py` | 瞬时异常重试一次 + 退避 1s（构造参数可调，未进 config） |
| `command_exec timeout_seconds` | 300（clamp [1,3600]） | `command_exec.py:110,139` | 单条命令超时 |
| `command_exec max_output_chars` | 20000 | `command_exec.py:113` | 输出截断 |
| `file_tools _WRITE_MAX_BYTES` | 5 MiB | `file_tools.py:31` | 写文件大小上限 |
| `web_fetch` httpx timeout | 15.0s | `web_fetch.py:51` | HTTP GET 超时 |
| `web_search` httpx timeout | 15.0s | `web_search.py:70` | HTTP POST 超时 |
| AgentClient `ping_interval`/`ping_timeout` | 30 / 300 | `agent_client.py:40-41` | Gateway→AgentServer 心跳 |

### 仍没有的（见 §设计缺口与取舍）

- **无熔断**——只有 `max_steps=1000` 兜底，转圈 / 重复失败的 agent 会一路烧到顶（jiuwen 有 `CircuitBreakerRail` 自动止损）。
- **退避策略固定**——`RetryHook` 的 `delay=1.0s` 是固定值，无指数退避；重试次数固定 1 次，未进 config（构造参数可调）。

---

## 设计缺口与取舍

曾经的五个缺口已全部修复，记录如下；其后是仍存的刻意取舍。

### 已修复的缺口（历史）

1. **Gateway 吞 `e2a.error` 文本**——曾因 `else` 分支按 `body.result.content` 取内容、`e2a.error` 无 `result` 键 → 空消息。**修复**：`message_handler.py` 加 `elif response_kind=="e2a.error"` 专门分支，`content="[error] "+body.error` 走 `CHAT_FINAL`。
2. **`ON_TOOL_EXCEPTION` 死代码**——曾因 `ToolManager.execute` 内部 catch-all 吞掉所有异常，永不冒泡到 `@hook`。**修复**：去掉 `execute` 的 catch-all，异常上抛触发 `ON_TOOL_EXCEPTION`，`RetryHook` 重试瞬时一次；兜底上移到 `agent_loop` 调用处。
3. **LLM 调用无超时**——曾无 `asyncio.wait_for`、未配 SDK timeout，模型 hang 住则永久阻塞。**修复**：`LLMClient(timeout=120)` 传给 `AsyncOpenAI`，per-chunk read timeout → `APITimeoutError`（瞬时，重试）。
4. **AgentClient 无请求超时**——曾 `await q.get()` 无超时，AgentServer 崩溃则永久挂起。**修复**：`_recv_loop` 的 `finally` → `_fail_pending` 向 pending 队列推 `ConnectionError`，`send_request_stream` 即抛（fail-fast，不加 wall-clock 以免误杀长 run）。
5. **无内置模型重试**——曾默认硬失败，重试需自写 hook。**修复**：`RetryHook`（由 `main()` 传入，无依赖）对瞬时异常重试一次（工具 + 模型皆然）。

### 取舍（仍存）

- **工具软失败 / 模型硬失败的二分**——刻意为之：工具可自愈（看到错误换路），模型坏上下文不该死循环重试。瞬时异常在两侧都先重试一次，仍失败才走各自的软/硬路径。
- **瞬时重试一次，更复杂的重试仍 opt-in**——`RetryHook` 只做「瞬时异常重试一次 + 固定 1s 退避」；换模型、压缩后重试、指数退避、按异常类型分级，仍要自写 hook。代价是简单场景之外仍需开发。
- **错误回灌用纯字符串而非 `is_error` 结构**——简单直接，但前缀不统一（`[error]` / `[tool error]` / `[ERROR]:`），且 `web_fetch`/`web_search` 的 HTTP 错误走 `[tool error]` 而非工具级 `[ERROR]:`。
- **`command_exec` 非零退出码不算失败**——退出码非零是常态，当错误会误导模型。
- **无熔断**——只有 `max_steps=1000` 兜底，转圈 / 重复失败的 agent 会一路烧到顶（jiuwen 有 `CircuitBreakerRail` 自动止损）。
- **不加 wall-clock 请求超时**——AgentClient 用 fail-fast 兜底主动断连，不设固定墙钟，避免误杀多步长 agent run；静默死连接仍由 ping_timeout=300 兜底。

---

## 文件地图

| 文件 | 角色 |
|---|---|
| `agentserver/tools/manager.py` | `ToolManager.execute`——抛异常不吞（未知工具仍返回 `[error]` 串）；兜底下移到 agent_loop |
| `agentserver/agent_loop.py` | ReAct 主循环——模型重试循环（含 sleep 退避）、`ON_MODEL_EXCEPTION`、`_hooked_tool_call` 调用处 `except Exception`→`[tool error]` 兜底、`tool_result` 回灌、`e2a.error` 产生点 |
| `agentserver/llm_client.py` | `LLMClient.stream`——模型调用入口，无 try/except、有 SDK `timeout` |
| `agentserver/server.py` | `run_task` 兜底 agent loop 异常 → `e2a.error str(exc)`；`build_agent_loop` 仅自动装配 `SubagentContextHook`（其 executor 在此构造），`main()` 传入 `RetryHook`/`PermissionHook`/`SkillHook`/`MemoryHook`/`LoggingHook`（无依赖）；`ws_handler` 并发路由 |
| `agentserver/hooks/builtin/retry_hook.py` | `RetryHook`——瞬时异常重试一次（模型+工具），`is_transient`/`TRANSIENT_EXCEPTIONS` 分类 |
| `e2a/models.py` | `E2AResponse`——`response_kind` 含 `e2a.error`，错误文本在 `body["error"]` |
| `agentserver/hooks/decorator.py` | `@hook` 装饰器——before/after/exception + 跳过执行 + 重试（`_MAX_RETRY_ATTEMPTS=3`） |
| `agentserver/hooks/base.py` | `HookEvent`（含 `ON_MODEL_EXCEPTION`/`ON_TOOL_EXCEPTION`）+ `RetryRequest`/`ForceFinishRequest`/`HookInterrupt` + `on_model_exception`/`on_tool_exception` no-op |
| `gateway/message_handler.py` | `_process_stream`——`e2a.error` 专门分支保文本送达；except 分支保留 ws 断连错误文本 |
| `gateway/agent_client.py` | `send_request_stream`——检测 `ConnectionError` 即抛（fail-fast）；`_recv_loop` finally→`_fail_pending` 推错；ping 30/300 |
| `agentserver/tools/builtin/command_exec.py` | `command_exec`——自处理 `[ERROR]:` 串 + 非零退出码返回 JSON |
| `agentserver/tools/builtin/file_tools.py` | 文件工具——自处理 `[ERROR]:` 串 + 5MiB 写上限 |
| `agentserver/tools/builtin/web_fetch.py` / `web_search.py` | HTTP 工具——`raise_for_status()` 不自兜，HTTP 错冒泡到 `@hook`→`agent_loop` `[tool error]`；httpx 15s |
| `agentserver/tools/builtin/subagent/executor.py` | `SubagentExecutor`——软/硬/abort 超时 + 异常全包 `SubagentResult(success=False)` 不抛；子 loop 默认装 `RetryHook` |
| `agentserver/tools/builtin/subagent/tools.py` | `_wrap`——`SubagentResult` 转串 tool_result + stop hint；`spawn_subagent` 工具入口 |
| `agentserver/tools/builtin/subagent/models.py` | `SubagentResult` / `SoftTimeoutError` / `EXCLUDED_TOOLS` |
| `config/schema.py` + `resources/config.yaml` | `max_steps` / `llm.timeout` / 压缩阈值 / subagent 超时 / permissions 档位等 |

---

## 与 jiuwenswarm 的差异

聚焦失败这条线（回调框架的大对比见 [`hook-design.md`](./hook-design.md)）：

| | jiuwenswarm | Twinkle |
|---|---|---|
| 工具失败回灌形态 | 结构化 `ToolMessage` + `AbilityExecutionError`（带 `tool_message` 字段） | 裸字符串 `[tool error]` / `[ERROR]:`（前缀不统一） |
| 工具异常分类 | `ToolInterruptException`（人工审批）/ `CancelledError`（优雅串）/ JSON 畸形（自纠正串）/ 空结果（占位串）分特化处理 | `except Exception` → `RetryHook` 仅区分瞬时/非瞬时；非瞬时兜底 `[tool error] {type}: {exc}` |
| 模型错误归一 | `MODEL_CALL_FAILED`(181001) → `ModelError(recoverable=True)`，所有错误类型归一 | 裸 `str(exc)`，不归一 |
| 模型重试 | 显式 `AsyncOpenAI(max_retries=3, timeout=60)`，SDK 内部退避；agent 层 `ModelBackupRail` 但**未注册** | `LLMClient(timeout=120)` + `RetryHook`（瞬时异常重试一次，`main()` 传入） |
| 模型失败回退用户 | piggyback 在 `answer` 事件（`result_type="error"`），文本走正常 content 通道**必达** | 专有 `e2a.error` 帧 + Gateway 专门分支，文本以 `[error] …` **送达** |
| 工具结果给客户端 | `_infer_tool_result_error` 推断 `is_error`/`success`/`status` 标志 | 不标（`tool_result` 只回模型，不直接发客户端） |
| 循环卡死熔断 | `CircuitBreakerRail`（无进展 ≥30 / 未知工具 ≥10 / ping-pong ≥20 / 重复 ≥10 → `force_finish` 止损，中英双语文案） | 无，仅 `max_steps=1000` 硬上限 |
| 特殊错误文案 | 图片不支持 → 友好中文文案不抛；断路器 / `command_exec` 有 i18n 文案表 | 无 i18n，硬编码短句 + `str(exc)` |
| `command_exec` 非零退出码 | 不算失败，返回 JSON，由 `ToolResultErrorDetector` 推断 error | 不算失败，返回 JSON（同） |
| 工具中断（人工审批） | `ToolInterruptException` → `chat.ask_user_question` 事件 + 权限审批 rail | `HookInterrupt` → `e2a.ask` 帧 + `PermissionHook`（语义同，名字不同） |

两边对失败的主链判断**一致**：工具失败回灌续循环、模型失败终止回退，且都「瞬时异常可重试」。差异在结构化程度与熔断——jiuwen 更结构化、有熔断；Twinkle 更裸、无熔断。砍掉熔断 / 错误码归一 / 工具结果错误推断，是因为学习型重实现优先跑通主链，这些能力在无规模化 / 多模型 / 客户端结构化错误展示场景前是纯成本。

---

## 设计决策回顾

### 为什么工具软失败、模型硬失败

工具失败是「局部、可恢复」的——换参数、换工具、放弃并回答，模型看到错误就能调整，不该一次报错即死。模型失败是「全局、可能死循环」的——坏上下文 / 鉴权错重试也是同样的错，不终止会烧 token 到 `max_steps`。二分把「可自愈」留给循环、「不可自愈」交给终止，是 ReAct 失败处理的根本判断。瞬时异常在两侧都先重试一次（`RetryHook`），不改变这个二分——重试成功就当没失败，仍失败才走各自的软/硬路径。

### 为什么瞬时重试一次，更复杂的重试仍 opt-in

哪些错该重试、退避多久、要不要换模型，强依赖场景——限流该退避重试，鉴权错重试无用，上下文超限该压缩后重试。`RetryHook` 只覆盖最常见、最安全的子集：瞬时异常（网络/超时/限流/5xx）重试一次 + 1s 退避，由 `main()` 传入（无依赖）。更复杂的（换模型、压缩后重试、指数退避、按异常分级）仍留给 `on_model_exception`/`on_tool_exception` hook 按场景实现。这避免「默认零重试」的尴尬，又不替你拍板复杂场景。

### 为什么错误回灌用纯字符串而非 `is_error` 结构

OpenAI tool 协议的 `tool` 消息 `content` 本就是字符串，用结构化对象还得让模型学会读 `is_error` 字段——不如直接把人类可读的错误描述喂给它，让它像看到「command timed out」一样自然换路。代价是前缀不统一（`[error]`/`[tool error]`/`[ERROR]:`），且 `web_fetch`/`web_search` 的 HTTP 错误走 `[tool error]` 而非工具级 `[ERROR]:`——模型得适应多种长相的错误串。jiuwen 用 `AbilityExecutionError` 结构化是另一条路，Twinkle 选了简单。

### 为什么 `e2a.error` 用 `body["error"]` 而非独立字段

`E2AResponse` 用一个 `body: dict` 承载所有 kind 的载荷（`e2a.chunk` 放 `result.content`，`e2a.error` 放 `error`，`e2a.ask` 放 `approval_id` 等），少一个字段、一种序列化形态。代价是 Gateway 翻译必须按 kind 取不同 key——曾经漏给 `e2a.error` 写专门分支，导致取错 key（`body.result.content`）拿到空串、错误文本丢失。现已补上专门分支（`content="[error] "+body.error`）。jiuwen 复用 `answer` 事件（`result_type="error"`）虽然丑，但错误文本走正常 content 路径必达，反而没这个坑——这是「专有类型更干净」与「复用通道更稳健」的权衡，Twinkle 选了专有类型并补齐翻译。

### 为什么兜底在 `agent_loop` 调用处而非 `ToolManager.execute` 层

曾经 `execute` 内部 try/except 兜底，保证「任何工具异常都不击穿循环」不依赖调用方。代价是 `ON_TOOL_EXCEPTION` 成了死代码——异常在 manager 层就被吃成串，永不冒泡到 `@hook`。**现已反转**：`execute` 抛异常（让 `ON_TOOL_EXCEPTION` 触发、`RetryHook` 能重试），兜底成 `[tool error]` 串的职责上移到 `agent_loop` 调用处（`except Exception`，在 `HookInterrupt` 之后）。不变式「工具异常不击穿循环」由调用方兜底保证；`execute` 契约从「返回错误串」变成「抛异常」，唯一调用方 `_hooked_tool_call` 已同步。代价是 `OTel` 的 `gen_ai.tool` span 行为变了：失败工具的 span 现在是 ERROR + `record_exception`（曾因 execute 吞异常而恒 OK）——这反而更正确。

### 为什么 `command_exec` 非零退出码不算失败

`grep` 没匹配返回 1、`test` 失败返回非零，是命令正常表达「没找到 / 不成立」的方式。把它当错误会让模型误以为命令「坏了」而放弃。退出码、stdout、stderr 全给模型，让它自己解读——这是把「失败」的定义权交给语义而非进程退出码。

### 取舍：无熔断 / 退避固定 / 不加 wall-clock 超时

LLM 超时（SDK 120s）与 AgentClient fail-fast 已补，剩下的取舍：**无熔断**（转圈/重复失败靠 `max_steps` 兜底，会烧到顶）、**退避固定 1s**（无指数退避，重试次数固定 1 次未进 config）、**不加 wall-clock 请求超时**（用 fail-fast 兜底主动断连，避免误杀长 agent run）。在没有规模化 / 多并发 / SLA 场景前，熔断与指数退避是纯成本；单个转圈 agent 会烧到 `max_steps` 是已知待补的点。
