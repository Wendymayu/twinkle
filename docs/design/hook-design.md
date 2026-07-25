# Hook 机制设计与实现

## 一句话概括

Hook 是 AgentLoop 的**生命周期切面**——在 ReAct 循环的 8 个关键节点触发回调，支持拦截、短路、重试三种控制流信号，让权限审批、日志、遥测等功能以插件形式注入，无需改动核心循环。

---

## 为什么需要 Hook

ReAct 循环的核心逻辑（思考 → 调用工具 → 读取结果 → 再思考）是稳定的，但围绕它的横切需求一直在变：

1. **权限拦截**：工具执行前需要检查"该不该执行"，不符合时跳过或暂停等人审批。
2. **日志 / 遥测**：LLM 调用、工具执行前后需要记录，用于调试和可观测性。
3. **上下文压缩**：LLM 调用出错时（token 溢出），需要压缩历史再重试。
4. **安全拦截**：某些请求需要在模型调用前直接拦截，不让 LLM 看到敏感内容。

如果把这些逻辑硬编码进 `agent_loop.py`，循环会变成一团意大利面——每加一个切面就改一次核心代码。Hook 机制把这些需求**抽成独立插件**，核心循环只管"在合适的时机喊一声"，插件决定"听到后做什么"。

类比：Hook 就像路由器的中间件链——请求在到达 handler 之前经过一层层 before-hook，返回之前经过一层层 after-hook，中间任何一层都可以短路或抛异常终止流程。

---

## 设计来源

Twinkle 的 Hook 机制对照 jiuwen 的两套概念：

| jiuwen 概念 | Twinkle 对应 | 说明 |
|---|---|---|
| `AgentCallbackEvent` (11 种) | `HookEvent` (11 种) | 生命周期触发点一一映射 |
| `AgentRail` (能力束) | `AgentHook` (能力束) | 多个回调打包进一个类 |
| `AgentCallbackManager` + `AsyncCallbackFramework` | `HookManager` | 注册 + 按优先级分发 |
| `ToolInterruptException` | `HookInterrupt` | HITL 挂起信号 |
| filter / circuit breaker / chain / transform | **不实现** | Twinkle 专注核心，砍掉高级中间件 |

选择"砍"的原因：Twinkle 是学习型重实现，核心需求是拦截 + 短路 + 重试，filter/链式变换等 jiuwen 特性目前没有使用场景，引入反而增加理解成本。

---

## 生命周期事件：11 个触发点

`HookEvent` 是枚举，11 个值对应 ReAct 循环的 11 个关键节点：

```
┌─────────────────── run_stream ───────────────────────┐
│                                                       │
│  BEFORE_INVOKE  ──→  _inner_run_stream  ──→  AFTER_INVOKE  │
│                          │                            │
│          ┌─── for step in range(MAX_STEPS) ───┐       │
│          │                                     │      │
│          │  BEFORE_MODEL_CALL                  │      │
│          │      ↓                              │      │
│          │  LLM.stream() ──→ TextDelta / Finish│      │
│          │      ↓                              │      │
│          │  AFTER_MODEL_CALL                   │      │
│          │      ↓                              │      │
│          │  [tool_calls?] ──→ _hooked_tool_call│      │
│          │      │                              │      │
│          │  BEFORE_TOOL_CALL                   │      │
│          │      ↓  execute tool                │      │
│          │  AFTER_TOOL_CALL                    │      │
│          │      ↓                              │      │
│          │  ON_TOOL_EXCEPTION (异常时)         │      │
│          │                                     │      │
│          │  ON_MODEL_EXCEPTION (LLM 异常时)    │      │
│          └─────────────────────────────────────┘      │
│                                                       │
└───────────────────────────────────────────────────────┘

  Reserved (未触发，为 jiuwen 映射保留):
  AFTER_REACT_ITERATION / BEFORE_TASK_ITERATION / AFTER_TASK_ITERATION
```

**8 个已触发，3 个保留**。保留的 3 个对应 jiuwen 的多任务迭代模型，Twinkle 当前只实现单请求 ReAct，暂不触发。

触发点分布在两个层级：

| 层级 | 事件 | 触发位置 |
|---|---|---|
| **外层** `run_stream` | BEFORE_INVOKE / AFTER_INVOKE | 请求进入和退出 |
| **内层** `_inner_run_stream` | BEFORE_MODEL_CALL / AFTER_MODEL_CALL | 每步 LLM 调用前后 |
| **内层** `_inner_run_stream` | BEFORE_TOOL_CALL / AFTER_TOOL_CALL / ON_TOOL_EXCEPTION | 每次工具执行前后（通过 `@hook` 装饰器） |
| **内层** `_inner_run_stream` | ON_MODEL_EXCEPTION | LLM 调用异常时 |

---

## AgentHook：能力束

一个 Hook 不是单个回调，而是**一组相关的生命周期方法打包进一个类**——叫"能力束"。

```python
class AgentHook:
    priority: int = 50  # 执行顺序：数字越大越先执行

    def init(self, agent) -> None: ...     # 注册时调用
    def uninit(self, agent) -> None: ...   # 注销时调用

    # 11 个生命周期回调——默认全部 no-op
    async def before_invoke(self, ctx) -> None: ...
    async def after_invoke(self, ctx) -> None: ...
    async def before_model_call(self, ctx) -> None: ...
    async def after_model_call(self, ctx) -> None: ...
    async def on_model_exception(self, ctx) -> None: ...
    async def before_tool_call(self, ctx) -> None: ...
    async def after_tool_call(self, ctx) -> None: ...
    async def on_tool_exception(self, ctx) -> None: ...
    # ... 3 个保留方法
```

子类**只覆写关心的方法**，其余保持 no-op。`get_callbacks()` 通过 `_is_base_method()` 自动检测哪些方法被覆写了——只返回被覆写的方法，no-op 的跳过。

为什么用"能力束"而不是独立函数：

- **共享状态**：同一个 Hook 的多个回调天然共享实例属性（如 `PermissionHook` 的 `self._engine`），不需要闭包或全局变量。
- **共享优先级**：一个 Hook 的所有回调按同一个 `priority` 执行，保证权限拦截（priority=100）始终在日志（priority=10）之前。
- **生命周期管理**：`init()` / `uninit()` 让 Hook 在注册/注销时做初始化和清理，不需要外部代码帮忙。

---

## HookContext：统一数据包

每个回调收到的不是散装参数，而是 `HookContext`——一个包含完整上下文的数据包：

```python
@dataclass
class HookContext:
    agent: Any                # AgentLoop 引用
    event: HookEvent          # 当前触发的事件
    inputs: HookInputs        # 阶段-specific 输入数据
    session_id: str | None    # 会话 ID
    request_id: str | None    # 请求 ID
    extra: dict               # 共享字典——跨 Hook 通信
    exception: Exception | None  # 异常信息
    retry_attempt: int        # 当前重试次数

    # 控制流信号（内部字段，consume 后清除）
    _retry_request: RetryRequest | None
    _force_finish_request: ForceFinishRequest | None
```

**`inputs` 是阶段特化的**——不同事件携带不同类型的输入：

| 事件 | inputs 类型 | 内容 |
|---|---|---|
| BEFORE/AFTER_INVOKE | `InvokeInputs` | query + envelope |
| BEFORE/AFTER/ON_MODEL_CALL | `ModelCallInputs` | messages + tools schemas |
| BEFORE/AFTER/ON_TOOL_CALL | `ToolCallInputs` | name + args + tool_call_id |

**`extra` 是跨 Hook 通信的关键**——任何 Hook 都可以往 `extra` 里写数据，后续 Hook 可以读取。例如 `PermissionHook` 在 ASK 恢复后把已批准的 `tool_call_id` 写进 `extra["_approved_tool_call_ids"]`，下次进入 `before_tool_call` 时检查 bypass。

`extra` 不用 ContextVar 的原因：HookContext 是同一个请求的同一个对象，所有 Hook 在同一次 `execute()` 调用链中共享它，天然就是 "per-request" 的，不需要 asyncio 的 task 级隔离。

---

## 控制流信号：三种干预方式

Hook 不只是"观察者"——它可以**干预执行流程**。三种信号对应三种干预级别：

### 1. 短路：ForceFinishRequest

```python
ctx.request_force_finish(result="deny message")
```

含义：**跳过当前步骤的方法体，直接返回 `result`**。

场景：`PermissionHook` 在 DENY 决定时调用——工具不执行，deny 消息直接作为 `tool_result` 回灌给 LLM。

机制：`@hook` 装饰器在触发 `before` 事件后检查 `ctx.consume_force_finish_request()`，如果非空则跳过方法体直接返回。

### 2. 挂起：HookInterrupt（异常信号）

```python
raise HookInterrupt(message="approval required", data={"approval_id": ..., "tool": ...})
```

含义：**立即中断当前执行，挂起等待外部干预**。

场景：`PermissionHook` 在 ASK 决定时抛出——需要人类审批才能继续。`_inner_run_stream` 的 `except HookInterrupt` 捕获后注册 `Future`、yield `e2a.ask` 帧、await 挂起，等 ws_handler 传入审批结果后恢复。

机制：`HookInterrupt` 是 Exception 子类，但不是"错误"——它是**控制流信号**。`HookManager.execute()` 特殊处理：遇到 `HookInterrupt` 直接 `raise` 传播，不走 fail-soft 逻辑。

### 3. 重试：RetryRequest

```python
ctx.request_retry(delay=0.5)
```

含义：**请求重新执行当前步骤**（比如 LLM 调用因 token 溢出失败，Hook 压缩上下文后请求重试）。

场景：上下文压缩 Hook 在 `ON_MODEL_EXCEPTION` 时压缩 `ctx.inputs.messages`，然后 `request_retry()` 让 LLM 用压缩后的消息重试。

机制：`@hook` 装饰器在异常路径检查 `ctx.consume_retry_request()`，如果有则 `continue` 重试方法体（最多 3 次）。`_inner_run_stream` 的 LLM 重试循环同理。

**信号消费是 consume 模式**——`consume_retry_request()` / `consume_force_finish_request()` 取出后清除，保证信号只被消费一次，不会"残留"到下一个事件。

---

## HookManager：注册与分发

`HookManager` 是核心分发器，每个 `AgentLoop` 持有一个：

```python
class HookManager:
    def __init__(self, agent: Any):
        self._agent = agent
        self._callbacks: dict[HookEvent, list[tuple[int, Callable]]] = {}
        self._hooks: list[AgentHook] = []

    def register_hook(self, hook: AgentHook) -> None:
        hook.init(self._agent)       # 初始化
        callbacks = hook.get_callbacks()  # 只取覆写的方法
        for event, method in callbacks.items():
            entries = self._callbacks.setdefault(event, [])
            entries.append((hook.priority, method))
            entries.sort(key=lambda p: p[0], reverse=True)  # 降序——数字越大越先
        self._hooks.append(hook)

    async def execute(self, event: HookEvent, ctx: HookContext) -> None:
        ctx.event = event
        for _pri, method in self._callbacks.get(event, []):
            try:
                await method(ctx)
            except HookInterrupt:
                raise  # 控制流信号——立即传播
            except Exception:
                log.exception(...)  # fail-soft——一个 Hook 挂不影响其他
```

**两个关键设计决策**：

1. **优先级降序执行**（priority 越大越先）：保证 `PermissionHook`(100) 在 `LoggingHook`(10) 之前运行——权限拦截必须先于日志记录。如果日志先跑、权限后拦截，日志会记录一个"本不该执行"的工具调用。

2. **fail-soft**：普通异常只 log 不传播，其他 Hook 继续执行。只有 `HookInterrupt` 传播——因为它不是错误，是控制流信号。选择 fail-soft 是因为 Hook 是"旁观者/拦截者"，一个旁观者崩溃不应该炸掉主流程。

---

## `@hook` 装饰器：方法的自动包装

`@hook` 把一个普通 async 方法变成自带 before/after/exception 生命周期的方法：

```python
@hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
      on_exception=HookEvent.ON_TOOL_EXCEPTION)
async def _hooked_tool_call(self, ctx, name, args) -> str:
    return await self._tools.execute(name, args)
```

装饰器自动执行 5 步流程：

```
1. await hooks.execute(BEFORE_TOOL_CALL, ctx)    → 触发 before 回调
2. ctx.consume_force_finish_request()             → 检查短路信号
   └─ 有 → 直接返回 ff.result，跳过方法体
3. await method(self, ctx, ...)                   → 执行方法体
4. await hooks.execute(AFTER_TOOL_CALL, ctx)      → 成功后触发 after 回调
5. 异常时：
   ├─ HookInterrupt → raise（传播控制流信号）
   ├─ CancelledError → raise（不干扰取消）
   └─ 其他异常 → hooks.execute(ON_TOOL_EXCEPTION, ctx)
      └─ ctx.consume_retry_request() → 有 → sleep(delay) + 重试（最多 3 次）
      └─ 无 → raise（异常传播）
```

**为什么 `_hooked_tool_call` 用装饰器而 `_inner_run_stream` 不用**：

`_inner_run_stream` 是 **async generator**（用 `yield` 产出 `E2AResponse` 帧），`@hook` 装饰器无法包装 async generator（装饰器只能处理 `return`，不能处理 `yield`）。所以 `_inner_run_stream` 用**手动** `self._hooks.execute()` 调用，而工具执行（普通 async 方法）用**装饰器**自动包装。

两种方式共享同一套信号机制（`consume_force_finish_request` / `consume_retry_request` / `HookInterrupt`），只是触发方式不同：

| 方式 | 适用 | 信号检查位置 |
|---|---|---|
| `@hook` 装饰器 | 普通 async 方法 | 装饰器内部自动检查 |
| 手动 `execute()` | async generator | 调用方手动检查 |

---

## 注册与组装

Hook 的注册发生在两个地方：

### 生产组装：`build_agent_loop()`

```python
def build_agent_loop(store, hooks=None, llm=None):
    loop = AgentLoop(llm, store, tools, memory, permission=engine)
    loop.register_hook(PermissionHook(engine))  # 始终注册——Phase 4 核心切面
    if hooks:
        for h in hooks:
            loop.register_hook(h)
    return loop
```

`main()` 调用时额外传入 `LoggingHook`：

```python
loop = build_agent_loop(store, hooks=[LoggingHook()])
```

### 测试注入：直接调用 `register_hook`

测试可以直接构造 `AgentLoop` 并注册任何 Hook：

```python
loop = AgentLoop(llm, store, tools, memory)
loop.register_hook(MyTestHook())
```

**注册顺序无关**——`HookManager` 按 `priority` 排序执行，先注册后注册不影响执行顺序。`PermissionHook`(100) 永远在 `LoggingHook`(10) 之前，无论谁先注册。

---

## 两个内置 Hook

### LoggingHook（priority=10）

```python
class LoggingHook(AgentHook):
    priority = 10

    async def before_model_call(self, ctx) -> None:
        log.info("LLM call starting, session=%s", ctx.session_id)

    async def after_model_call(self, ctx) -> None:
        log.info("LLM call finished, session=%s", ctx.session_id)

    async def before_tool_call(self, ctx) -> None:
        log.info("tool %s starting, args=%s", ctx.inputs.name, ctx.inputs.args)

    async def after_tool_call(self, ctx) -> None:
        log.info("tool %s finished, session=%s", ctx.session_id)
```

纯观察者——4 个回调只做日志记录，不干预执行流。priority=10 确保它**在功能 Hook 之后**执行：权限拦截已经完成（或放行），日志才记录"工具真正要执行了"。

### PermissionHook（priority=100）

```python
class PermissionHook(AgentHook):
    priority = 100

    async def before_tool_call(self, ctx) -> None:
        # bypass 检查——ASK 恢复后的重调用不再拦截
        if inp.tool_call_id in ctx.extra.get("_approved_tool_call_ids", set()):
            return
        decision = self._engine.check(tool=inp.name, args=inp.args, ...)
        if decision.level == "deny":
            ctx.request_force_finish(decision.deny_message)    # 短路
        elif decision.level == "ask":
            raise HookInterrupt(message="approval required", data={...})  # 挂起
        # allow → no-op
```

三种决策对应三种控制流：

| 决策 | 控制流信号 | 效果 |
|---|---|---|
| ALLOW | no-op | 工具正常执行 |
| DENY | `request_force_finish()` | `@hook` 短路，deny 消息变 tool_result |
| ASK | `raise HookInterrupt()` | 挂起等待人类审批 |

**bypass 机制**：ASK 恢复后，`_inner_run_stream` 把已批准的 `tool_call_id` 写进 `ctx.extra["_approved_tool_call_ids"]`，下次 `before_tool_call` 检查到已批准直接 return——避免恢复后重新拦截同一个工具调用。

---

## ASK 挂起/恢复流：完整数据流

这是 Hook 机制最复杂的场景——PermissionHook 抛出 `HookInterrupt` 触发人类审批，agent_loop 挂起等待，ws_handler 恢复后继续：

```
AgentLoop._inner_run_stream
  ├─ tool_call → @hook _hooked_tool_call → before_tool_call(PermissionHook)
  │     └─ ASK → raise HookInterrupt(approval_id, tool, args, ...)
  ├─ except HookInterrupt as hi:
  │     ├─ approval_id = hi.data["approval_id"]
  │     ├─ future = APPROVAL_REGISTRY.register(approval_id)  # 进程内 Future
  │     ├─ yield E2AResponse(response_kind="e2a.ask")        # 通知前端
  │     ├─ decision = await future                            # ★ 挂起 — 等审批
  │     ├─ if "allow" / "allow_always":
  │     │     ├─ ctx.extra["_approved_tool_call_ids"].add(tc["id"])  # bypass 标记
  │     │     └─ result = await _hooked_tool_call(ctx, ...)  # 重调用——bypass 放行
  │     ├─ else:
  │     │     └─ result = "[tool denied by user]"
  │     └─ tool_result → store.append → 下一步 ReAct
```

关键不变式：**挂起的 run_stream 在原 request_id 上恢复**，审批响应用独立的 request_id（R2），两者不冲突。ws_handler 的并发 per-request task 模型保证审批消息不会阻塞正在挂起的 run_stream。

---

## 优先级设计：分层约定

| 层 | priority 范围 | 典型 Hook | 为什么这个优先级 |
|---|---|---|---|
| **安全拦截** | 100+ | PermissionHook | 最先执行——拦截必须在日志/功能之前 |
| **功能切面** | 50–99 | 上下文压缩、future skill hook | 中间——安全已放行，功能切面可以安全修改上下文 |
| **观察者** | 0–49 | LoggingHook | 最后——所有拦截已完成，记录的是"真正会发生的事" |

选择**降序**（越高越先）而非升序的原因：安全拦截是"门卫"，必须站在门口。如果升序执行，日志（priority=1）会先于权限（priority=100），记录一条"不该执行的调用"。

---

## 添加新 Hook 的步骤

按照 CLAUDE.md 约定的流程：

1. 在 `twinkle/agentserver/hooks/builtin/` 下新建 `*_hook.py`
2. 写一个类继承 `AgentHook`，覆写关心的生命周期方法
3. 设置 `priority`（安全拦截 → 100+，功能 → 50–99，观察 → 0–49）
4. 在 `build_agent_loop()` 或调用处 `loop.register_hook(hook_instance)` 注册
5. `agent_loop` 自动通过 `HookManager` 触发——**核心循环零改动**

示例：添加一个上下文压缩 Hook

```python
class ContextCompressionHook(AgentHook):
    priority = 80  # 功能层——在权限之后，日志之前

    async def on_model_exception(self, ctx) -> None:
        if isinstance(ctx.exception, TokenOverflowError):
            compressed = await compress(ctx.inputs.messages)
            ctx.inputs.messages = compressed  # 修改 inputs → 下次重试用压缩后的
            ctx.request_retry(delay=0)        # 请求重试
```

---

## 文件地图

| 文件 | 角色 |
|---|---|
| `hooks/base.py` | `AgentHook` + `HookContext` + `HookInterrupt` + `RetryRequest` + `ForceFinishRequest` + 输入类型 |
| `hooks/manager.py` | `HookManager`——注册/注销/按优先级分发 |
| `hooks/decorator.py` | `@hook` 装饰器——before/after/exception + 短路 + 重试 |
| `hooks/__init__.py` | 包入口 re-export |
| `hooks/builtin/logging_hook.py` | `LoggingHook`——日志观察者（priority=10） |
| `hooks/builtin/permission_hook.py` | `PermissionHook`——权限拦截（priority=100，ALLOW/DENY/ASK） |
| `agent_loop.py` | Hook 触发点——手动 `execute()` + `@hook` 装饰器 + `HookInterrupt` 捕获 + ASK 恢复流 |
| `server.py` | `build_agent_loop()` 组装——始终注册 PermissionHook，可选传入其他 Hook |
| `permission_context.py` | `APPROVAL_CHANNEL` ContextVar——PermissionHook 的 channel 路由 |

---

## 与 jiuwenclaw 的差异

| | jiuwenclaw | Twinkle |
|---|---|---|
| 基类名 | `AgentRail` | `AgentHook` |
| 回调管理 | `AgentCallbackManager` + `AsyncCallbackFramework`（支持 filter/circuit breaker/chain/transform） | `HookManager`（只有 register/unregister/execute） |
| 控制流信号 | `ToolInterruptException` | `HookInterrupt`（语义相同，名字不同） |
| 上下文传递 | 分散参数 | `HookContext` 统一数据包 + `extra` 字典跨 Hook 通信 |
| 短路机制 | 回调返回特殊值 | `ctx.request_force_finish()` + consume 模式 |
| 重试机制 | 外部循环控制 | `ctx.request_retry()` + consume 模式 + `@hook` 内置重试循环 |
| 优先级 | 同概念，升序 | 降序——数字越大越先 |
| init/uninit | 无 | 有——注册/注销时初始化/清理 |

砍掉 filter / circuit breaker / chain / transform 是因为 Twinkle 当前场景（权限拦截 + 日志 + 遥测）不需要这些高级中间件能力，保留它们会增加 50% 的理解成本却没有使用场景。

---

## 设计决策回顾

### 为什么用类而不是函数

Hook 的多个回调共享状态（`PermissionHook._engine`）和优先级（同一个 `priority`），类天然提供这两者。如果用独立函数，要么用闭包（难调试），要么用全局变量（并发不安全），要么每次手动传优先级（容易不一致）。

### 为什么 fail-soft

Hook 是"拦截者/观察者"，不是"核心逻辑"。一个日志 Hook 崩溃不应该阻止工具执行；一个遥测 Hook 超时不应该中断 LLM 调用。只有 `HookInterrupt` 是有意为之的控制流信号，才值得传播。

### 为什么 consume 模式

`request_retry()` / `request_force_finish()` 是"一次性信号"——发出后被消费方取走并清除。如果不清除（残留模式），一个 before_tool_call 的 force_finish 会意外短路 after_model_call 的检查，造成难以调试的"幽灵短路"。consume 模式保证信号精确地只影响它应该影响的那一步。

### 为什么 manual execute() 和 @hook 并存

async generator 不能被装饰器包装——这是 Python 的根本限制（`yield` 不等于 `return`）。`_inner_run_stream` 是 Twinkle 的核心数据流（产出 E2AResponse 帧），必须是 generator，所以只能手动触发。工具执行是普通 async 方法，可以用装饰器自动包装。两种方式共享同一套信号机制，只是触发方式不同。
