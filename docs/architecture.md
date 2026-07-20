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
| 核心组件 | `AgentLoop`（ReAct 循环）、`LLMClient`、`SessionStore`、`ToolRegistry` |

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
- 立即回一个 ACK `{type:"res", id, ok:true, payload:{accepted:true}}` — 不等 Agent 响应
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

**出站转换**（E2AResponse → Message → Queue）：
- 每个 chunk → `Message(event_type=chat.delta, content=chunk_content)` → `enqueue_outbound(msg)`
- 终止帧 → `Message(event_type=chat.final, content=final_content)` → `enqueue_outbound(msg)`
- 错误 → `Message(event_type=chat.final, content="[error] ...")` → `enqueue_outbound(msg)`

出站消息不直接交给 ChannelManager——而是投进自己的 `_robot_messages` Queue，由 ChannelManager 的 `_dispatch_loop` 通过 `dequeue_outbound()` 消费。这是对齐 jiuwenclaw 的单向依赖模式。

`_process_stream` 是 fire-and-forget task（`asyncio.create_task`），不阻塞下一个请求。

### 3.4 AgentClient — ws 客户端 + demux

[agent_client.py](../twinkle/gateway/agent_client.py) 是 Gateway 连 AgentServer 的 ws 客户端：

**连接**：
- `connect()` — 建立 ws 连接，先 `recv()` 消费 `connection.ack` 帧，再启动 `_recv_loop`

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
async def send_request_stream(self, env: E2AEnvelope) -> AsyncIterator[E2AResponse]:
    rid = env.request_id
    q: asyncio.Queue = asyncio.Queue()
    self._queues[rid] = q              # ← 注册：告诉 recv_loop 这个 rid 的帧投到这里
    await self._send(env)
    while True:
        data = await q.get()            # ← 只取属于这个请求的帧
        resp = E2AResponse.model_validate(data)
        yield resp
        if resp.is_final:
            break
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
          │  yield E2AResponse     │  每个 TextDelta → e2a.chunk
          │  最终 → e2a.complete   │  或 e2a.error（超过 max_steps）
          └───┬────────────────────┘
              │
    ┌─────────┼───────────┐
    │         │           │
    ▼         ▼           ▼
LLMClient  SessionStore  ToolRegistry
(流式API)  (对话记忆)    (只读工具)
```

### 4.1 server.py — ws 接口 + AgentLoop 分发

[server.py](../twinkle/agentserver/server.py) 的 `handler` 流程：

1. 新连接 → 发 `connection.ack` 帧（非 E2A 格式，是普通 event）
2. 循环收帧 → `E2AEnvelope.model_validate_json(raw)` 解析
3. 解析失败 → 发 `e2a.error` 响应
4. 解析成功 → `async for frame in loop.run_stream(env): await _safe_send(ws, frame)`
5. AgentLoop 异常 → 发 `e2a.error` 响应

`_safe_send` 静默吞掉 `ConnectionClosed` — 客户端断连时不报错，这是正常生命周期事件。

`make_handler(loop)` 让测试注入假 loop，`build_default_loop()` 用真实配置组建。

### 4.2 AgentLoop — ReAct 核心闭环

[agent_loop.py](../twinkle/agentserver/agent_loop.py) 是整个系统的核心算法：

```
用户 query → store.append(user) → memory.recall(stub空) → msgs = store.get_messages()
    │
    ▼  ReAct 循环（max_steps=8 守护）
    │
    ├── llm.stream(msgs, tools)
    │   ├── TextDelta → yield E2AResponse(e2a.chunk)
    │   ├── ToolCalls → 执行工具 → store.append(tool) → continue（再问模型）
    │   └── Done(reason=stop) → store.append(assistant) → yield e2a.complete → break
    │
    └── 超过 max_steps → yield e2a.error
```

关键设计：
- `run_stream` 是 **async generator**，yield E2AResponse — loop 对 ws 零依赖，单测无需起 ws
- 工具结果回灌是命门：`{role:"tool", tool_call_id, content:result}` append 进 store，下一轮 `get_messages` 自然带上
- `max_steps=8` 防止工具循环不收敛

### 4.3 SessionStore — 短期对话记忆

[session_store.py](../twinkle/agentserver/session_store.py) 是 in-memory `dict[session_id, list[msg]]`，存的就是 OpenAI 原生 `messages` 格式：

- `append(session_id, message)` — 添加一条消息
- `get_messages(session_id)` — 返回该会话的所有消息（含 user/assistant/tool）

Phase 1 不做落盘持久化；接口允许后续换 SQLite，不回炉。

### 4.4 LLMClient — 模型流式接口

[llm_client.py](../twinkle/agentserver/llm_client.py) 是 OpenAI SDK 薄封装，`base_url` 可配兼容任意端点。`stream()` 返回 `AsyncIterator[TextDelta | ToolCalls | Finish]`。

### 4.5 ToolRegistry — 最小版工具管理

[tools/registry.py](../twinkle/agentserver/tools/registry.py) 静态注册了 `web_fetch` 和 `web_search` 两个只读工具：
- `schemas()` → OpenAI function calling 格式
- `execute(name, args)` → 返回文本结果

Phase 2 会演进为动态注册 + `tool_catalog`。

### 4.6 LongTermMemory — stub

[memory.py](../twinkle/agentserver/memory.py) 是空实现：`recall()` 返回空列表，`store()` 不做事。接口形状钉死，将来换真实现不回炉。

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
```

- `chat.delta` — 每帧一个字/词片段，浏览器拼接到上一个 assistant 消息
- `chat.final` — 最终完整内容，浏览器用此标记对话结束
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

**6 个核心字段**（无 `is_stream` —— 流式专用，所有请求隐式 streaming）：

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
| `response_kind` | str | `e2a.chunk` / `e2a.complete` / `e2a.error` |
| `body` | dict | 载荷内容 |
| `is_stream` | bool | 固定 `true`（Twinkle 流式专用） |

**三种 response_kind**：

| response_kind | 含义 | body 结构 |
|---|---|---|
| `e2a.chunk` | 流式分片 | `{result: {content: "字片段"}}` |
| `e2a.complete` | 正常终止 | `{result: {content: "完整文本"}}` |
| `e2a.error` | 错误终止 | `{error: "错误描述"}` |

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
    event_type: EventType | None = None  # chat.delta / chat.final
    payload: dict[str, Any]              # 附加载荷
    ok: bool = True                      # ACK 成功标志
    content: str = ""                    # 文本内容
```

`EventType` 只有三个值：

| EventType | 说明 |
|---|---|
| `connection.ack` | 连接就绪 |
| `chat.delta` | 流式分片 |
| `chat.final` | 终止帧 |

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
   → 立回 ACK JSON: {type:"res", id:"req_m1abc_0", ok:true, payload:{accepted:true}}
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
- **webClient.ts** — ws 客户端，发 req、收 event、按 request_id 拼 delta/final

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
  → res → ignore（ACK，Phase 0 不展示）
  → event.chat.delta → onDelta(content, request_id)
  → event.chat.final → onFinal(content, request_id)
```

### 8.4 App.vue

[App.vue](../web/src/App.vue) 极简聊天 UI：

- `onDelta(delta, rid)` — 拼接到最后一个 assistant bubble（如果 rid == currentId）
- `onFinal(text, rid)` — 最终内容覆盖或新建 assistant bubble
- `send()` — 用户输入 → `msgs.push({role:'user'})` → `client.send('chat.send', {query})`

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
    server.py              # ws server + AgentLoop 分发
    agent_loop.py          # ReAct 核心闭环
    llm_client.py          # OpenAI SDK 薄封装
    session_store.py       # in-memory 对话记录
    memory.py              # 长期记忆 stub
    tools/
      registry.py          # 最小版工具注册
      web_fetch.py          # URL → markdown/文本
      web_search.py         # DuckDuckGo Lite 搜索
  gateway/
    __main__.py            # python -m 入口，装配四件 + 起 web_channel
    agent_client.py         # ws client + demux + stream
    web_channel.py          # 浏览器 ws server + req 解析 + event 广播
    channel_manager.py      # channel 注册 + 出站 dispatch
    message_handler.py      # 格式转换 + 流式扇出
  web/
    src/
      App.vue               # 极简聊天 UI
      services/
        webClient.ts         # 浏览器 ws 客户端
    vite.config.ts          # Vite 配置 + ws 代理
tests/
  test_agent_loop.py        # AgentLoop 单测
  test_agentserver_handler.py # ws handler 级测试
  test_integration.py       # 端到端全链路
  test_llm_client.py        # LLM 客户端单测
  test_session_store.py     # 会话存储单测
  test_tool_registry.py     # 工具注册单测
  test_memory_stub.py       # 记忆 stub 单测
  test_web_fetch.py         # web_fetch 单测
  test_web_search.py        # web_search 单测
scripts/
  start_services.py         # 一键启动两进程
```

---

## 11. 与参考实现 jiuwenclaw 的对照

| Twinkle 模块 | jiuwenclaw 对应 | 差异 |
|---|---|---|
| `e2a/models.py` (6+8 字段) | `e2a/models.py` (20+ 字段) | 最小子集，砍 provenance/auth/wire_codec |
| `gateway/web_channel.py` | `channel/web_channel.py:110-596` | 简化版，砍 Origin 校验/heartbeat |
| `gateway/agent_client.py` | `gateway/agent_client.py:153-846` | 砍 send_request(unary)、server-push |
| `gateway/message_handler.py` | `gateway/message_handler.py:2408-2484` | 砺 _process_unary |
| `gateway/channel_manager.py` | `gateway/channel_manager.py:57-239` | 基本对齐 |
| `agentserver/server.py` | `agentserver/agent_ws_server.py` | 砺 legacy/heartbeat/cron |
| `agentserver/agent_loop.py` | `agentserver/deep_agent/interface_deep.py` | 最小 ReAct，砍 skill/todo/command |
| `schema/message.py` | `schema/message.py:141-249` | 只保留 3 个 EventType |

---

*本文与 Twinkle 代码库同步维护。*
