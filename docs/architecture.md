# Twinkle 系统架构介绍

> 本文描述 Twinkle 从浏览器到 AgentServer 的完整架构——进程拓扑、Channel 机制、消息格式、数据流轨迹。代码以当前状态为准（流式专用，unary 已移除）。

---

## 1. 全局拓扑：两进程 + 三段通信

Twinkle 由**两个 Python 进程**和一个**Vue 前端**组成，中间有两段 WebSocket 通信：

```
┌──────────┐    ws: /ws (Vite 代理)     ┌──────────────┐    ws: E2A 信封    ┌───────────────┐
│  浏览器   │ ──────────────────────────→ │  Gateway     │ ─────────────────→ │  AgentServer  │
│ (Vue 3)  │    req/res/event 格式       │  (:19000)     │    E2AEnvelope     │  (:18000)     │
│ :5173    │ ←────────────────────────── │              │ ←───────────────── │               │
└──────────┘    event 广播                 │              │    E2AResponse     │  AgentLoop    │
                                          └──────────────┘                    │  LLM + Tools  │
                                                                               └───────────────┘
```

三段通信各有自己的**消息格式**：

| 段 | 方向 | 消息格式 | 定义文件 |
|---|---|---|---|
| 浏览器 ↔ Gateway | 双向 | `{type, id, method/event, params/payload}` | `webClient.ts` + `web_channel.py` |
| Gateway ↔ AgentServer | 双向 | E2AEnvelope / E2AResponse（Pydantic 模型） | `twinkle/e2a/models.py` |

**Gateway 是两种格式的翻译层**——入站时把浏览器 req 转为 E2AEnvelope，出站时把 E2AResponse 转回浏览器 event。

---

## 2. 进程职责与为何两进程

### 2.1 AgentServer（执行核心）

| 属性 | 说明 |
|---|---|
| 端口 | `:18000`（`TWINKLE_AGENTSERVER_PORT`） |
| 角色 | 持长任务、重资源（LLM API、工具执行） |
| 入口 | `python -m twinkle.agentserver` |
| 核心组件 | `AgentLoop`（ReAct 循环）、`LLMClient`、`SessionStore`、`ToolManager` |

AgentServer 不直接接触浏览器——它只接收 E2AEnvelope、产出 E2AResponse，对"消息来自哪里"一无所知。这使得它可以在不改动代码的前提下被任何通道（Web、飞书、钉钉）调用。

### 2.2 Gateway（连接边缘）

| 属性 | 说明 |
|---|---|
| 端口 | `:19000`（`TWINKLE_GATEWAY_PORT`） |
| 角色 | 轻量、可频繁重启的连接/调度边缘 |
| 入口 | `python -m twinkle.gateway` |
| 核心组件 | `WebChannel`、`MessageHandler`、`ChannelManager`、`AgentClient` |

Gateway 同时是**浏览器侧 ws server**和**AgentServer 侧 ws client**。它做两件事：
1. **格式翻译**：浏览器 req → E2AEnvelope，E2AResponse → 浏览器 event
2. **流式扇出**：把 AgentServer 的逐帧 E2AResponse 转成逐字 chat.delta 广播给浏览器

### 2.3 为何两进程而非一进程

这是对齐 jiuwenclaw 主架构的有意识取舍：

| 考虑 | 两进程 | 一进程 |
|---|---|---|
| 部署灵活性 | Gateway 可独立重启，不中断 Agent 任务 | 全部耦合在一起 |
| 资源隔离 | Agent 持长任务、重 LLM 调用；Gateway 轻量 | 无隔离 |
| 代码对照 | 与 jiuwenclaw 同架构，学习对比零摩擦 | 架构偏离参考实现 |
| 实际开销 | 本地两端口，进程间通信快 | — |

Twinkle 是学习型重写项目，保留两进程架构是为了与 jiuwenclaw 对照、降低架构差距。即便 Phase 0 单用户也保留此拆分——这是学习取舍，不是性能需要。

---

## 3. Gateway 内部组件详解

Gateway 由四个组件组成，在 `__main__.py` 中按单向依赖装配：

```python
# __main__.py 装配（单向依赖，无循环引用）
message_handler = MessageHandler(agent_client)         # ← 只依赖 AgentClient
channel_manager = ChannelManager(message_handler)       # ← 只依赖 MessageHandler
```

### 3.0 总体架构图

下面这张图展示了四个组件之间的**数据流向**和**依赖方向**：

```
                         ┌─────────────────── 入站方向（用户消息进来）────────────────────┐
                         │                                                          │
                         │   浏览器 ws                                               │
                         │      │                                                    │
                         │      │ {type:"req", method:"chat.send", params:{query}}    │
                         │      ▼                                                    │
                         │  ┌─── WebChannel ───────────────────────────────────────┐  │
                         │  │  channel_id="web"                                    │  │
                         │  │  ws server :19000                                    │  │
                         │  │                                                      │  │
                         │  │  ① 收浏览器 JSON → 建 Message → 立回 ACK {type:res} │  │
                         │  │  ② 调 on_message 回调 → 闭包 → ChannelManager       │  │
                         │  └─────────── on_message(msg) ──────────┬──────────────┘  │
                         │                                          │                  │
                         │                                          ▼                  │
                         │  ┌─── ChannelManager ───────────────────────────────────┐  │
                         │  │  持 MessageHandler（单向依赖）                        │  │
                         │  │                                                      │  │
                         │  │  入站：register_channel 注册闭包                     │  │
                         │  │    → channel.on_message → MH.handle_message(msg)    │  │
                         │  │                    │                                  │  │
                         │  │                    ▼                                  │  │
                         │  │  ┌─── MessageHandler ─────────────────────────────┐ │  │
                         │  │  │  持 AgentClient（单向依赖）                     │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │  入站：Message → E2AEnvelope 格式转换          │ │  │
                         │  │  │    → AgentClient.send_request_stream(envelope) │ │  │
                         │  │  │                    │                             │ │  │
                         │  │  │                    ▼                             │ │  │
                         │  │  │  ┌─── AgentClient ─────────────────────────┐  │ │  │
                         │  │  │  │  ws client 连 AgentServer(:18000)      │  │ │  │
                         │  │  │  │  _send_lock 保护并发写                  │  │ │  │
                         │  │  │  │  demux: request_id → asyncio.Queue     │  │ │  │
                         │  │  │  └──────────── ws 发 E2AEnvelope ────────┘  │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │              ··· ws 帧 ···                      │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │              AgentServer 处理                    │ │  │
                         │  │  │              产出 E2AResponse 帧                 │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │              ··· ws 帧 ···                      │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │                    │                             │ │  │
                         │  │  │                    ▼  demux 投 Queue           │ │  │
                         │  │  │  send_request_stream yield E2AResponse         │ │  │
                         │  │  │                                                  │ │  │
                         │  │  │  出站：E2AResponse → Message 格式转换          │ │  │
                         │  │  │    e2a.chunk  → Message(chat.delta)            │ │  │
                         │  │  │    e2a.complete → Message(chat.final)           │ │  │
                         │  │  │    e2a.error   → Message(chat.final, [error])  │ │  │
                         │  │  │    e2a.todo_update → Message(todo.update, body)│ │  │
                         │  │  │                    │                             │ │  │
                         │  │  │                    ▼                             │ │  │
                         │  │  │  enqueue_outbound(msg) → _robot_messages Queue │ │  │
                         │  │  └─────────── _robot_messages Queue ────┬─────────┘ │  │
                         │  │                                          │             │  │
                         │  │                                          ▼             │  │
                         │  │  出站：_dispatch_loop 循环                │             │  │
                         │  │    dequeue_outbound(msg)                 │             │  │
                         │  │    → 按 msg.channel_id 查 _channels     │             │  │
                         │  │    → channel.send(msg)                   │             │  │
                         │  │                    │                     │             │  │
                         │  │                    ▼                     │             │  │
                         │  │  ┌─── WebChannel ───────────────────────────────┐   │  │
                         │  │  │  ③ send(msg) → 转浏览器 JSON               │   │  │
                         │  │  │  → {type:"event", event:"chat.delta"}     │   │  │
                         │  │  │  → 广播给 _clients set 所有 ws 连接        │   │  │
                         │  │  └────────── ws 发 ──────────┬───────────────┘   │  │
                         │  │                               │                   │  │
                         │  │                               ▼                   │  │
                         │  │                           浏览器 ws               │  │
                         │  └───────────────────────────────────────────────────┘  │
                         │                                                          │
                         └─────────────────── 出站方向（Agent 响应回去）────────────┘


依赖方向（单向，无循环）：

    ChannelManager ──→ MessageHandler ──→ AgentClient
         │                   │                  │
         │ 持有引用           │ 持有引用          │ 持有 ws 连接
         │                   │                  │
         │ 入站：调 MH.handle_message       发 E2AEnvelope
         │ 出站：调 MH.dequeue_outbound     收 E2AResponse
```

**入站流**（用户消息进来）：
```
浏览器 req JSON → WebChannel._handle_raw → Message → on_message 回调
→ ChannelManager.register_channel 闭包 → MessageHandler.handle_message
→ E2AEnvelope → AgentClient.send_request_stream → ws 发给 AgentServer
```

**出站流**（Agent 响应回去）：
```
AgentServer E2AResponse 帧 → AgentClient demux → yield E2AResponse
→ MessageHandler._process_stream 翻译成 Message(chat.delta/final)
→ enqueue_outbound → _robot_messages Queue
→ ChannelManager._dispatch_loop dequeue_outbound
→ 按 channel_id 查 WebChannel → WebChannel.send → 浏览器 event JSON
```

### 3.1 WebChannel — 浏览器 ws 接口

[web_channel.py](../twinkle/gateway/web_channel.py) 是一个 `websockets` server，负责：

**入站**（浏览器 → Gateway）：
- 解析浏览器 JSON `{type:"req", id, method, params}`
- 构建 `Message` 对象
- 立即回一个 ACK `{type:"res", id, ok:true, payload:{accepted:true, session_id}}` — 不等 Agent 响应
- 调 `on_message` 回调把 Message 交给 ChannelManager

**出站**（Gateway → 浏览器）：
- `send(msg)` 把 `Message` 转成 `{type:"event", event, payload, request_id}` **广播给所有连接的浏览器**
- 每个 ws 连接在 `handler` 中加入 `_clients` set，离开时移除

`channel_id = "web"` — 这是 ChannelManager 路由出站消息的关键。当前只有一种 Channel，但接口预留了扩展能力。

### 3.2 ChannelManager — 入站路由 + 出站 dispatch

[channel_manager.py](../twinkle/gateway/channel_manager.py) 持有 MessageHandler（单向依赖），负责两个方向的衔接：

**注册**：
- `register_channel(channel)` — 把 channel 按其 `channel_id` 存进 `_channels` dict
- 同时给 channel 注册 `on_message` 回调，回调内调 `MessageHandler.handle_message`

**入站**：ChannelManager 本身不做入站处理——是回调闭包间接调用 MessageHandler。

**出站**：
- `_dispatch_loop` — 从 `MessageHandler.dequeue_outbound()` 取出每条 Message，查 `channel_id` 找到对应 Channel，调 `channel.send()`
- 出站 Queue 在 MessageHandler 内部（`_robot_messages`），不是 ChannelManager 的——ChannelManager 只是消费方

这是**单线程异步 dispatch**，不是多线程，所以没有锁。Queue + asyncio task 的模式简单可靠。

### 3.3 MessageHandler — 格式转换 + 出站 Queue

[message_handler.py](../twinkle/gateway/message_handler.py) 只依赖 AgentClient，不依赖 ChannelManager。它做两件事：

**入站转换**（Message → E2AEnvelope）：
```python
envelope = E2AEnvelope(
    request_id=msg.id,
    channel=msg.channel_id,
    session_id=msg.session_id,
    method=msg.method,
    params=msg.params,
)
```

**出站转换**（E2AResponse → Message → Queue）——按 `response_kind` 分支：
- `e2a.todo_update` → `Message(event_type=todo.update, payload=body)`（结构化快照 `{tasks, remaining, total}`）→ `enqueue_outbound(msg)`
- 否则按 `is_final`：每个 chunk → `Message(event_type=chat.delta, content=chunk_content)` → `enqueue_outbound(msg)`
- 终止帧 → `Message(event_type=chat.final, content=final_content)` → `enqueue_outbound(msg)`
- 错误 → `Message(event_type=chat.final, content="[error] ...")` → `enqueue_outbound(msg)`
- 流异常（传输/代码层异常，非 `e2a.error` 帧）→ 同样发终止 `chat.final`（content=`[error] {exc}`），确保浏览器不卡在 busy 态

出站消息不直接交给 ChannelManager——而是投进自己的 `_robot_messages` Queue，由 ChannelManager 的 `_dispatch_loop` 通过 `dequeue_outbound()` 消费。这是对齐 jiuwenclaw 的单向依赖模式。

`_process_stream` 是 fire-and-forget task（`asyncio.create_task`），不阻塞下一个请求；整个 `async for` 包在 try/except 里，异常时发终止 `chat.final`（`[error] {exc}`）兜底，与协议层 `e2a.error` 帧是两条不同错误通路。

### 3.4 AgentClient — ws 客户端 + demux

[agent_client.py](../twinkle/gateway/agent_client.py) 是 Gateway 连 AgentServer 的 ws 客户端：

**连接**：
- `connect()` — 建立 ws 连接，先 `recv()` 消费 `connection.ack` 帧，再启动 `_recv_loop`（`ping_interval=30` / `ping_timeout=300` / `max_size=8MiB`；维护 `_ready: asyncio.Event` 与 `ready` 属性；`close()` 取消 recv task 并关 ws）

**demux**（解多路复用 — 关键机制）：

**demux** 是 **de-multiplexing**（解多路复用）的缩写：把一条共享通道上混在一起的多个流，按标识重新分开投递到各自的消费者。

在 Twinkle 里，Gateway 和 AgentServer之间只有**一条 ws 连接**。如果用户快速发了两条消息（r1 和 r2），两个请求的响应帧会交错出现在同一条 ws 上：

```
一条 WebSocket 连接（共享通道）
    │
    │  上面同时跑着多个请求的响应帧：
    │  request_id="r1" 的 chunk 0
    │  request_id="r2" 的 chunk 0
    │  request_id="r1" 的 chunk 1
    │  request_id="r2" 的 final
    │  request_id="r1" 的 final
    │
    ▼  _recv_loop 按 request_id 分开投递：
    │
    ├── Queue["r1"] ← 只收 request_id=="r1" 的帧
    │     send_request_stream yield 直到 is_final
    │
    └── Queue["r2"] ← 只收 request_id=="r2" 的帧
          send_request_stream yield 直到 is_final
```

对应的代码：

```python
# recv_loop：持续读 ws 帧，按 request_id 分投
async def _recv_loop(self) -> None:
    async for raw in self._ws:
        data = json.loads(raw)
        rid = data.get("request_id")
        q = self._queues.get(rid)      # ← demux：按 rid 找到对应的 Queue
        if q is not None:
            await q.put(data)           # ← 投进该请求的专属 Queue

# send_request_stream：为某个请求创建专属 Queue，从中取帧
async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
    rid = envelope.request_id
    q: asyncio.Queue = asyncio.Queue()
    self._queues[rid] = q              # ← 注册：告诉 recv_loop 这个 rid 的帧投到这里
    await self._send(envelope)
    try:
        while True:
            data = await q.get()        # ← 只取属于这个请求的帧
            resp = E2AResponse.model_validate(data)
            yield resp
            if resp.is_final:
                break
    finally:
        self._queues.pop(rid, None)    # ← 流结束移除该 rid，防 demux 表无限增长
```

**为什么需要 demux**？没有 demux，一条 ws 只能同时处理一个请求——前一个完成之前不能发下一个。有了 demux，一条 ws 就能并发处理多个请求，每个有自己的 Queue 互不干扰。

类比：就像邮局把混在一起的信件按收件人分拣投到各自的信箱——信件（ws 帧）混在一条运输线上，但到了邮局（`_recv_loop`）就被按标识（`request_id`）分开投递到各信箱（`asyncio.Queue`）。

**发送**：
- `_send_lock` 保护 ws 写操作，避免两个并发请求的帧交叉发送

---

## 4. AgentServer 内部组件

AgentServer 的核心是 `AgentLoop`，但 `server.py` 是 ws 接口层：

```
                Gateway ws 连入
                      │
                      ▼
              ┌── server.py handler ──┐
              │  发 connection.ack     │
              │  解析 E2AEnvelope      │
              │  分发到 AgentLoop      │
              │  把 E2AResponse 发回   │
              └───┬──────────────────┘
                  │ loop.run_stream(env)
                  ▼
          ┌── AgentLoop ──────────┐
          │  ReAct: think → tool  │
          │         → result →     │
          │         re-decide      │
          │                        │
          │  yield E2AResponse     │  每个 TextDelta → e2a.chunk；工具执行后 → e2a.todo_update
          │  最终 → e2a.complete   │  或 e2a.error（超过 max_steps）
          └───┬────────────────────┘
              │
    ┌─────────┼───────────┐
    │         │           │
    ▼         ▼           ▼
LLMClient  SessionStore  ToolManager
(流式API)  (对话记忆)    (含写入/执行工具)
```

### 4.1 server.py — ws 接口 + AgentLoop 分发

[server.py](../twinkle/agentserver/server.py) 的 `handler` 流程：

1. 新连接 → 发 `connection.ack` 帧（非 E2A 格式，是普通 event）
2. 循环收帧 → `E2AEnvelope.model_validate_json(raw)` 解析
3. 解析失败 → 发 `e2a.error` **终止帧**（`is_final=true`）
4. 解析成功 → `async for frame in loop.run_stream(env): await _safe_send(ws, frame)`
5. AgentLoop 异常 → 发 `e2a.error` **终止帧**（`is_final=true`）

> 错误帧必须显式置 `is_final=true`——`E2AResponse.is_final` 默认 `false`，不置则 gateway demux 不终止、请求挂起。`_safe_send` 静默吞掉 `ConnectionClosed`（客户端断连是正常生命周期事件）。`ws_handler(loop, store)` 让测试注入假 loop + 共享 `SessionStore`，`agent_loop()` 用真实配置组建。

### 4.2 AgentLoop — ReAct 核心闭环

[agent_loop.py](../twinkle/agentserver/agent_loop.py) 是整个系统的核心算法：

```
入口：set plan-todo ContextVar → reset_todo_events → [会话首次] store.append(TODO_SYSTEM_PROMPT)
用户 query → store.append(user) → memory.recall(stub空) → msgs = store.get_messages()
    │
    ▼  ReAct 循环（max_steps 守护）
    │
    ├── llm.stream(msgs, tools)  # 返回 TextDelta | Finish
    │   ├── TextDelta → yield E2AResponse(e2a.chunk)
    │   └── Finish → store.append(assistant)
    │       ├── finish_reason=="tool_calls" → 执行工具 → drain_todo_events
    │       │     → [若有快照] yield e2a.todo_update → store.append(tool) → continue
    │       └── finish_reason=="stop" → yield e2a.complete → return
    │
    └── 超过 max_steps → yield e2a.error(is_final=true) → 生成器正常返回（非异常）
```

关键设计：
- `run_stream` 是 **async generator**，yield E2AResponse — loop 对 ws 零依赖，单测无需起 ws
- 工具结果回灌是命门：`{role:"tool", tool_call_id, content:result}` append 进 store，下一轮 `get_messages` 自然带上
- `max_steps` 防止工具循环不收敛；触顶是"正常 yield `e2a.error` 后返回"，**非异常**——异常才走 §4.1 步骤 5
- 入口设 plan-todo ContextVar + `reset_todo_events`，会话首次插入 `TODO_SYSTEM_PROMPT`；工具执行后 `drain_todo_events` 产 `e2a.todo_update` 侧信道（结构化快照 `{tasks, remaining, total}`）

### 4.3 SessionStore — 会话记忆（磁盘落盘 + 内存缓存）

[session_store.py](../twinkle/agentserver/session_store.py) 是**磁盘 + 内存两层**的会话存储，存的是 OpenAI 原生 `messages` 格式：内存缓存 `dict[session_id, list[msg]]` 服务热读，磁盘 `<SESSIONS_DIR>/<sid>/{metadata.json,history.json}` 服务持久化与历史回看。

- `append(session_id, message, request_id, event_type)` — async；更新缓存 + 追加 `history.json` + 更新 metadata（首条 user 消息自动起 title）
- `get_messages(session_id)` — sync；缓存命中直接返，未命中从 `history.json` 冷启动 hydrate（保留完整 `tool_calls`/`tool_call_id` 以重建 ReAct 上下文）
- `create_session` / `delete_session` — async；幂等建/删会话目录
- `list_sessions` / `get_history` — sync；列表按 `last_message_at` 倒序，history 返回 raw 记录
- `list_files(session_id)` — sync；返回会话目录扁平文件列表 `[{name, is_dir, size}]`，未知会话返回 `[]`
- `read_file(session_id, name)` — sync；路径安全地读取会话目录内的单文件文本内容（拒绝非裸文件名与路径穿越）

详见 §4.7。

### 4.4 LLMClient — 模型流式接口

[llm_client.py](../twinkle/agentserver/llm_client.py) 是 OpenAI SDK 薄封装，`base_url` 可配兼容任意端点。`stream()` 返回 `AsyncIterator[TextDelta | Finish]`（`Finish` 携带 `finish_reason` 与 `assistant_message`，`tool_calls` 累积其中，`finish_reason=="tool_calls"` 表示需执行工具；另以 `stream_options.include_usage` 捕获 token `usage`）。

### 4.5 ToolManager — 四层工具系统（Phase 2）

[twinkle/agentserver/tools/](../twinkle/agentserver/tools/) 重写为 openjiuwen 风格四层：

- `base.py` — `ToolCard`（纯元数据：name/description/parameters）+ `Tool`（Protocol 接口：card + invoke）
- `local_function.py` — `LocalFunction`（本地 Python 函数这一种 Tool 实现）
- `decorator.py` — `@tool` 装饰器：函数 + docstring + 签名自动抽 schema 产 LocalFunction
- `schema_extractor.py` — 最小手写抽取器（str/int/float/bool/list/dict/Optional/`X | None`（PEP 604） → JSON schema）
- `manager.py` — `ToolManager`：register/unregister/list/get/schemas/execute，存 `dict[str, Tool]`，只认 Tool 接口

agent_loop 调用面 `self._tools.schemas()` / `self._tools.execute(name, args)` 不变——ToolManager 是旧实现的超集。

具体工具位于 `builtin/` 子包（`web_fetch` / `web_search` / `command_exec` / `file_tools` / `todo_tools`），`__init__.py` 的 `tool_manager()` 预注册全部 builtin 工具。**新增工具**：在 `builtin/` 下加 `*_tools.py` 写 `@tool` 函数，于 `tool_manager()` 中 `tm.register(it)`，agent_loop 经 `schemas()` / `execute()` 自动接入，无需改 loop。

### 4.6 LongTermMemory — stub

[memory.py](../twinkle/agentserver/memory.py) 是空实现：`recall()` 返回空列表，`store()` 不做事。接口形状钉死，将来换真实现不回炉。

### 4.7 Observability — OTel 遥测切面

[twinkle/observability/](../twinkle/observability/) 是 agentserver 的 in-tree 可观测模块，用 OpenTelemetry 采集 ReAct 运行的 traces + metrics，经 OTLP/gRPC 导出到 env 配置的 collector。**业务代码零插桩调用**——启动时 monkey-patch 三个 choke point 施加，依赖方向单向 `observability → agentserver`（observability import agentserver 的类来打补丁），agentserver 永不 import observability。

启动：[`agentserver/__main__.py`](../twinkle/agentserver/__main__.py) 在 `asyncio.run(main())` 前接入 `twinkle.observability.setup()`（进程入口装配，非业务插桩）。`setup()` 读 env，`OTEL_ENABLED=false`（默认）→ 零成本 no-op（不 patch、不造 provider）；`true` 则造 TracerProvider + MeterProvider 并 patch 三个 choke point。幂等 + fail-soft（全 try/except，失败 log 不抛进业务）。

三个插桩点各产一个 span，组成单请求 trace 树：

```
twinkle.agent.invoke  (root, INTERNAL)  ← AgentLoop.run_stream
├─ gen_ai.chat                          ← LLMClient.stream（每个 loop step 一个）
├─ gen_ai.tool                          ← ToolManager.execute（若有工具调用）
└─ gen_ai.chat                          ← 工具结果回灌后再问模型
```

- **`AgentLoop.run_stream`** → `twinkle.agent.invoke`（root）：`twinkle.request.id` / `twinkle.session.id`（取自 envelope）、`twinkle.agent.iterations`（ContextVar 计数）、`twinkle.agent.status`(succeeded/failed)、总时延。failed 有两条路径：`run_stream` 抛异常 → status=failed + span ERROR + 重抛；或正常 yield 了终止错误帧（`e2a.error` / `status=="failed"`，如撞 `max_steps`）后正常 return——此路径**无异常**，wrapper 在 yield 循环内检测该帧也置 failed（否则会被误标 succeeded）。
- **`LLMClient.stream`** → `gen_ai.chat`：`gen_ai.request.model`、`gen_ai.response.finish_reason`、`gen_ai.usage.{input,output,total}_tokens`、TTFT；收到 `Finish` 时**先 end span 再 yield**（防调用方末轮 return 致 span 不导出）。
- **`ToolManager.execute`** → `gen_ai.tool`：`gen_ai.tool.name` / `gen_ai.tool.error` / `gen_ai.tool.arguments` / `gen_ai.tool.result`。`ToolManager.execute` 内部 catch 工具异常返回 `[tool error] ...` 串（不抛），故 span status 恒 OK，`tool.error` 由检测返回串的 `[tool error]` 前缀置 true。

metrics（全 fail-soft，`Metrics(None)` 静默 no-op）：counter `gen_ai.client.token.usage`（input/output/total）、`gen_ai.tool.count`；histogram `gen_ai.client.operation.duration`、`gen_ai.tool.duration`、`twinkle.agent.duration`。属性遵循 OTel GenAI semconv（`gen_ai.*`）+ 自定义维度（`twinkle.*`）。

| 文件 | 职责 |
|---|---|
| `__init__.py` | `setup()` 单入口：config → provider → apply_instrumentors；幂等 + fail-soft |
| `config.py` | `ObservabilityConfig` + `load_config()`，env 驱动默认关 |
| `provider.py` | `init_providers(cfg)` → (tracer, meter)：TracerProvider + MeterProvider over `Resource(service.name)`；OTLP gRPC / console / none；**不设全局 provider**（参数传，测试无污染） |
| `attributes.py` | 属性键常量：`gen_ai.*`（对齐 GenAI semconv）+ `twinkle.*` |
| `context.py` | `RequestContext` ContextVar（request_id/session_id/agent_name）+ `_llm_call_counter` |
| `metrics.py` | `Metrics` 类：5 个 instrument + fail-soft `record_*` |
| `usage.py` | `read_usage_token()`：统一读 token，兼容 dict（测试）与 pydantic `CompletionUsage`（真 SDK） |
| `wrap.py` | `patch_method(cls, name, factory)`：幂等 + fail-soft |
| `instrumentors/` | `apply_instrumentors(...)`：每个 instrumentor 独立 try/except；生产传 `None` 懒加载真类、测试传 fake |

依赖放 `[obs]` optional extra（`opentelemetry-api` / `-sdk` / `-exporter-otlp-proto-grpc`），`pip install -e ".[obs]"` 才装；不装时 try-import 守卫跳过。借鉴 `jiuwenswarm-instrumentor`（OTel auto-instrumentation），砍跨进程 W3C / context-token 分桶 / CLI wrapper / logs，是其最小子集 + 学习重写。

### 4.8 会话持久化与 history RPC

> **超出 roadmap 的有意扩展**：`roadmap.md` 原定 Phase 1/2 "不做落盘持久化"。本节描述的 session management 是在 roadmap 之外有意补回的能力——为了支持跨重启的对话延续与历史回看。设计与实施记录在 [spec](../superpowers/specs/2026-07-22-session-management-design.md) 与 [plan](../superpowers/plans/2026-07-22-session-management.md)。

`SessionStore` 从纯内存 dict 升级为**磁盘落盘 + 内存缓存**两层结构，`session_rpc.py` 在 AgentServer 侧分发六个会话/历史/文件 RPC，`session_id` 由浏览器生成并 sticky 在 `localStorage`。

**每会话磁盘布局**（根目录 `SESSIONS_DIR`，见 `config.py`）：

```
<SESSIONS_DIR>/<session_id>/
    metadata.json   # {session_id, title, created_at, last_message_at, message_count, channel_id}
    history.json    # JSONL，每条 append 的消息一行
```

- `history.json` 每行一条记录，**保留完整 OpenAI 原生字段**（`role`/`content`/`tool_calls`/`tool_call_id`），以便冷启动时无损重建 ReAct 上下文（system prompt、tool_calls、tool 结果都能还原）。首条 user 消息自动生成 `title`。
- 内存缓存 `dict[sid -> list[OpenAI msg]]` 服务 AgentLoop 的热读；`get_messages` 在缓存未命中时从 `history.json` 冷启动 hydrate。坏 JSONL 行跳过不抛错；`metadata.json` 损坏时 fallback 到目录 mtime。
- `append`/`create_session`/`delete_session` 是 async（持一把 `asyncio.Lock` 串行化 metadata 的 read-modify-write）；`get_messages`/`list_sessions`/`get_history`/`list_files`/`read_file` 是 sync。

**六个 RPC** 在 [session_rpc.py](../twinkle/agentserver/session_rpc.py) 的 `dispatch_session_rpc(envelope, store)` 中分发，被 `server.py` 的 `ws_handler(loop, store)` 在进入 AgentLoop 之前路由：

| method | 动作 | body |
|---|---|---|
| `session.create` | 幂等建会话目录 + metadata | `{type:"session.create", session_id}` |
| `session.list` | 列会话（按 `last_message_at` 倒序） | `{type:"session.list", sessions:[...]}` |
| `session.delete` | 删会话目录 + 清缓存 | `{type:"session.delete", session_id}` |
| `history.get` | 返回该会话 raw history 记录 | `{type:"history.get", messages:[...]}` |
| `session.files` | 列该会话目录的**扁平**文件列表 | `{type:"session.files", files:[{name,is_dir,size}]}` |
| `file.read` | 路径安全地读取会话目录内的单文件内容 | `{type:"file.read", name, content:<str>}` |

每个 RPC **只产一帧** `E2AResponse(response_kind="e2a.result", is_final=true, sequence=0)`；失败时产 `status="failed"` 的 result 帧（body 带 `error`），让前端 `request()` 干净地 reject。Gateway 的 `MessageHandler._process_stream` 把 `e2a.result` 映射为浏览器 `result` event（单帧，无流式分片）。

**文件浏览 RPC 的路径安全**：`file.read` 的 `name` 参数来自浏览器且内容会回显到前端预览区，因此 `SessionStore.read_file` 拒绝任何非裸文件名的请求——空串、含 `/` 或 `\`、`.`/`..` 均抛 `ValueError`；路径 resolve 后必须留在会话目录内（`base in target.parents`），否则也抛 `ValueError`。这是 load-bearing 安全约束，由显式单元测试覆盖。`list_files` 返回会话目录顶层条目的扁平列表 `[{name, is_dir, size}]`（当前会话目录无子目录，无需递归），未知会话返回空列表。

**前端导航壳**：App.vue 从 3 列永远可见布局（SessionSidebar | ChatPanel | TodoPanel）重构为 jiuwen 式导航壳——窄 `LeftNav`（聊条 + 会话两个入口）切换 `useSessions.activeNav` composable 字段，内容区 `v-if` 在 `ChatView`（ChatPanel + TodoPanel）与 `SessionsView`（3 栏文件浏览器）之间切换。旧的 `SessionSidebar.vue` 已删除。设计与实施记录在 [sessions-page spec](../superpowers/specs/2026-07-22-sessions-page-design.md) 与 [plan](../superpowers/plans/2026-07-22-sessions-page.md)。

**SessionsView 3 栏布局**：`SessionListPane | FileTreePane | FilePreviewPane`（grid 1fr : 1fr : 3fr，与 jiuwen 对齐）。`SessionListPane` 展示历史会话列表，选中后调用 `loadSessionFiles(sid)` 拉取文件列表；`FileTreePane` 展示扁平文件列表，点击调用 `readSessionFile(sid, name)`；`FilePreviewPane` 按文件名分派渲染——`history.json` 提供「聊天气泡 / 原始 JSON」切换（气泡模式复用 `fromHistory` composable），`metadata.json` 格式化 JSON `<pre>`，其余文件纯文本 `<pre>`。「↩ 恢复」按钮执行 `restoreSession(sid)`：加载聊天历史 + 切回 `ChatView`。

**session_id 的归属**：`session_id` 由浏览器 `webClient.ts` 生成（`'sess_' + crypto.randomUUID()`）并 sticky 存在 `localStorage`；贯穿 `req.params.session_id` → `E2AEnvelope.session_id` → `SessionStore`。AgentServer 自己不生成 session_id，只接收并落盘。

---

## 5. Channel 机制

### 5.1 什么是 Channel？

Channel 是 Twinkle 中**用户接入通道**的抽象。每个 Channel 是一种"用户连接方式"（当前只有 Web），有自己的：
- `channel_id` — 路由标识（如 `"web"`）
- 入站解析 — 把该通道的原始格式转为 `Message`
- 出站发送 — 把 `Message` 转回该通道的原生格式广播

### 5.2 Channel 接口约定

所有 Channel 实现须满足：

| 方法 | 说明 |
|---|---|
| `channel_id` 属性 | 路由标识（str） |
| `on_message(cb)` | 注册入站回调 `cb(Message) → Awaitable[bool]` |
| `send(msg)` | 出站：把 Message 转为该通道原生格式发送 |
| `start()` | 启动通道（通常是起 ws server） |

### 5.3 WebChannel 当前实现

WebChannel 是唯一实现：

```
浏览器 JSON ──→ WebChannel._handle_raw ──→ Message ──→ on_message 回调
                    │
                    └─ 立即回 ACK {type:"res", id, ok:true}

Message(event) ──→ WebChannel.send ──→ {type:"event", event, payload, request_id}
                    │
                    └─ 广播给 _clients set 中所有 ws 连接
```

### 5.4 Channel 如何注册和路由

在 `__main__.py` 中：

```python
message_handler = MessageHandler(agent_client)        # ← 只依赖 AgentClient
channel_manager = ChannelManager(message_handler)      # ← 只依赖 MessageHandler（单向）
channel_manager.register_channel(web_channel)          # 按 "web" 注册
```

`register_channel` 内部做了两件事：
1. 存进 `_channels["web"]`
2. 给 channel 注册 `on_message` 回调 → 闭包调 `MessageHandler.handle_message`

出站路由：`ChannelManager._dispatch_loop` 从 `MessageHandler.dequeue_outbound()` 取 Message，按 `msg.channel_id` 查 `_channels` dict，调对应 channel 的 `send()`。

### 5.5 将来扩展

如果要加飞书 Channel，只需：
1. 实现 `FeishuChannel(channel_id="feishu")`，满足 Channel 接口约定
2. 在 `__main__.py` 中 `channel_manager.register_channel(feishu_channel)`
3. **Gateway 其他组件（MessageHandler / ChannelManager / AgentClient）零改动**

这正是 Channel 抽象的价值——新增通道只在边缘层扩展，核心不动。

---

## 6. 消息格式详解

### 6.1 浏览器 ↔ Gateway 格式

浏览器和 Gateway 之间有三种帧类型：`req`、`res`、`event`。

#### req — 请求（浏览器 → Gateway）

```json
{
  "type": "req",
  "id": "req_m1abc_0",          // request_id，浏览器造，贯穿全链路
  "method": "chat.send",         // RPC 方法名
  "params": {
    "query": "你好",             // 用户输入
    "session_id": "sess_xxx"     // 浏览器生成的会话 id
  }
}
```

- `id` 由浏览器 `webClient.ts` 生成：`'req_' + Date.now().toString(36) + '_' + seq.toString(36)`
- `session_id` 由浏览器在连接时生成：`'sess_' + crypto.randomUUID()`
- 当前只用 `chat.send` method

#### res — 立即 ACK（Gateway → 浏览器）

```json
{
  "type": "res",
  "id": "req_m1abc_0",          // 回显 request_id
  "ok": true,
  "payload": {
    "accepted": true,
    "session_id": "sess_xxx"
  }
}
```

Gateway 收到 req 后**立刻**回 ACK，不等 Agent 响应。浏览器据此知道请求已被接受。

#### event — 流式事件（Gateway → 浏览器，广播）

```json
// chat.delta — 逐字分片
{
  "type": "event",
  "event": "chat.delta",
  "payload": { "content": "你" },
  "request_id": "req_m1abc_0"
}

// chat.final — 终止
{
  "type": "event",
  "event": "chat.final",
  "payload": { "content": "你好世界" },
  "request_id": "req_m1abc_0"
}

// todo.update — Todo 快照（结构化，整体替换，不走 content）
{
  "type": "event",
  "event": "todo.update",
  "payload": {
    "tasks": [{"idx":1,"title":"...","status":"completed","result":"done"}],
    "remaining": 1,
    "total": 2
  },
  "request_id": "req_m1abc_0"
}
```

- `chat.delta` — 每帧一个字/词片段，浏览器拼接到上一个 assistant 消息
- `chat.final` — 最终完整内容，浏览器用此标记对话结束
- `todo.update` — agent 自规划时推送的 Todo 结构化快照，浏览器整体替换右侧面板（不走 `content`）
- `request_id` — 浏览器据此关联 delta 和 final 属于哪个请求
- **广播给所有连接**：WebChannel.send 对 `_clients` set 中所有 ws 连接做 `gather` 广播

#### connection.ack — 连接握手（Gateway → 浏览器）

```json
{
  "type": "event",
  "event": "connection.ack",
  "payload": { "status": "ready" }
}
```

浏览器 ws 连上后，Gateway 先发此帧，`webClient.ts` 消费它后设 `connected=true`。

### 6.2 Gateway ↔ AgentServer 格式（E2A）

E2A 是 Gateway 和 AgentServer 之间的**内部信封协议**。定义在 [twinkle/e2a/models.py](../twinkle/e2a/models.py)，是 jiuwenclaw E2A 的最小子集。

#### E2AEnvelope — 请求（Gateway → AgentServer）

```json
{
  "protocol_version": "1.0",
  "request_id": "req_m1abc_0",
  "channel": "web",
  "session_id": "sess_xxx",
  "method": "chat.send",
  "params": { "query": "你好" },
  "timestamp": 0.0
}
```

**7 个核心字段**（无 `is_stream` —— 流式专用，所有请求隐式 streaming；`timestamp` 当前恒 `0.0`、未启用）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `protocol_version` | str | 固定 `"1.0"` |
| `request_id` | str | 贯穿全链路的请求 id，浏览器造 |
| `channel` | str | 入口通道名，默认 `"web"` |
| `session_id` | str/null | 会话 id，浏览器造，跨轮对话关联 |
| `method` | str | 网关 RPC 方法名，当前只用 `chat.send` |
| `params` | dict | 唯一业务参数字典 |
| `timestamp` | float | 请求时刻 |

#### E2AResponse — 响应（AgentServer → Gateway，流式多帧）

```json
// chunk 0
{
  "protocol_version": "1.0",
  "request_id": "req_m1abc_0",
  "sequence": 0,
  "is_final": false,
  "status": "in_progress",
  "response_kind": "e2a.chunk",
  "body": { "result": { "content": "你" } },
  "is_stream": true
}

// chunk 1
{
  "protocol_version": "1.0",
  "request_id": "req_m1abc_0",
  "sequence": 1,
  "is_final": false,
  "status": "in_progress",
  "response_kind": "e2a.chunk",
  "body": { "result": { "content": "好" } },
  "is_stream": true
}

// final
{
  "protocol_version": "1.0",
  "request_id": "req_m1abc_0",
  "sequence": 2,
  "is_final": true,
  "status": "succeeded",
  "response_kind": "e2a.complete",
  "body": { "result": { "content": "你好世界" } },
  "is_stream": true
}
```

**8 个核心字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `protocol_version` | str | 固定 `"1.0"` |
| `request_id` | str | 回显请求 id |
| `sequence` | int | 同 request_id 下从 0 严格递增 |
| `is_final` | bool | 最后一帧 `true` |
| `status` | str | `in_progress` / `succeeded` / `failed` |
| `response_kind` | str | `e2a.chunk` / `e2a.complete` / `e2a.error` / `e2a.todo_update` / `e2a.result` |
| `body` | dict | 载荷内容 |
| `is_stream` | bool | 固定 `true`（Twinkle 流式专用） |

**五种 response_kind**（chunk/complete/error/todo_update/result）：

| response_kind | 含义 | body 结构 |
|---|---|---|
| `e2a.chunk` | 流式分片 | `{result: {content: "字片段"}}` |
| `e2a.complete` | 正常终止 | `{result: {content: "完整文本"}}` |
| `e2a.error` | 错误终止 | `{error: "错误描述"}` |
| `e2a.todo_update` | Todo 快照 | `{tasks, remaining, total}` |
| `e2a.result` | 单帧 RPC 结果（session/history） | `{type: "session.*"|"history.get", ...}` |

#### connection.ack — 连接握手（AgentServer → Gateway）

```json
{"type":"event","event":"connection.ack","payload":{"status":"ready"}}
```

**不是 E2A 格式**，是普通 event 帧。AgentClient 在 `connect()` 中先 `recv()` 消费它，再启动 demux 循环。

### 6.3 Gateway 内部 Message 格式

`Message` 是 Gateway 内部的统一数据结构，定义在 [schema/message.py](../twinkle/schema/message.py)：

```python
@dataclass
class Message:
    id: str                              # request_id
    type: str = "req"                    # "req" 或 "event"
    channel_id: str = "web"              # 路由标识
    session_id: str | None = None        # 会话 id
    method: str = "chat.send"            # 方法名
    params: dict[str, Any]               # 参数
    event_type: EventType | None = None  # chat.delta / chat.final / todo.update / connection.ack
    payload: dict[str, Any]              # 附加载荷
    ok: bool = True                      # ACK 成功标志
    content: str = ""                    # 文本内容
```

`EventType` 取值：

| EventType | 说明 |
|---|---|
| `connection.ack` | 连接就绪 |
| `chat.delta` | 流式分片 |
| `chat.final` | 终止帧 |
| `todo.update` | Todo 快照（结构化 payload） |

Message 在 Gateway 内流转时不带 `is_stream` 字段——所有请求隐式流式。

---

## 7. 数据流全轨迹（一次 chat.send）

下面是一个完整的请求-响应轨迹，标注每个帧的格式：

```
① 浏览器发 req
   WebClient.send('chat.send', {query:'你好'})
   → ws 发 JSON: {type:"req", id:"req_m1abc_0", method:"chat.send",
                  params:{query:"你好", session_id:"sess_xxx"}}
                      │
                      ▼
② WebChannel._handle_raw 收 req
   → 建 Message(id="req_m1abc_0", method="chat.send", ...)
   → 立回 ACK JSON: {type:"res", id:"req_m1abc_0", ok:true, payload:{accepted:true, session_id:"sess_xxx"}}
   → 调 on_message 回调 → ChannelManager → MessageHandler
                      │
                      ▼
③ MessageHandler.handle_message
   → 建 E2AEnvelope(request_id="req_m1abc_0", channel="web",
                    method="chat.send", params={query:"你好"})
   → asyncio.create_task(_process_stream)
                      │
                      ▼
④ AgentClient.send_request_stream
   → ws 发 E2AEnvelope JSON: {protocol_version:"1.0", request_id:"req_m1abc_0", ...}
                      │
                      ▼
⑤ AgentServer handler 收
   → E2AEnvelope.model_validate_json(raw)
   → async for frame in loop.run_stream(envelope):
                      │
                      ▼
⑥ AgentLoop.run_stream
   → store.append(session_id, {role:"user", content:"你好"})
   → llm.stream → yield TextDelta("你")
   → yield E2AResponse(request_id="req_m1abc_0", sequence=0,
                        is_final=false, response_kind="e2a.chunk",
                        body={result:{content:"你"}})
                      │
                      ▼
⑦ AgentServer _safe_send → ws 发 E2AResponse JSON
                      │
                      ▼
⑧ AgentClient._recv_loop → 按 request_id 投递到 asyncio.Queue
   → send_request_stream yield E2AResponse
                      │
                      ▼
⑨ MessageHandler._process_stream
   → content = "你"
   → event_type = chat.delta（因为 is_final=false）
   → 建 Message(id="req_m1abc_0", event_type=chat.delta, content="你")
   → enqueue_outbound(msg) → _robot_messages Queue.put
                      │
                      ▼
⑩ ChannelManager._dispatch_loop
   → dequeue_outbound(msg) → _robot_messages Queue.get
   → 查 channel_id="web" → WebChannel.send(msg)
                      │
                      ▼
⑪ WebChannel.send → 广播给所有浏览器 ws:
   JSON: {type:"event", event:"chat.delta", payload:{content:"你"},
          request_id:"req_m1abc_0"}
                      │
                      ▼
⑫ 浏览器 webClient.handle
   → chat.delta → onDelta("你", "req_m1abc_0")
   → App.vue 拼接到上一个 assistant bubble

   ...（更多 delta 帧重复 ⑥→⑫）...

   ⑥ final: E2AResponse(is_final=true, response_kind="e2a.complete",
                         body={result:{content:"你好世界"}})
   ⑨ event_type = chat.final
   ⑪ JSON: {type:"event", event:"chat.final", payload:{content:"你好世界"}, ...}
   ⑫ onFinal("你好世界", "req_m1abc_0") → 结束
```

### request_id 的贯穿链

```
浏览器造 "req_m1abc_0"
  → req JSON 的 id
  → Message 的 id
  → E2AEnvelope 的 request_id
  → E2AResponse 的 request_id
  → Message 的 id（出站）
  → event JSON 的 request_id
  → 浏览器 onDelta/onFinal 的 requestId 参数
```

这个 id 是**流式分片关联的命脉**——浏览器靠它知道哪些 delta 和哪个 final 属于同一个请求。

---

## 8. 前端架构

### 8.1 技术栈

- **Vite** (:5173) — dev server，代理 `/ws` 到 Gateway(:19000)
- **Vue 3** + TypeScript — UI 框架
- **webClient.ts** — ws 客户端，发 req、收 event、按 request_id 拼 delta/final，并把 `todo.update` 结构化快照交给侧栏面板

### 8.2 Vite ws 代理

[vite.config.ts](../web/vite.config.ts) 在 dev 模式下把 `/ws` 代理到 `ws://127.0.0.1:19000`：

```typescript
proxy: {
  '/ws': {
    target: 'ws://127.0.0.1:19000',
    ws: true,
  },
},
```

这保证浏览器同源（`ws://localhost:5173/ws`），免 CORS 问题。生产部署需要另行配置。

### 8.3 webClient.ts

[webClient.ts](../web/src/services/webClient.ts) 的核心逻辑：

```
connect(onReady)
  → ws = new WebSocket(`${proto}://${location.host}/ws`)
  → 连上后造 session_id = 'sess_' + crypto.randomUUID()
  → onReady()

send(method, params)
  → 造 id = 'req_' + Date.now().toString(36) + '_' + seq++
  → ws.send JSON: {type:"req", id, method, params:{...params, session_id}}
  → return id

handle(frame)
  → connection.ack → ignore
  → res → ignore（立即 ACK，不展示）
  → event.chat.delta → onDelta(content, request_id)
  → event.chat.final → onFinal(content, request_id)
  → event.todo.update → onTodoUpdate(payload{tasks,remaining,total}, request_id)

setHandlers(onDelta, onFinal, onTodoUpdate) — 注册三个回调；todo.update 的 payload 是结构化快照，不经 content
```

### 8.4 导航壳与 App.vue

[App.vue](../web/src/App.vue) 从 3 列永远可见布局重构为 jiuwen 式导航壳：

- `LeftNav`（窄左栏，聊条 + 会话两个入口）→ `setNav(key)` 切换 `useSessions.activeNav`
- 内容区 `v-if="activeNav === 'chat'" → ChatView`（ChatPanel + TodoPanel），`v-else → SessionsView`（3 栏文件浏览器）
- 旧 `SessionSidebar.vue` 已删除——会话列表只存在于 Sessions 页面

`ChatPanel` 输入栏左侧新增 ➕ 「新对话」按钮（调用 `createSession()`），取代旧 sidebar 的「+ 新对话」入口。

---

## 9. 启动与配置

### 9.1 启动

```bash
# 方式一：脚本一键起
python scripts/start_services.py

# 方式二：分两个终端
python -m twinkle.agentserver    # 起执行核心(:18000)
python -m twinkle.gateway        # 起连接边缘(:19000)

# 方式三：起前端
cd web && npm install && npm run dev   # Vite(:5173)
```

### 9.2 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TWINKLE_AGENTSERVER_HOST` | `127.0.0.1` | AgentServer 监听地址 |
| `TWINKLE_AGENTSERVER_PORT` | `18000` | AgentServer 监听端口 |
| `TWINKLE_GATEWAY_HOST` | `127.0.0.1` | Gateway 浏览器 ws 地址 |
| `TWINKLE_GATEWAY_PORT` | `19000` | Gateway 浏览器 ws 端口 |
| `TWINKLE_LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API 端点 |
| `TWINKLE_LLM_API_KEY` | 空 | LLM API key（**放在 .env 文件，不暴露在环境变量中**） |
| `TWINKLE_LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `TWINKLE_WORKSPACE_DIR` | `~/.twinkle` | `command_exec`/`file_tools` 的工作区根（agent 文件操作收敛其下）。默认用户家,生成物不污染仓库;可覆盖 |
| `TWINKLE_AGENT_MAX_STEPS` | `1000` | ReAct 循环最大步数，超限 yield `e2a.error`（防不收敛的硬上限，非目标值） |

**Observability 配置**（读自 `twinkle/observability/config.py`，默认全关 = 零成本 no-op；装 `[obs]` extra 才生效）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `OTEL_ENABLED` | `false` | false → `setup()` 直接返回，不 patch、不造 provider |
| `OTEL_TRACES_EXPORTER` | `none` | `otlp` / `console` / `none` |
| `OTEL_METRICS_EXPORTER` | `none` | 同上；none → `Metrics(None)` 静默 no-op |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | 当前仅 gRPC exporter 落地 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 | `http://` → 明文 gRPC(insecure)，`https://` → TLS |
| `OTEL_EXPORTER_OTLP_HEADERS` | 空 | 逗号分隔 `k=v`，鉴权用 |
| `OTEL_SERVICE_NAME` | `twinkle-agentserver` | Resource.service.name |

配置从 [config.py](../twinkle/config.py) 读取，优先级：环境变量 > `.env` 文件 > 默认值。

---

## 10. 模块目录与职责速查

```
twinkle/
  config.py                # 环境变量 + .env 加载
  e2a/
    models.py              # E2AEnvelope / E2AResponse（Pydantic 最小子集）
    __init__.py            # 导出
  schema/
    message.py             # Message + EventType（Gateway 内部格式）
    __init__.py
  agentserver/
    __main__.py            # python -m 入口
    server.py              # ws server + AgentLoop 分发 + ws_handler(loop, store) 路由 session RPC
    agent_loop.py          # ReAct 核心闭环（入口 set plan-todo ContextVar + 首次插入 todo system message）
    session_rpc.py         # session.create/list/delete + history.get + session.files/file.read RPC 分发（产单帧 e2a.result）
    llm_client.py          # OpenAI SDK 薄封装
    session_store.py       # 磁盘+内存会话存储（<sid>/{metadata,history}.json）
    memory.py              # 长期记忆 stub
    plan_todo_context.py   # ContextVar：当前请求的 todo session 路由
    todo_store.py          # TodoStore：内存 dict[session_id, list[TodoTask]] + 每 session 一把 asyncio.Lock
    tools/
      base.py              # ToolCard + Tool(Protocol)
      schema_extractor.py  # 签名/docstring → JSON schema
      local_function.py    # LocalFunction（本地函数 Tool 实现）
      decorator.py         # @tool 装饰器
      manager.py           # ToolManager（容器，存 dict[str, Tool]）
      __init__.py           # 框架 re-export + tool_manager() 预注册 builtin 工具
      builtin/             # 具体工具（web/shell/file/todo）：框架/实现分层
        web_fetch.py          # URL → markdown/文本
        web_search.py         # DuckDuckGo Lite 搜索
        command_exec.py       # 跨平台 shell 执行（blocklist + workspace 收敛 + 超时 + 输出裁剪 + 非阻塞后台）
        file_tools.py         # @tool 文件工具：read_file / write_file / edit_file / list_files / glob（workspace 收敛 + 先读后写）
        todo_tools.py         # @tool todo 工具：create / complete / list
  observability/            # OTel 遥测切面（setup() 启动接入，默认 off 零成本）
    __init__.py             # setup() 单入口（幂等 + fail-soft）
    config.py               # ObservabilityConfig（env 驱动默认关）
    provider.py             # TracerProvider + MeterProvider（otlp/console/none）
    attributes.py           # gen_ai.* / twinkle.* 属性键
    context.py              # RequestContext ContextVar + iteration 计数
    metrics.py              # Metrics（5 instrument + fail-soft）
    usage.py                # read_usage_token（dict / CompletionUsage 兼容）
    wrap.py                 # patch_method（幂等 + fail-soft）
    instrumentors/          # agent/llm/tool 三个插桩点
  gateway/
    __main__.py            # python -m 入口，装配四件 + 起 web_channel
    agent_client.py         # ws client + demux + stream
    web_channel.py          # 浏览器 ws server + req 解析 + event 广播
    channel_manager.py      # channel 注册 + 出站 dispatch
    message_handler.py      # 格式转换 + 流式扇出
  web/
    src/
      App.vue               # 导航壳（LeftNav + ChatView/SessionsView 切换）
      services/
        webClient.ts         # 浏览器 ws 客户端
      composables/
        useSessions.ts       # 会话状态 + activeNav + 文件浏览 composable
      components/
        LeftNav.vue           # 左侧导航（聊条 + 会话）
        ChatView.vue          # 聊天视图（ChatPanel + TodoPanel）
        ChatPanel.vue         # 聊天面板（➕ 新对话按钮 + 输入 + 发送）
        TodoPanel.vue         # Todo 侧栏
        SessionsView.vue      # 3 栏会话页面容器
        SessionListPane.vue   # 会话列表栏
        FileTreePane.vue      # 文件树栏（扁平文件列表）
        FilePreviewPane.vue   # 文件预览栏（history.json 气泡/JSON 切换 + metadata.json 格式化）
    vite.config.ts          # Vite 配置 + ws 代理
tests/
  test_agent_loop.py        # AgentLoop 单测
  test_agentserver_handler.py # ws handler 级测试
  test_integration.py       # 端到端全链路
  test_llm_client.py        # LLM 客户端单测
  test_session_store.py     # 会话存储单测（含 list_files/read_file + 路径安全）
  test_session_rpc.py       # 会话 RPC 单测（含 session.files/file.read）
  test_tool_manager.py      # ToolManager 单测
  test_base.py              # ToolCard + Tool 单测
  test_schema_extractor.py  # schema 抽取器单测
  test_local_function.py    # LocalFunction 单测
  test_tool_decorator.py   # @tool 装饰器单测
  test_command_exec.py     # command_exec 单测
  test_todo_tools.py       # todo 工具单测
  test_todo_store.py       # TodoStore 单测
  test_plan_todo_context.py # plan-todo ContextVar + TODO_EVENTS 总线单测
  test_message_handler.py  # MessageHandler 翻译/异常兜底单测
  test_observability.py    # OTel 插桩 + 指标单测
  test_memory_stub.py       # 记忆 stub 单测
  test_web_fetch.py         # web_fetch 单测
  test_web_search.py        # web_search 单测
  test_file_tools.py        # 文件工具单测
scripts/
  start_services.py         # 一键启动两进程
```

---

## 11. 与参考实现 jiuwenclaw 的对照

| Twinkle 模块 | jiuwenclaw 对应 | 差异 |
|---|---|---|
| `e2a/models.py` (7+8 字段) | `e2a/models.py` (20+ 字段) | 最小子集，砍 provenance/auth/wire_codec |
| `gateway/web_channel.py` | `channel/web_channel.py:110-596` | 简化版，砍 Origin 校验/heartbeat |
| `gateway/agent_client.py` | `gateway/agent_client.py:153-846` | 砍 send_request(unary)、server-push |
| `gateway/message_handler.py` | `gateway/message_handler.py:2408-2484` | 砺 _process_unary |
| `gateway/channel_manager.py` | `gateway/channel_manager.py:57-239` | 基本对齐 |
| `agentserver/server.py` | `agentserver/agent_ws_server.py` | 砺 legacy/heartbeat/cron |
| `agentserver/agent_loop.py` | `agentserver/deep_agent/interface_deep.py` | 最小 ReAct；入口 set plan-todo ContextVar + 首次插入 todo system message（ReAct 主体未改；todo/command 已回补一部分，skill 仍砍） |
| `tools/{base,local_function,decorator,schema_extractor,manager}` | openjiuwen foundation/tool/* + ability_manager.py | 四层最小子集，砍 MCP/Input/Output/触发器 |
| `schema/message.py` | `schema/message.py:141-249` | 只保留 4 个 EventType |
| `observability/` | jiuwenswarm-instrumentor | OTel 自动插桩最小子集，砍跨进程 W3C / context-token 分桶 / CLI wrapper / logs |

---

*本文与 Twinkle 代码库同步维护。*
