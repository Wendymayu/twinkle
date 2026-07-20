# Phase 0 设计文档 — 两进程骨架打通

> 状态：已实现并通过验收。对应 [roadmap](../roadmap.md) Phase 0。
> 参考实现：`D:\opensource\gitcode\jiuwenclaw`。
> **后置更新**：Phase 1 之后移除了 unary（单次）模式，系统改为流式专用。E2AEnvelope 的 `is_stream` 字段已删除，所有请求隐式流式；`run_unary` / `send_request` / `_process_unary` 均已移除。

## 1. 目标

复刻 jiuwenclaw 的 **gateway ↔ agentserver 两进程 + 双向 WebSocket** 主架构，跑通一条 echo 贯穿 `浏览器 → gateway → agentserver → 回流 → 浏览器`。Phase 0 不接真模型、不做 agent loop，handler 内联 echo；目的是把"两进程接缝"和"流式协议"这两条命脉先钉死，后续 Phase 1+ 在此骨架上替换 echo 为真 agent 运行时。

## 2. 进程架构

```
                浏览器 (Vite dev :5173)
                    │  ws (同源, Vite 代理 /ws → :19000)
                    ▼
┌─────────────────────────────────────────────────────┐
│ Gateway 进程 (:19000)                                 │
│   WebChannel  (websockets server, 浏览器侧)            │
│   MessageHandler  (Message ⇄ E2A 信封转换 + 流式扇出)   │
│   ChannelManager (出站 dispatch 循环)                  │
│   AgentClient  (websockets client, 连 AgentServer)    │
└──────────────────────┬──────────────────────────────┘
                       │ ws (E2A 信封, 主动连接)
                       ▼
┌─────────────────────────────────────────────────────┐
│ AgentServer 进程 (:18000)                             │
│   AgentWebSocketServer (websockets server)            │
│   echo handler (Phase 0 内联; Phase 1 换成真 agent)    │
└─────────────────────────────────────────────────────┘
```

**为何两进程**（对齐 jiuwenclaw）：gateway 是连接/调度边缘（轻、可频繁重启），agentserver 是执行核心（持长任务、重资源）。即便 Phase 0 单用户也保留此拆分，是为了后续与 jiuwenclaw 对照、降低架构差距。这是学习型项目的有意识取舍，不是性能需要。

**为何一条 ws 双向**：jiuwenclaw 的 server→client 推送（`gateway_push/`）走同一条 ws、用 `metadata` 标记区分推送帧与 RPC 响应帧。Phase 0 暂未用 server-push 通道（echo 是请求驱动），但 ws 单连接 + 按 `request_id` demux 的结构已就位，Phase 3 上下文压缩的异步推送可在此接。

## 3. 协议

### 3.1 Gateway ↔ AgentServer：E2A 子集

定义在 [`twinkle/e2a/models.py`](../twinkle/e2a/models.py)，是 jiuwenclaw `e2a/models.py` 的最小子集（砍掉 legacy fallback、wire_codec、provenance/service_id 等）。

**连接握手**：AgentServer 在每条新连接上先发一帧 `connection.ack`（**非 E2A 形状**，普通 event 帧）：
```json
{"type":"event","event":"connection.ack","payload":{"status":"ready"}}
```
AgentClient 必须先 `recv()` 消费它再启动 demux 循环。

**请求帧**（Gateway → Server，`E2AEnvelope`）：
```json
{"protocol_version":"1.0","request_id":"r1","channel":"web",
 "session_id":null,"method":"chat.send","params":{"query":"hi"},"is_stream":true}
```

**响应帧**（Server → Gateway，`E2AResponse`）：
- 流式 chunk：`{"...","sequence":0,"is_final":false,"status":"in_progress","response_kind":"e2a.chunk","body":{"result":{"content":"h"}}}`
- 终止帧：`{"...","is_final":true,"status":"succeeded","response_kind":"e2a.complete","body":{"result":{"content":"Echo: hi"}}}`
- 错误帧：`response_kind="e2a.error"`, `status="failed"`

**demux**：AgentClient 维护 `dict[request_id, asyncio.Queue]`，`_recv_loop` 把每帧按 `request_id` 投递。`send_request_stream` 是 async generator，yield 到 `is_final` 为止；`send_request` 是 unary，`wait_for` 一个帧。

### 3.2 Browser ↔ Gateway：req/res/event

定义在 [`twinkle/schema/message.py`](../twinkle/schema/message.py)（EventType + Message）。

**入站**（浏览器 → gateway）：
```json
{"type":"req","id":"r1","method":"chat.send","params":{"query":"hello"}}
```
- `id` = request_id，贯穿到 E2A 的 `request_id`，再回到出站 event 的 `request_id`，浏览器据此关联流式分片。

**出站**（gateway → 浏览器），两类：
- 立即 ACK：`{"type":"res","id":"r1","ok":true,"payload":{"accepted":true,"session_id":null}}` — 收到 req 立刻回，不等 agent。
- 流式事件（广播给所有连接）：`{"type":"event","event":"chat.delta","payload":{"content":"h"},"request_id":"r1"}`，逐帧直到 `chat.final`（`payload.is_complete` 语义在 Phase 1 补，Phase 0 用 final 帧本身表示结束）。

## 4. 数据流（一次 chat.send 的逐步轨迹）

1. 浏览器 `webClient.ts` 开 `ws://localhost:5173/ws`（Vite 代理到 :19000），发 `{type:req,id:r1,method:chat.send,params:{query:"hello"}}`。
2. `WebChannel.handler` 收帧 → `_handle_raw` 解析 → 建 `Message(id=r1,...)` → 立即 `_send_response(res{accepted})` → 调 `on_message` 回调。
3. `ChannelManager` 的 `_on_message` 闭包 → `MessageHandler.handle_message`。
4. `handle_message` 把 Message 包成 `E2AEnvelope(request_id=r1,is_stream=true)` → `asyncio.create_task(_process_stream)`（fire-and-forget）。
5. `_process_stream` 调 `AgentClient.send_request_stream` → 发 E2A 请求帧。
6. AgentServer `handler` 解析 E2A → `_echo_stream`：把 `"Echo: hello"` 逐字发 N 个 `e2a.chunk` + 1 个 `e2a.complete`。
7. AgentClient `_recv_loop` 按 `request_id=r1` 投递到队列 → `send_request_stream` yield 各 `E2AResponse`。
8. `_process_stream` 把每个 chunk 包成 `Message(event_type=chat.delta, content=...)` → `ChannelManager.publish_robot_message`。
9. `ChannelManager._dispatch_loop` 取出 → 查 `channel_id="web"` → `WebChannel.send` 广播 `{type:event,event:chat.delta,payload:{content},request_id:r1}`。
10. 终止 chunk → `chat.final`。浏览器 `webClient` 按 `request_id` 拼 delta，final 结束。

## 5. 模块职责表

| 文件 | 职责 | 对齐 jiuwenclaw |
|---|---|---|
| `e2a/models.py` | E2AEnvelope / E2AResponse（pydantic 最小子集） | `e2a/models.py`（砍 legacy） |
| `schema/message.py` | Message + EventType(chat.delta/final/connection.ack) | `schema/message.py:141-249` |
| `agentserver/server.py` | ws server + echo handler（stream/unary/error） | `agentserver/agent_ws_server.py:339,788,821` |
| `agentserver/__main__.py` | `python -m twinkle.agentserver` 入口 | `app_agentserver.py`（极简） |
| `gateway/agent_client.py` | ws client + connection.ack + demux + stream/unary | `gateway/agent_client.py:153,205,336,726-846` |
| `gateway/web_channel.py` | 浏览器 ws server + req 解析 + res/event 广播 | `channel/web_channel.py:110-596` |
| `gateway/channel_manager.py` | channel 注册 + 出站 dispatch 循环 | `gateway/channel_manager.py:57-239` |
| `gateway/message_handler.py` | Message→E2A + chunk→chat.delta 扇出 | `gateway/message_handler.py:2408-2484` |
| `gateway/__main__.py` | 装配四件 + 起 web_channel | `app_gateway.py`（极简） |
| `web/src/services/webClient.ts` | 浏览器 ws + 按 request_id 拼 delta/final | `web/src/services/webClient.ts:237-394` |
| `web/src/App.vue` | 极简聊天 UI | （twinkle 自有，非搬运） |

## 6. 关键设计取舍

- **全 `websockets` 库**：gateway↔agentserver 与 browser↔gateway 都用 websockets 库（≥14 现代 asyncio API），与 jiuwenclaw 一致，且 `agent_client.py:726-846` 的 mock echo 可直接当测试蓝本。不引 aiohttp。
- **静态页由 Vite 代理**：dev 模式 Vite(:5173) 代理 `/ws`→:19000，浏览器同源、免 CORS。Phase 0 不实现 gateway 服静态产物（jiuwenclaw 的 `app_web.py` 静态+代理服务器暂不搬）。
- **echo 逐字流式**：让流式肉眼可见（每字 `await asyncio.sleep(0.02)`），验证的不只是"能回"而是"能分片流式"。
- **依赖最小**：仅 `websockets`+`pydantic`，不引 openjiuwen/chromadb/sqlalchemy/tree-sitter 等重家伙——Phase 0 echo 不依赖任何 agent 运行时。
- **测试不依赖 pytest-asyncio**：用 `asyncio.run()` + 自带 `_free_port()`，避免为一个端口 fixture 拖进插件。

## 7. 验证

- 自动：`python -m pytest tests/`（3 项：echo 流式+unary、坏包错误帧、端到端全链路）。
- 手动：`python scripts/start_services.py` + `cd web && npm run dev` → http://localhost:5173。
- 进程冒烟：`python -m twinkle.agentserver` + `python -m twinkle.gateway` 独立启动，gateway 日志出现 `connected to AgentServer`、`WebChannel listening on :19000`。

## 8. Phase 0 明确不做

slash 命令、session_map/session_index、多 channel（只 web）、Origin 白名单（本地开发不校验）、heartbeat/cron、文件传输、prod 静态产物服务、E2A legacy fallback codec、server-push 通道（结构已留，echo 不用）。

## 9. 下阶段衔接点（Phase 1）

Phase 1 接真模型 + agent loop 时，**只替换 `agentserver/server.py` 的 `_echo_stream/_echo_unary`** 为真 agent 调用（模型 client + think→tool→result→return）。gateway 全链路（web_channel / message_handler / channel_manager / agent_client）**无需改动**——这正是 Phase 0 把接缝钉死的价值。

短期记忆（对话记录）在 `_process_stream` 处接入 session 历史；长期 memory 接口在 agent loop 调用处打 stub（roadmap Phase 1 已记）。
