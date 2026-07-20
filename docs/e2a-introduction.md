# E2A（Everything-to-Agent）协议介绍

> **Twinkle 实现**：`twinkle/e2a/models.py`（最小子集）。**参考实现**：`jiuwenclaw/e2a/`（完整协议 + 适配器 + wire_codec）。**版本**：`E2A_PROTOCOL_VERSION = "1.0"`。**完整规范**：[jiuwenclaw E2A-protocol.md](../../jiuwenclaw/docs/zh/E2A-protocol.md)。

---

## 1. E2A 是什么？

**E2A**（**Everything-to-Agent**）是 jiuwenclaw 定义的一种**统一信封协议**，用于 Gateway ↔ AgentServer 之间的双向 WebSocket 通信。其核心思想是：

> 无论消息来自哪个通道（Web、飞书、钉钉）或哪种外部协议（ACP、A2A），经过 Gateway 规范化后，统一以 **E2A 信封** 发往 AgentServer；AgentServer 的响应也统一以 **E2A 响应** 回流 Gateway。

这样，AgentServer 只需理解一种协议，Gateway 负责适配一切——这正是"Everything-to-Agent"名字的由来。

```
  ACP 客户端 ──┐                         ┌── Web 浏览器
  A2A Agent  ──┤  ──→ Gateway ──E2A──→ AgentServer
  飞书/钉钉  ──┘    适配+规范化         执行核心（只懂 E2A）
```

---

## 2. 为什么需要 E2A？

### 问题：通道多样性导致协议碎片化

一个 Agent 系统可能对接多种入口——浏览器 WebSocket、飞书/钉钉 webhook、ACP JSON-RPC 客户端、A2A 协议 Agent 等。每种入口有自己的消息格式：

- **浏览器**：自定义 `{type, id, method, params}` JSON
- **ACP**：JSON-RPC 2.0 `{jsonrpc, id, method, params}`
- **A2A**：`{task_id, message, context}` 结构体
- **飞书/钉钉**：各平台独有的 event/callback JSON

如果 AgentServer 直接处理所有这些格式，代码会极其混乱——每种格式都要一套解析、路由、响应逻辑。

### 解法：E2A 作为统一中间层

Gateway 在入口侧做**适配 + 规范化**，把一切通道的原生格式映射到 E2A 的规范化字段（`method`、`params`、`session_id`、`channel` 等），然后以统一信封发给 AgentServer。AgentServer 只需处理 E2A 请求、产出 E2A 响应，Gateway 再把响应转回各通道的原生格式。

这带来了三个关键好处：

1. **AgentServer 与通道解耦**：Agent 核心不需要知道消息来自浏览器还是飞书。
2. **Gateway 可独立演进**：新增通道（如 Discord）只需在 Gateway 加适配层，AgentServer 不动。
3. **单连接多协议共存**：一条 WebSocket 上可以跑 E2A 原生请求、ACP 转入请求、A2A 转入请求，通过 `provenance.source_protocol` 区分来源。

---

## 3. E2A 的核心结构

E2A 由两个对称的数据结构组成：

### 3.1 E2AEnvelope — 请求信封（Gateway → AgentServer）

```
┌─────────────────────────────────────────────────────────────┐
│ E2AEnvelope                                                  │
│                                                              │
│  ┌─── 基础/关联 ──────────────────────────────────────────┐ │
│  │ protocol_version  ─  "1.0"                              │ │
│  │ request_id        ─  网关↔AgentServer 主请求 id          │ │
│  │ session_id        ─  会话 id（跨轮对话关联）              │ │
│  │ （无 is_stream ── Twinkle 流式专用，隐式 streaming）     │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─── 入口/方法 ──────────────────────────────────────────┐ │
│  │ method             ─  网关 RPC 名（如 chat.send）         │ │
│  │ params             ─  唯一业务参数字典                    │ │
│  │ channel            ─  入口名（web / feishu / dingding）  │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─── 出处/扩展（Twinkle 不用，jiuwenclaw 完整版有）──────┐ │
│  │ provenance         ─  来源协议标记（e2a/acp/a2a）        │ │
│  │ a2a_metadata       ─  A2A 互操作专用                    │ │
│  │ acp_meta           ─  ACP 互操作专用                    │ │
│  │ auth               ─  凭据引用                          │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**完整版**（jiuwenclaw）额外有：`provenance`（E2AProvenance）、`jsonrpc_id`、`correlation_id`、`task_id`、`context_id`、`message_id`、`identity_origin`、`user_id`、`chat_id`、`source_agent_id`、`expected_output_modes`、`auth`（E2AAuth）、`channel_context`、`a2a_metadata`、`acp_meta`、`service_id`、`agent_id`。

### 3.2 E2AResponse — 响应信封（AgentServer → Gateway）

E2A 响应采用**三层结构**：

```
┌─────────────────────────────────────────────────────┐
│ E2AResponse                                          │
│                                                      │
│  L1 ── 信封层                                       │
│  │  request_id    ── 回显请求 id                    │
│  │  sequence      ── 同 request_id 下从 0 递增      │
│  │  is_final      ── 最后一帧为 true               │
│  │  status        ── succeeded/failed/in_progress   │
│  │  timestamp     ── RFC 3339 UTC                   │
│  │  provenance    ── 出处（与请求同结构）            │
│                                                      │
│  L2 ── 判别载荷                                     │
│  │  response_kind ── 载荷类型标识                   │
│  │  body          ── 具体业务内容                   │
│                                                      │
│  L3 ── 投影（可选）                                  │
│  │  projections   ── acp/a2a 已组装的外部协议 JSON  │
└─────────────────────────────────────────────────────┘
```

#### response_kind — 载荷类型

| response_kind | 含义 | body 要点 |
|---|---|---|
| `e2a.chunk` | 流式分片 | `delta_kind`（text/reasoning/tool/custom）、`delta`（字符串或结构化） |
| `e2a.complete` | 正常终止 | `result`（业务结果对象） |
| `e2a.error` | 错误终止 | `code`、`message`、`details` |
| `acp.session_update` | ACP 会话更新通知 | 对齐 ACP `session/update` |
| `acp.prompt_result` | ACP prompt 结果 | 对应 JSON-RPC `result` |
| `a2a.task` | A2A Task 对象 | A2A 协议 Task 形 |
| `a2a.message` | A2A Message 对象 | A2A 协议 Message 形 |
| `a2a.stream_event` | A2A 流式事件 | branch: task/message/status_update/artifact_update |
| `ext` | 扩展方法 | `ext_method` + `params` |

> **Twinkle 当前只使用**：`e2a.chunk`、`e2a.complete`、`e2a.error`。ACP/A2A 相关的 response_kind 属于 jiuwenclaw 的互操作能力，Twinkle roadmap 明确不做。

---

## 4. Twinkle 的 E2A 实现 — 最小子集

Twinkle 是 jiuwenclaw 的**学习型重写**项目，只保留核心 agent 链路。因此 E2A 的实现也做了大幅裁剪：

### 4.1 实现文件

| 文件 | 说明 |
|---|---|
| [`twinkle/e2a/models.py`](../twinkle/e2a/models.py) | E2AEnvelope（6 字段）+ E2AResponse（8 字段）（Pydantic BaseModel） |
| [`twinkle/e2a/__init__.py`](../twinkle/e2a/__init__.py) | 导出 E2AEnvelope、E2AResponse、E2A_PROTOCOL_VERSION |

### 4.2 保留了什么

Twinkle 的 E2AEnvelope 只保留 **6 个核心字段**（对比 jiuwenclaw 的 20+ 字段）：

```python
class E2AEnvelope(BaseModel):
    protocol_version: str = "1.0"       # 协议版本
    request_id: str                      # 请求 id（流式 chunk 关联）
    channel: str = "web"                 # 入口通道（Twinkle 只用 web）
    session_id: str | None = None        # 会话 id
    method: str                          # 网关 RPC 方法名
    params: dict[str, Any]               # 业务参数
    # 注：无 is_stream 字段 —— Twinkle 是流式专用，所有请求隐式流式

class E2AResponse(BaseModel):
    protocol_version: str = "1.0"
    request_id: str                      # 回显请求 id
    sequence: int = 0                    # 同 request_id 下从 0 递增
    is_final: bool = False               # 最后一帧
    status: str = "in_progress"          # succeeded/failed/in_progress
    response_kind: str = "e2a.chunk"     # e2a.chunk/e2a.complete/e2a.error
    body: dict[str, Any]                 # 载荷内容
    is_stream: bool = True               # 响应总是流式（Twinkle 专用）
```

### 4.3 砍掉了什么

| 砍掉的内容 | 原因 |
|---|---|
| `E2AEnvelope.is_stream`（流式/单次切换） | Twinkle 流式专用，所有请求隐式 streaming |
| `E2AProvenance`（出处结构） | Twinkle 只有一条 ws + 单通道，不需要区分 ACP/A2A 来源 |
| `wire_codec`（线编解码 + legacy fallback） | Twinkle 没有历史遗留格式需要兼容 |
| `gateway_normalize`（旧 AgentRequest 迁移） | Twinkle 从头设计，不搬运旧数据结构 |
| `adapters.py`（ACP/A2A 适配器） | roadmap 明确不做多协议互操作 |
| `agent_compat.py`（E2A → AgentRequest 转换） | 同上 |
| `E2AAuth`（鉴权） | roadmap 推迟危险工具审批和鉴权 |
| `E2AFileRef`（文件引用） | roadmap 不做文件传输 |
| `acp/` 子目录 | ACP 协议子集，roadmap 不做 |
| `acp_tool_updates.py` | 同上 |
| `jsonrpc_id`、`correlation_id`、`task_id` 等 10+ 字段 | 单用户 + 单通道不需要分布式追踪和多租户 |

> 核心取舍原则：**保留"流式分片 + 请求关联"这个命脉，砍掉所有"兼容旧格式 + 多协议互操作 + 企业级"的能力**。这些在 jiuwenclaw 里有完整实现，需要时参考即可。

### 4.4 序列化方式

- **jiuwenclaw**：使用 dataclass + 手写 `to_dict()` / `from_dict()`（含兼容键迁移、legacy fallback）
- **Twinkle**：使用 **Pydantic BaseModel**，直接 `model_dump_json()` / `model_validate_json()`，零手写序列化代码

选择 Pydantic 的理由：
1. 自带 JSON 序列化/反序列化 + 类型校验，不需要手写 `_dataclass_to_json_dict`
2. Twinkle 的依赖最小策略只保留 `pydantic` + `websockets`，不引更多框架
3. 字段裁剪后结构足够简单，不需要 jiuwenclaw 那套复杂的迁移逻辑

---

## 5. E2A 在 Twinkle 中的流转轨迹

下面是一次 `chat.send` 请求的完整流转，标注了 E2A 信封在哪里出现：

```
 ① 浏览器发原生格式
    {type:"req", id:"r1", method:"chat.send", params:{query:"你好"}}
                        │
                        ▼
 ② WebChannel 收 → Message(id=r1, method=chat.send, params={query:"你好"})
                        │
                        ▼
 ③ MessageHandler 转换 → E2AEnvelope(         ← E2A 信封在这里诞生
        request_id="r1",
        channel="web",
        method="chat.send",
        params={query:"你好"})
                        │
                        ▼
 ④ AgentClient.send → ws 发 E2AEnvelope.model_dump_json()
                        │
                        ▼
 ⑤ AgentServer 收 → E2AEnvelope.model_validate_json(raw)
                        │
                        ▼
 ⑥ AgentLoop.run_stream → yield E2AResponse(    ← E2A 响应在这里诞生
        request_id="r1",
        sequence=0,
        is_final=False,
        response_kind="e2a.chunk",
        body={result:{content:"你"}})
                        │
                        ▼
 ⑦ AgentServer 发 → ws.send(E2AResponse.model_dump_json())
                        │
                        ▼
 ⑧ AgentClient._recv_loop → 按 request_id demux 投递到 asyncio.Queue
                        │
                        ▼
 ⑨ MessageHandler 出站 → Message(event_type=chat.delta, content="你")
                          → enqueue_outbound → _robot_messages Queue.put
                          │
                          ▼
 ⑩ ChannelManager._dispatch_loop → dequeue_outbound → Queue.get
                          → 查 channel_id="web" → WebChannel.send → 广播
    {type:"event", event:"chat.delta", payload:{content:"你"}, request_id:"r1"}
```

### 关键设计点

- **E2A 只在 Gateway ↔ AgentServer 的 ws 上出现**：浏览器侧用的是 `Message`（req/res/event 格式），Gateway 在 `MessageHandler` 里做两种格式的转换。
- **request_id 贯穿全链路**：浏览器发的 `id=r1` → E2A 的 `request_id=r1` → E2AResponse 的 `request_id=r1` → 浏览器 event 的 `request_id=r1`。这个 id 是流式分片关联的命脉。
- **AgentLoop 对 ws 零依赖**：loop 只 yield E2AResponse，不触碰 socket；ws 发送边界留在 `server.py`。这让 AgentLoop 可以不起 ws 就单测。
- **Gateway 依赖单向**：`ChannelManager → MessageHandler → AgentClient`，无循环引用。MessageHandler 持自己的 `_robot_messages` Queue 出站，ChannelManager 通过 `dequeue_outbound()` 消费，不反向依赖。

---

## 6. 连接握手与 demux

### 6.1 连接握手

AgentServer 在每条新 WebSocket 连接上，**先发一帧 `connection.ack`**（这不是 E2A 格式，是普通 event 帧）：

```json
{"type":"event","event":"connection.ack","payload":{"status":"ready"}}
```

AgentClient 必须先 `recv()` 消费这帧，再启动 demux 循环。这确保了：
- 客户端知道服务端已就绪
- 避免在服务端未初始化时就发送请求

### 6.2 request_id demux

AgentClient 维护一个 `dict[request_id, asyncio.Queue]`：

```python
class AgentClient:
    self._queues: dict[str, asyncio.Queue] = {}
```

- `_recv_loop`：持续读 ws，把每帧按 `request_id` 投递到对应 Queue
- `send_request_stream`：为指定 request_id 创建 Queue，发请求，然后 yield 直到 `is_final`

这使得**同一个 ws 连接上可以并发多个请求**——每个请求有自己的 Queue，互不干扰。

---

## 7. Twinkle 流式专用模式

Twinkle 只使用流式模式——**没有 unary（单次）响应**。这是简化系统的关键取舍：

- `E2AEnvelope` 没有 `is_stream` 字段，所有请求隐式流式
- `AgentLoop` 只有 `run_stream`，没有 `run_unary`
- `AgentClient` 只有 `send_request_stream`，没有 `send_request`
- `MessageHandler` 只有 `_process_stream`，没有 `_process_unary`
- `MessageHandler` 出站走 `enqueue_outbound` / `dequeue_outbound` Queue 模式（对齐 jiuwenclaw）
- 依赖单向：`ChannelManager → MessageHandler → AgentClient`，无循环引用

### 7.1 流式帧序列

AgentServer 逐帧 yield `E2AResponse`：

```json
// chunk 0 — 流式分片
{"protocol_version":"1.0","request_id":"r1","sequence":0,"is_final":false,
 "status":"in_progress","response_kind":"e2a.chunk","body":{"result":{"content":"你"}}}

// chunk 1 — 流式分片
{"protocol_version":"1.0","request_id":"r1","sequence":1,"is_final":false,
 "status":"in_progress","response_kind":"e2a.chunk","body":{"result":{"content":"好"}}}

// final — 终止帧
{"protocol_version":"1.0","request_id":"r1","sequence":2,"is_final":true,
 "status":"succeeded","response_kind":"e2a.complete","body":{"result":{"content":"你好"}}}
```

**不变量**：
- 同 `request_id` 下 `sequence` 从 0 严格递增
- **恰好一条**记录的 `is_final=true`

### 7.2 错误帧

错误以 `e2a.error` 表示：

```json
{"protocol_version":"1.0","request_id":"r1","sequence":3,"is_final":true,
 "status":"failed","response_kind":"e2a.error","body":{"error":"agent loop exceeded max_steps=8"}}
```

> **为何砍掉 unary**：Twinkle 是单用户 + 单 web 通道，所有交互都是"用户等待逐字输出"的流式场景。unary 模式在 jiuwenclaw 里用于非聊天 RPC（如 `history.get`），但 Twinkle roadmap 不做这些 RPC。保留 unary 只增加了 `if/else` 分支而无实际收益。

---

## 8. method 字段完整取值清单

`method` 是 E2A 信封中最容易混淆的字段——同一字段在不同来源下含义不同。下面分三组列出所有取值。

### 8.1 网关 RPC（Gateway → AgentServer）

这是 jiuwenclaw 项目自定义的 RPC 方法名，定义在 `jiuwenclaw/schema/message.py` 的 `ReqMethod` 枚举中。**不在** `ACP_CLIENT_TO_AGENT_METHODS` 里枚举。

#### 聊天类

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `chat.send` | 发送聊天消息（最常用） | ✅ 唯一在用的 method |
| `chat.resume` | 恢复被中断的对话 | ❌ roadmap 不做 |
| `chat.interrupt` | 中断当前对话 | ❌ roadmap 不做 |
| `chat.user_answer` | 用户回答 Agent 提问 | ❌ roadmap 不做 |

#### 历史与会话类

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `history.get` | 获取对话历史 | ❌ roadmap 不做 |
| `session.list` | 列出所有会话 | ❌ roadmap 不做 |
| `session.create` | 创建新会话 | ❌ roadmap 不做 |
| `session.delete` | 删除会话 | ❌ roadmap 不做 |
| `session.rename` | 重命名会话 | ❌ roadmap 不做 |

#### 命令类（slash commands 调用）

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `command.add_dir` | 添加工作目录 | ❌ roadmap 不做 |
| `command.chrome` | 浏览器操作 | ❌ roadmap 不做 |
| `command.compact` | 压缩上下文 | ❌ roadmap 不做 |
| `command.diff` | 查看变更差异 | ❌ roadmap 不做 |
| `command.ls` | 列出文件 | ❌ roadmap 不做 |
| `command.view` | 查看文件内容 | ❌ roadmap 不做 |
| `command.model` | 切换模型 | ❌ roadmap 不做 |
| `command.resume` | 恢复命令 | ❌ roadmap 不做 |
| `command.session` | 切换会话 | ❌ roadmap 不做 |

#### 配置与路径类

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `config.get` | 获取配置 | ❌ roadmap 不做 |
| `config.set` | 设置配置 | ❌ roadmap 不做 |
| `config.cache_clear` | 清除配置缓存 | ❌ roadmap 不做 |
| `channel.get` | 获取通道信息 | ❌ roadmap 不做 |
| `path.get` | 获取路径 | ❌ roadmap 不做 |
| `path.set` | 设置路径 | ❌ roadmap 不做 |

#### Agent/浏览器运行时类

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `agent.reload_config` | 重载 Agent 配置 | ❌ roadmap 不做 |
| `browser.start` | 启动浏览器运行时 | ❌ roadmap 不做 |
| `browser.runtime_restart` | 重启浏览器运行时 | ❌ roadmap 不做 |
| `initialize` | 初始化 Agent | ❌ roadmap 不做 |

#### 文件传输类

| method | 说明 | Twinkle 是否使用 |
|---|---|---|
| `file.transfer.start` | 开始上传文件 | ❌ roadmap 不做 |
| `file.transfer.chunk` | 传输文件分片 | ❌ roadmap 不做 |
| `file.transfer.complete` | 完成上传 | ❌ roadmap 不做 |

#### Skill 系统（roadmap 全砍）

| method | 说明 |
|---|---|
| `skills.list` / `skills.installed` / `skills.get` | Skill 查询 |
| `skills.install` / `skills.uninstall` / `skills.import_local` | Skill 安装卸载 |
| `skills.marketplace.*` | Skill 市场操作 |
| `skills.skillnet.*` | SkillNet 搜索安装 |
| `skills.clawhub.*` | ClawHub 下载 |
| `skills.evolution.*` | Skill 自进化 |

#### 权限系统（roadmap 推迟）

| method | 说明 |
|---|---|
| `permissions.tools.*` | 工具权限管理 |
| `permissions.enabled.*` | 权限开关 |
| `permissions.rules.*` | 权限规则 |
| `permissions.approval_overrides.*` | 审批覆盖 |
| `permissions.file_guard.*` | 文件守卫 |

#### 多通道配置（roadmap 不做）

| method | 说明 |
|---|---|
| `channel.feishu.*` / `channel.dingtalk.*` / `channel.wechat.*` / `channel.whatsapp.*` / `channel.telegram.*` / `channel.xiaoyi.*` | 各通道配置 get/set |

#### Web-only 方法（不在 ReqMethod 枚举，仅在 `app_web_handlers.py` 注册）

| method | 说明 |
|---|---|
| `config.validate_model` | 校验模型配置 |
| `models.list` / `save` / `remove` / `validate` / `set_active` | 模型管理（5 个） |
| `locale.get_conf` / `set_conf` | 语言配置 |
| `heartbeat.get_path` | 心跳路径 |
| `channel.discord.*` / `channel.wecom.*` | Discord/企微通道配置 |
| `cron.job.*` | 定时任务 CRUD + 预览 + 立即执行（8 个） |
| `permissions.owner_scopes.*` | 权限范围管理 |
| `memory.forbidden.*` | 记忆禁止词管理 |

> 注：jiuwenclaw 的 method 取值总计约 **118 个**，以上分组覆盖了绝大部分。完整权威列表以 `jiuwenclaw/schema/message.py:ReqMethod` 枚举 + `app_web_handlers.py` 的 `register_method` 为准。

### 8.2 ACP JSON-RPC 方法（ACP 客户端转入时）

当消息经 `envelope_from_acp_jsonrpc` 从 ACP 转入时，`method` 承载的是 JSON-RPC method，定义在 `jiuwenclaw/e2a/constants.py` 的 `ACP_CLIENT_TO_AGENT_METHODS`。

| method | 方向 | 说明 |
|---|---|---|
| `initialize` | 客户端→Agent | 初始化会话 |
| `authenticate` | 客户端→Agent | 鉴权 |
| `session/new` | 客户端→Agent | 创建新会话 |
| `session/load` | 客户端→Agent | 加载已有会话 |
| `session/list` | 客户端→Agent | 列出会话 |
| `session/set_mode` | 客户端→Agent | 设置会话模式 |
| `session/set_config_option` | 客户端→Agent | 设置配置项 |
| `session/prompt` | 客户端→Agent | 发送 prompt（最核心） |
| `session/set_model` | 客户端→Agent | 设置模型 |
| `session/fork` | 客户端→Agent | 分叉会话 |
| `session/resume` | 客户端→Agent | 恢复会话 |
| `session/close` | 客户端→Agent | 关闭会话 |
| `logout` | 客户端→Agent | 登出 |

Agent → 客户端方向（`ACP_AGENT_TO_CLIENT_METHODS`）：

| method | 方向 | 说明 |
|---|---|---|
| `session/update` | Agent→客户端 | 会话更新通知 |
| `session/request_permission` | Agent→客户端 | 请求权限 |
| `session/elicitation` | Agent→客户端 | 向用户提问 |
| `fs/read_text_file` | Agent→客户端 | 读文件 |
| `fs/write_text_file` | Agent→客户端 | 写文件 |
| `terminal/create` | Agent→客户端 | 创建终端 |
| `terminal/output` | Agent→客户端 | 终端输出 |
| `terminal/release` | Agent→客户端 | 释放终端 |
| `terminal/wait_for_exit` | Agent→客户端 | 等待终端退出 |
| `terminal/kill` | Agent→客户端 | 杀终端 |

### 8.3 A2A 方法（A2A Agent 转入时）

A2A 协议使用 `SendMessage` 等抽象操作，经 `envelope_from_a2a_send_message` 转入 E2A 后，`method` 字段的取值取决于适配层的映射策略。A2A 本身没有固定的 method 字符串列表——其核心操作语义是 `task`、`message`、`stream_event`，这些在 E2A 响应侧通过 `response_kind`（`a2a.task` / `a2a.message` / `a2a.stream_event`）表达，而不是通过请求侧的 `method`。

### 8.4 特殊值

| method | 说明 |
|---|---|
| `null` | 心跳等无 RPC 枚举的场景（jiuwenclaw 中允许 method 为 null） |
| `ext` + `ext_method` | 自定义扩展方法：当 `method == "ext"` 时，真实方法名在 `ext_method` 字段 |

### 8.5 Twinkle 当前与未来

Twinkle 当前**只使用 `chat.send`**——这是唯一在 `Message.method` 默认值、`web_channel.py` fallback、测试、agent_loop 中出现的 method。

Roadmap 中可能逐步引入的 method：

| 未来 method | 对应 roadmap 阶段 | 说明 |
|---|---|---|
| `chat.interrupt` | Phase 2+ | 中断当前 agent loop |
| `history.get` | Phase 3+ | 获取对话历史（配合上下文压缩） |
| `command.compact` | Phase 3 | 手动触发上下文压缩 |

其余 jiuwenclaw method（skill 系统、多通道、权限、文件传输等）明确不在 Twinkle roadmap 范围内，需要时参考 jiuwenclaw 对应实现即可。

---

## 9. 与 ACP、A2A 的关系

E2A 不是孤立存在的。在 jiuwenclaw 的设计中，它是 ACP 和 A2A 的**统一桥接层**：

```
ACP 客户端 ──JSON-RPC──→ Gateway ──envelope_from_acp_jsonrpc──→ E2AEnvelope
                                                           provenance.source_protocol = "acp"

A2A Agent ──SendMessage──→ Gateway ──envelope_from_a2a_send_message──→ E2AEnvelope
                                                           provenance.source_protocol = "a2a"

Web 浏览器 ──req/res──→ Gateway ──直接映射──→ E2AEnvelope
                                                           provenance.source_protocol = "e2a"
```

| 维度 | ACP | A2A | E2A（对内） |
|---|---|---|---|
| 角色 | JSON-RPC 会话协议 | Task/Message/Card 协议 | 统一信封；外部经适配器**转入** |
| `method` | JSON-RPC method | 抽象操作 | 网关 RPC 或 ACP method（见 §8） |
| 扩展 | `_meta` | `metadata` | 网关用规范化字段；`a2a_metadata`/`acp_meta` 仅互操作 |

Twinkle roadmap 明确不做 ACP/A2A 互操作，所以这些适配器在 Twinkle 里不存在。但 E2A 的字段命名（如 `provenance`、`a2a_metadata`）已经预留了位置——将来需要时参考 jiuwenclaw 的 `adapters.py` 实现即可。

---

## 10. E2A 的设计哲学

### 10.1 单一真源原则

E2A 的协议说明以 `models.py` 中的数据结构为**最终实现源**，文档（`E2A-protocol.md`）是规范性说明。冲突时以代码为准，回头修正文档。

这避免了"文档和代码不一致"的经典问题——在 jiuwenclaw 中，`E2A-protocol.md` 开头就声明了这条规则。

### 10.2 params 是唯一业务参数字典

E2A 的一个重要约定：**所有业务参数都放在 `params` 里**。

- 用户输入（`query`/`text`/`content`）
- 多模态内容（`content_blocks`）
- 附件（`files`/`attachments`）
- RPC 选项（`mode`/`page_idx`）

**不再使用顶层 `payload`**。旧版 `payload` 的键在 `from_dict` 中会合并进 `params`（不覆盖已有键），但新协议不应再发顶层 `payload`。

### 10.3 流式分片是命脉

E2A 的核心价值不只是"统一格式"，而是**流式分片 + 请求关联**：

- `request_id` 贯穿请求和所有响应帧
- `sequence` 从 0 严格递增
- `is_final` 标记终止
- `response_kind` 区分载荷类型

这让一条 WebSocket 上可以并发多个流式请求，每个请求的分片按 `request_id` demux 到各自的消费者。

---

## 11. Twinkle vs. jiuwenclaw E2A 对比速查

| 维度 | jiuwenclaw | Twinkle |
|---|---|---|
| E2AEnvelope 字段数 | 20+（含 provenance/auth/file/a2a/acp/is_stream） | 6（只保留核心，无 is_stream） |
| E2AResponse 字段数 | 15+（含 projections/metadata/a2a/acp） | 8（只保留核心，is_stream=True 固定） |
| 响应模式 | 流式 + 单次（unary），通过 is_stream 切换 | **流式专用**，无 is_stream 切换 |
| response_kind | 11 种（含 ACP/A2A/cron/ext） | 3 种（e2a.chunk/complete/error） |
| 序列化 | dataclass + 手写 to_dict/from_dict + legacy fallback | Pydantic BaseModel + model_dump_json/model_validate_json |
| wire_codec | 完整（legacy AgentChunk/AgentResponse 兜底） | 无（不需要兼容旧格式） |
| adapters | 完整（ACP↔E2A、A2A↔E2A 双向适配） | 无（roadmap 不做互操作） |
| provenance | E2AProvenance（source_protocol/converter/details） | 无（单通道不需要出处标记） |
| 鉴权 | E2AAuth（bearer_token/credential_ref 等） | 无（roadmap 推迟） |
| 文件传输 | E2AFileRef + 文件传输协议常量 | 无（roadmap 不做） |
| demux | 按 request_id + asyncio.Queue | 同（对齐 jiuwenclaw） |
| 连接握手 | connection.ack | 同（对齐 jiuwenclaw） |

---

## 12. 将来演进方向

E2A 在 Twinkle 中当前是最小子集，但接口形状已经为演进留了位置：

| 演进方向 | 涉及的 E2A 变化 | roadmap 阶段 |
|---|---|---|
| 多通道接入 | `channel` 字段从 `"web"` 扩展为 `"feishu"`/`"dingding"` 等 | 后续（参考 jiuwenclaw） |
| ACP/A2A 互操作 | 加 `provenance`、`adapters`、`wire_codec` | 不在当前范围 |
| 长期记忆 | E2A `params` 可带 `memory_query` 等 | Phase 3+ |
| 上下文压缩推送 | server-push 通道（同一条 ws，`metadata` 标记区分） | Phase 3 |
| 鉴权 | 加 `auth` 字段 | 不在当前范围 |
| 分布式追踪 | 加 `correlation_id` | 不在当前范围 |

> 核心原则：**E2AEnvelope 和 E2AResponse 的核心字段（request_id、sequence、is_final、status、response_kind、body）已钉死，不会回炉**。演进只在"扩展字段"方向，调用方不需要改动。

---

## 13. 参考

- [Twinkle E2A 实现](../twinkle/e2a/models.py) — 最小子集，7 字段 Pydantic 模型
- [Twinkle Phase 0 设计](phase-0-design.md) §3.1 — E2A 子集的协议说明
- [jiuwenclaw E2A-protocol.md](../../jiuwenclaw/docs/zh/E2A-protocol.md) — 完整协议规范（12 节）
- [jiuwenclaw e2a/models.py](../../jiuwenclaw/jiuwenclaw/e2a/models.py) — 完整实现（E2AEnvelope 20+ 字段）
- [jiuwenclaw e2a/constants.py](../../jiuwenclaw/jiuwenclaw/e2a/constants.py) — response_kind、status、source_protocol 常量

---

*本文与 `twinkle/e2a/models.py` 及 `jiuwenclaw/docs/zh/E2A-protocol.md` 同步维护。*
