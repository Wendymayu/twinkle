# Twinkle Hook 机制设计文档

## 一、概述

Hook 是 Agent 执行循环中的钩子系统——每个 Hook 实现一组生命周期回调方法，在 Agent 的 invoke / model_call / tool_call 等关键节点按优先级顺序执行，实现安全拦截、可观测上报、上下文压缩恢复等横切能力，**无需修改 Agent 核心代码**。

核心思想：Agent 的执行逻辑和横切能力彻底解耦。加一个新能力（如日志采集、安全拦截、上下文压缩恢复）只需写一个 Hook 类并 `register_hook()`，不需要改 AgentLoop 的一行代码。

**本设计的定位**：核心基础设施精简（不搬 jiuwen 的 AsyncCallbackFramework 全套），但 HookEvent 和 AgentHook 定义 11 个生命周期（和 jiuwen 一一对应），预留暂不触发的事件以保持对照映射完整性。

---

## 二、核心类型定义

### 2.1 HookEvent（枚举，11 个值）

**为什么需要这个类**：HookEvent 定义了 Agent 执行循环中有哪些"时间节点"可以插入钩子。它解决的核心问题是：**钩子的触发点必须是固定且有限的，而不是任意字符串**。

如果没有 HookEvent，开发者可能会用字符串 `"before_model_call"` 注册回调——这容易拼写错误、没有 IDE 提示、无法在注册时校验有效性。枚举的好处是：
- **类型安全**：注册回调时只能传入合法的枚举值，编译/运行时就能发现错误
- **固定词汇表**：钩子触发点是 Agent 循环的"关口"，不是任意位置——枚举明确声明了这些关口是什么
- **对照映射**：每个枚举值和 jiuwen 的 `AgentCallbackEvent` 一一对应，学习时一眼看出对应关系

可以理解为：HookEvent 是一张**时间表**，上面写着 Agent 循环里所有可以被拦截的时间节点。

和 jiuwen 的 `AgentCallbackEvent` 一一对应：

| HookEvent | jiuwen 映射 | 当前是否触发 |
|---|---|---|
| `BEFORE_INVOKE` | `BEFORE_INVOKE` | ✅ |
| `AFTER_INVOKE` | `AFTER_INVOKE` | ✅ |
| `BEFORE_MODEL_CALL` | `BEFORE_MODEL_CALL` | ✅ |
| `AFTER_MODEL_CALL` | `AFTER_MODEL_CALL` | ✅ |
| `ON_MODEL_EXCEPTION` | `ON_MODEL_EXCEPTION` | ✅ |
| `BEFORE_TOOL_CALL` | `BEFORE_TOOL_CALL` | ✅ |
| `AFTER_TOOL_CALL` | `AFTER_TOOL_CALL` | ✅ |
| `ON_TOOL_EXCEPTION` | `ON_TOOL_EXCEPTION` | ✅ |
| `AFTER_REACT_ITERATION` | `AFTER_REACT_ITERATION` | 预留，暂不触发 |
| `BEFORE_TASK_ITERATION` | `BEFORE_TASK_ITERATION` | 预留，暂不触发 |
| `AFTER_TASK_ITERATION` | `AFTER_TASK_ITERATION` | 预留，暂不触发 |

### 2.2 AgentHook（基类）

**为什么需要这个类**：AgentHook 解决的核心问题是：**一个横切能力往往关心多个时间节点，而不是一个**。

如果只用普通函数做钩子（`@hook.before("model_call", my_func)`），每个函数只能挂在一个事件上。但现实中，一个能力是跨多个节点的：
- 日志采集关心 model_call + tool_call 的 before/after（4 个事件）
- 可观测关心 invoke + model_call + tool_call 的全部生命周期（8 个事件）
- 上下文压缩恢复只关心 on_model_exception（1 个事件），但它需要 `init()` 时存储配置、需要跨事件共享压缩状态

把多个回调函数散落在各处注册，就失去了"这是一个完整能力"的内聚性。AgentHook 把**一个能力的所有回调打包成一个类**：
- 子类只重写关心的方法，不关心的自动空操作——不需要写 `pass` 或注册空回调
- `priority` 统一管理——同一个 Hook 的所有回调优先级一致，不会出现"日志的 model_call 钩子优先级 10 但 tool_call 钩子优先级 50"这种混乱
- `init/uninit` 提供生命周期——注册时初始化（如拿到 agent 引用、读取配置），注销时拆卸（如关闭连接、清理缓存）
- `get_callbacks()` 自动检测——只注册子类实际重写的方法，空壳方法零开销

可以理解为：AgentHook 是一个**能力的完整包装**——不是单个回调函数，而是一个"插件"，声明了它关心的所有时间节点。

和 jiuwen 的 `AgentRail` 对应。所有 Hook 继承此基类，子类只重写关心的钩子方法：

```python
class AgentHook:
    priority: int = 50  # 执行顺序：数值越大越先执行

    async def init(self, agent: AgentLoop) -> None: ...      # 注册时初始化
    async def uninit(self, agent: AgentLoop) -> None: ...    # 注销时拆卸

    # 11 个生命周期钩子——全部默认空操作
    async def before_invoke(self, ctx: HookContext) -> None: ...
    async def after_invoke(self, ctx: HookContext) -> None: ...
    async def before_model_call(self, ctx: HookContext) -> None: ...
    async def after_model_call(self, ctx: HookContext) -> None: ...
    async def on_model_exception(self, ctx: HookContext) -> None: ...
    async def before_tool_call(self, ctx: HookContext) -> None: ...
    async def after_tool_call(self, ctx: HookContext) -> None: ...
    async def on_tool_exception(self, ctx: HookContext) -> None: ...
    async def after_react_iteration(self, ctx: HookContext) -> None: ...
    async def before_task_iteration(self, ctx: HookContext) -> None: ...
    async def after_task_iteration(self, ctx: HookContext) -> None: ...

    def get_callbacks(self) -> dict[HookEvent, Callable]:
        """自动检测子类重写了哪些方法，只返回被重写的 {event: bound_method}。"""
```

`get_callbacks()` 使用 `_is_base_method()` 检测——和 jiuwen 的实现一致，只返回子类实际重写的方法，未重写的空壳方法零开销跳过。

### 2.3 HookContext（数据包）

**为什么需要这个类**：HookContext 解决的核心问题是：**不同时间节点传递的信息不同，但 Hook 需要一个统一接口来接收它们**。

如果每个钩子方法接收不同的参数（`before_model_call(messages, tools)` vs `before_tool_call(name, args)`），Hook 的接口就变得复杂且不一致。HookContext 把所有信息打包成一个统一数据包：
- **公共字段始终存在**：`agent`、`session_id`、`request_id`、`extra`——不管在哪个时间节点，Hook 都能安全访问
- **阶段特有字段通过 `inputs` 区分**：`inputs` 是 `HookInputs` 的子类（`InvokeInputs` / `ModelCallInputs` / `ToolCallInputs`），携带当前阶段的特有数据。Hook 通过 `ctx.event` 知道自己处在哪个节点，据此访问对应的 `inputs` 子类
- **`extra` dict 实现跨 Hook 通信**：Hook A 写入 `ctx.extra["memory_variables"]`，Hook B 后续读取——不需要 Hook 之间互相引用，完全解耦
- **控制流信号也统一在 ctx 上**：`request_retry()` / `request_force_finish()` 不需要额外的返回值机制，直接在 ctx 上设信号，调用方后续检查

可以理解为：HookContext 是一张**通知单**——上面写着"现在是什么时间点、输入是什么、会话信息是什么、其他 Hook 留了什么信息、你可以发出什么控制信号"。

和 jiuwen 的 `AgentCallbackContext` 对应。每次触发钩子时传入：

```python
@dataclass
class HookContext:
    agent: AgentLoop                    # Agent 引用
    event: HookEvent                    # 当前触发的事件
    inputs: HookInputs                  # 当前阶段的输入（按阶段细分）
    session_id: str | None              # 当前会话 ID
    request_id: str | None              # 当前请求 ID
    extra: dict                         # 跨 Hook 共享字典
    exception: Exception | None         # 异常时的错误对象
    retry_attempt: int = 0              # 当前是第几次重试

    # 控制流信号方法
    def request_retry(self, delay: float = 0) -> None: ...
    def request_force_finish(self, result: Any = None) -> None: ...
    def consume_retry_request(self) -> RetryRequest | None: ...
    def consume_force_finish_request(self) -> ForceFinishRequest | None: ...
```

### 2.4 HookInputs（按阶段细分）

**为什么需要这个类**：HookInputs 解决的核心问题是：**不同阶段的数据形状不同，需要类型安全地区分它们**。

`BEFORE_MODEL_CALL` 需要传递 `messages + tools`，`BEFORE_TOOL_CALL` 需要传递 `name + args + tool_call_id`——如果全部塞进 HookContext 的平铺字段，要么字段冗余（每个事件都有很多 None 字段），要么类型模糊（`Any` 到处都是）。用 typed dataclass 子类：
- 每个阶段的数据形状一目了然——`ModelCallInputs` 只有 messages 和 tools，`ToolCallInputs` 只有 name、args、tool_call_id
- IDE/类型检查器能验证——Hook 在 `before_tool_call` 里访问 `ctx.inputs.name` 是安全的，因为此时 `inputs` 必定是 `ToolCallInputs`
- 对照映射清晰——和 jiuwen 的 `InvokeInputs / ModelCallInputs / ToolCallInputs / TaskIterationInputs` 一对一

可以理解为：HookInputs 是通知单上的**具体内容部分**——不同时间点的通知单，内容格式不同，但信封格式（HookContext）统一。

和 jiuwen 的 `InvokeInputs / ModelCallInputs / ToolCallInputs / TaskIterationInputs` 对应：

| Twinkle | jiuwen 映射 | 对应事件 |
|---|---|---|
| `InvokeInputs(query, envelope)` | `InvokeInputs` | BEFORE/AFTER_INVOKE |
| `ModelCallInputs(messages, tools)` | `ModelCallInputs` | BEFORE/AFTER/ON_MODEL_CALL |
| `ToolCallInputs(name, args, tool_call_id)` | `ToolCallInputs` | BEFORE/AFTER/ON_TOOL_CALL |
| `TaskIterationInputs(envelope)` | `TaskIterationInputs` | BEFORE/AFTER_TASK_ITERATION（预留） |

### 2.5 控制流信号

**为什么需要这些类**：控制流信号解决的核心问题是：**Hook 不只是"观察者"，它还能影响执行流程**。

如果 Hook 只能看不能动，那上下文压缩恢复就无法工作——压缩后需要告诉 AgentLoop "请用压缩后的上下文重试 LLM 调用"，这不能通过返回值实现（因为多个 Hook 按顺序执行，返回值会被下一个 Hook 的返回覆盖）。用信号机制：
- **`RetryRequest`**：Hook 在 `on_model_exception` 里检测到 413 错误 → 压缩上下文 → 调用 `ctx.request_retry()` → AgentLoop 消费信号后重试当前步骤。为什么是独立 dataclass 而不是布尔标志？因为重试可能需要延迟（`delay`），未来可能需要指定重试次数上限，单独的类容易扩展
- **`ForceFinishRequest`**：Hook 在 `before_model_call` 里检测到安全风险 → 调用 `ctx.request_force_finish("操作被拒绝")` → AgentLoop 消费信号后跳过 LLM 调用，直接返回拒绝结果。为什么携带 `result`？因为强制结束时需要知道"返回什么给用户"——拒绝消息、错误信息等
- **`HookInterrupt`**：Hook 在 `before_tool_call` 里检测到需要人类审批 → 抛出 `HookInterrupt` → AgentLoop 捕获异常，暂停执行等待审批。为什么用异常而不是信号？因为中断需要**立即打断**当前执行流——和 `retry/force_finish`（"之后处理"的信号）不同，中断是"现在就停下来"的紧急刹车

三种信号的对比：

| 信号 | 时机 | 类别 | jiuwen 对应 |
|---|---|---|---|
| `request_retry()` | 在 `on_*_exception` 里 | 事后信号——"下次重试" | `ctx.request_retry()` |
| `request_force_finish()` | 在 `before_*` 里 | 事前信号——"跳过当前步骤" | `ctx.request_force_finish()` |
| `HookInterrupt` | 在 `before_tool_call` 里 | 立即中断——"现在就停" | `ToolInterruptException` |

和 jiuwen 的 `RetryRequest / ForceFinishRequest / ToolInterruptException` 对应：

```python
@dataclass
class RetryRequest:
    delay: float = 0  # 重试前的等待时间

@dataclass
class ForceFinishRequest:
    result: Any = None  # 强制结束时的返回结果

class HookInterrupt(Exception):
    """Hook 中断执行信号——对应 jiuwen 的 ToolInterruptException。
    当 Hook 需要暂停执行等待外部审批（如 HITL 权限审批）时抛出。
    当前 roadmap 不做权限审批，但接口形状预留，未来 HITL 实现时直接使用。"""
    def __init__(self, message: str = "", data: dict | None = None):
        super().__init__(message)
        self.data = data or {}
```

---

## 三、执行引擎

### 3.1 HookManager

**为什么需要这个类**：HookManager 解决的核心问题是：**"谁在什么时候运行"的管理逻辑不应该混在 AgentLoop 的业务代码里**。

如果让 AgentLoop 自己管理回调注册、优先级排序、事件分发，那 AgentLoop 的代码就变成一半业务逻辑一半基础设施——每次新增一个 Hook 注册点就要改 AgentLoop。HookManager 把这些基础设施职责抽离出来：
- **注册/注销**：AgentLoop 只需调用 `register_hook(hook)`，不需要关心回调怎么排序、怎么存储
- **优先级排序**：HookManager 维护每个事件的回调列表，按 priority 降序排列。AgentLoop 触发事件时只需 `execute(event, ctx)`，不需要关心"哪个 Hook 先跑"
- **事件分发**：同一个事件可能有多个 Hook 响应（如 `BEFORE_MODEL_CALL` 同时触发安全注入 + 权限检查 + 日志记录），HookManager 挨个执行它们，AgentLoop 不需要知道有几个 Hook

这是**调度器模式**——类似事件总线，但作用域限定在一个 Agent 实例内。和全局事件总线的区别是：HookManager 管理的是 Agent **内部执行流程**的拦截点，不是系统级别的广播事件。

可以理解为：HookManager 是一个**排班表管理员**——Hook 告诉它"我关心哪些时间节点、我的优先级是多少"，它负责在同一时间节点上按优先级排好执行顺序，并在触发时挨个通知。

和 jiuwen 的 `AgentCallbackManager + AsyncCallbackFramework` 对应，但**只保留核心功能**——注册、注销、按优先级排序、顺序执行。不搬 jiuwen 的 filter / circuit breaker / chain / transform / metrics 等生产级功能。

```python
class HookManager:
    def __init__(self, agent: AgentLoop) -> None:
        self._agent = agent
        # {HookEvent: [(priority, hook_method)]} 按优先级降序排列
        self._callbacks: dict[HookEvent, list[tuple[int, Callable]]] = {}
        self._hooks: list[AgentHook] = []

    def register_hook(self, hook: AgentHook) -> None:
        """1. hook.init(agent) → 2. hook.get_callbacks() → 3. 按 priority 插入排序"""

    def unregister_hook(self, hook: AgentHook) -> None:
        """1. hook.uninit(agent) → 2. 从 _callbacks 移除所有该 Hook 的方法"""

    async def execute(self, event: HookEvent, ctx: HookContext) -> None:
        """按 priority 降序挨个执行。信号留给调用方判断。"""
```

**优先级设计意图**（和 jiuwen 一致）：

| 优先级范围 | 设计意图 | 示例 |
|---|---|---|
| 90-100 | 安全拦截 / 异常恢复最先执行 | 上下文压缩恢复(100)、安全拦截(90) |
| 80-85 | 功能性干预 | prompt 注入(85)、推流(80) |
| 50（默认） | 中间层 | 记忆加载(50) |
| 0-10 | 观察型最后执行 | 日志(10)、span 管理(0) |

### 3.2 @hook 装饰器

**为什么需要这个装饰器**：@hook 装饰器解决的核心问题是：**在每个执行关口插入"触发钩子→检查信号→执行方法体→触发钩子→处理异常→检查重试"这个固定流程，是大量重复的样板代码**。

如果不用装饰器，每个关口（调 LLM、执行工具等）都需要手动写：
```python
# 每个关口重复 5+ 行样板代码
await self._hooks.execute(HookEvent.BEFORE_MODEL_CALL, ctx)
ff = ctx.consume_force_finish_request()
if ff:
    return ff.result
try:
    result = await self._llm.stream(messages, tools)
    await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
    return result
except Exception as exc:
    ctx.exception = exc
    await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
    retry = ctx.consume_retry_request()
    if retry:
        await asyncio.sleep(retry.delay)
        return await self._railed_model_call(ctx, messages, tools)  # 重试
    raise
```

这 5+ 行在每个关口重复出现，很容易漏掉某个步骤（比如忘记检查 force_finish、忘记处理 retry）。`@hook` 装饰器把这整套流程封装成一行声明：

```python
@hook(BEFORE_MODEL_CALL, AFTER_MODEL_CALL, ON_MODEL_EXCEPTION)
async def _railed_model_call(self, ctx, messages, tools):
    return await self._llm.stream(messages, tools)  # 只写核心逻辑
```

**适用范围说明**：`@hook` 用于包装**普通 async 方法**（如 `_railed_model_call`、`_railed_tool_call`，返回值而非生成器）。对于 **async generator 方法**（如 `run_stream`、`LLMClient.stream`），before/after/exception 模式不适用——生成器的生命周期跨越多次 yield，无法用简单的 enter/exit 包裹。因此 `run_stream` 使用**手动调用** `self._hooks.execute()` 来触发 BEFORE/AFTER_INVOKE 等事件，而非 `@hook` 装饰器。

可以理解为：@hook 装饰器是一个**通关检查站**——方法体是"过关的人"，装饰器在前后安排"安检流程"（触发钩子、检查信号、处理异常），过关的人不需要关心安检流程的细节。

```python
def hook(before: HookEvent, after: HookEvent, on_exception: HookEvent | None = None):
    """装饰器参数说明：
    - before: 方法执行前触发的 HookEvent
    - after: 方法正常结束后触发的 HookEvent
    - on_exception: 方法异常时触发的 HookEvent（可选，None 则异常直接上抛）

    执行流程：
    1. 触发 before 事件
    2. 检查 ctx.force_finish_request — 如有则跳过方法体直接返回
    3. 执行方法体
    4. 触发 after 事件（finally 块，确保始终执行）
    5. 异常时触发 on_exception，并检查 ctx.retry_request — 如有则重试
    """
```

---

## 四、AgentLoop 改造

### 4.1 改造原则

- `run_stream(envelope)` 的**签名和返回类型完全不变**——server.py 的 ws_handler、所有现有测试零改动
- 内部拆出 `_inner_run_stream` 逐步插入 Hook 触发点
- Hook 触发逻辑不影响流式帧的 yield 逻辑

### 4.2 改造后结构

```python
class AgentLoop:
    def __init__(self, llm, store, tools, memory):
        ...
        self._hooks = HookManager(self)

    def register_hook(self, hook: AgentHook) -> None:
        self._hooks.register_hook(hook)

    def unregister_hook(self, hook: AgentHook) -> None:
        self._hooks.unregister_hook(hook)

    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        """入口方法——签名和返回类型完全不变。"""
        ctx = HookContext(
            agent=self,
            inputs=InvokeInputs(query=..., envelope=envelope),
            session_id=envelope.session_id,
            request_id=envelope.request_id,
            extra={},
        )
        await self._hooks.execute(HookEvent.BEFORE_INVOKE, ctx)
        try:
            async for frame in self._inner_run_stream(ctx, envelope):
                yield frame
        except Exception as exc:
            ctx.exception = exc
            await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            await self._hooks.execute(HookEvent.AFTER_INVOKE, ctx)

    async def _inner_run_stream(self, ctx, envelope):
        """原 run_stream 的 ReAct 循环逻辑搬到这里，逐步插入 Hook 触发点。"""
        ...
```

### 4.3 控制流在 AgentLoop 中的处理

**force_finish**：在 `BEFORE_MODEL_CALL` / `BEFORE_TOOL_CALL` 后检查 `ctx.consume_force_finish_request()`，如果有则跳过当前步骤，直接 yield `e2a.complete` 帧。

**retry**：在 `ON_MODEL_EXCEPTION` 后检查 `ctx.consume_retry_request()`，如果有则重新执行当前 LLM 调用（最多重试 N 次，默认 3），递增 `ctx.retry_attempt`。

---

## 五、第一个示范 Hook：LoggingHook

```python
class LoggingHook(AgentHook):
    priority = 10  # 比 Observability(0) 早，比安全类(85+) 晚

    async def before_model_call(self, ctx):
        logger.info("LLM call starting, session=%s", ctx.session_id)

    async def after_model_call(self, ctx):
        logger.info("LLM call finished, session=%s", ctx.session_id)

    async def before_tool_call(self, ctx):
        logger.info("tool %s starting, args=%s", ctx.inputs.name, ctx.inputs.args)

    async def after_tool_call(self, ctx):
        logger.info("tool %s finished, session=%s", ctx.session_id)
```

注册方式——在 `build_agent_loop()` 或配置中可选启用：

```python
def build_agent_loop(hooks=None):
    llm = LLMClient(...)
    store = SessionStore(SESSIONS_DIR)
    tools = tool_manager()
    memory = LongTermMemory()
    loop = AgentLoop(llm, store, tools, memory)
    if hooks:
        for h in hooks:
            loop.register_hook(h)
    return loop, store
```

---

## 六、文件结构

```
twinkle/agentserver/hooks/
    __init__.py              # 导出 AgentHook, HookEvent, HookContext, HookManager, @hook
    base.py                  # AgentHook 基类 + HookEvent 枚举 + HookContext/HookInputs dataclass + RetryRequest/ForceFinishRequest
    manager.py               # HookManager（注册/注销/执行）
    decorator.py             # @hook 装饰器
    builtin/
        __init__.py
        logging_hook.py      # LoggingHook — 第一个示范
```

---

## 七、测试策略

所有测试遵循现有约定：不用 `pytest-asyncio`，用 `asyncio.run()` + `free_port` fixture。

| 测试文件 | 验证什么 |
|---|---|
| `tests/test_hook_manager.py` | register/unregister 优先级排序、get_callbacks 只返回被重写的方法、空 Hook 不报错 |
| `tests/test_hook_context.py` | extra dict 跨 Hook 通信、request_retry/consume 一轮、request_force_finish/consume 一轮 |
| `tests/test_hook_decorator.py` | @hook 触发 before→body→after 正常流程、异常走 on_exception、force_finish 跳过方法体、retry 重试 |
| `tests/test_agent_loop_with_hooks.py` | AgentLoop + LoggingHook 实际跑一轮 ReAct，验证钩子按优先级顺序被调用，帧输出不变 |
| `tests/test_control_flow.py` | force_finish 在 before_model_call 中拦截→直接返回完成帧、retry 在 on_model_exception 中请求重试→重跑 LLM |

---

## 八、jiuwen 全量对照映射

| Twinkle | jiuwen |
|---|---|
| `AgentHook` | `AgentRail` |
| `HookEvent`（11 个） | `AgentCallbackEvent`（11 个） |
| `HookContext` | `AgentCallbackContext` |
| `HookInputs` 各子类 | `InvokeInputs/ModelCallInputs/ToolCallInputs/TaskIterationInputs` |
| `HookManager` | `AgentCallbackManager` + `AsyncCallbackFramework`（精简版） |
| `@hook` 装饰器 | `@rail` 装饰器 |
| `register_hook()` | `register_rail()` |
| `unregister_hook()` | `unregister_rail()` |
| `ctx.extra` | `ctx.extra` |
| `ctx.request_retry()` | `ctx.request_retry()` |
| `ctx.request_force_finish()` | `ctx.request_force_finish()` |
| `LoggingHook` | `OtelRail` 的日志子集 |
| 文件 `hooks/base.py` | `openjiuwen/core/single_agent/rail/base.py` |
| 文件 `hooks/manager.py` | `agent_callback_manager.py` + `callback/framework.py` |
| 文件 `hooks/decorator.py` | `@rail` 装饰器（在 base.py 里） |
