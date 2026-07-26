# 工具权限审批设计与实现

## 一句话概括

工具调用前经 `PermissionEngine` 判定 `ALLOW` / `DENY` / `ASK`;`ASK` 时 `AgentLoop` 挂起、`yield e2a.ask`、等前端人类决策回灌 `approval.respond` 后恢复——人类-in-the-loop 的安全切面，挂在 `before_tool_call` Hook 上，核心 ReAct 循环零改动。

---

## 为什么需要权限确认

ReAct 循环让 agent 能自主调工具，但工具的风险不对等：`web_fetch` 只读网页，`command_exec` 却能改文件系统、起进程、连外网。一律放行不安全，一律拒绝又让 agent 失能。需要一个机制：

1. **分级判定** —— 每个工具有自己的档位（放行 / 拒绝 / 需审批），配置驱动而非硬编码。
2. **人类兜底** —— 高危操作（如 `command_exec`）在执行前暂停，把"该不该跑"交给人类决定，再继续。
3. **挂起不阻塞** —— agent 在等人决策时不能堵死连接，否则后续消息（包括审批结果本身）进不来。
4. **决策留底** —— 每次 allow / deny / ask 都写审计，事后可查。

权限审批不是一个独立模块，而是挂在 `before_tool_call` 生命周期上的一个 Hook（`PermissionHook`，priority=100）。Hook 机制本身见 [`hook-design.md`](./hook-design.md)——本文只讲审批这一条切面。

---

## 设计来源

对照 jiuwenswarm 的权限层，Twinkle 做了**同构但裁剪**的实现：

| jiuwenswarm | Twinkle | 说明 |
|---|---|---|
| `permissions/*` 引擎 + `builtin_rules.yaml` | `permissions/` 包（7 个 .py） | 同样的"判定 + 规则 + 覆盖 + 审计"四件套，规则从 YAML 换成 Python + JSON 配置 |
| DB-backed 审计表 | `permission_audit.jsonl` 追加文件 | 砍掉 DB 依赖，文件即可查；代价是无索引 |
| shell AST 解析做命令安全 | `builtin_rules.py` 17 条正则 deny | 砍掉 AST，用正则黑名单 + 工作区 confinement 兜底 |
| 3-axis path confinement | 不实现 | command_exec 只靠 `workdir` 沙箱 + 黑名单 |
| `ToolInterruptException` | `HookInterrupt` | HITL 挂起信号，语义相同名字不同 |

选择裁剪的原因：Twinkle 是学习型重实现，目标是把"判定 → 挂起 → 恢复"这条主链跑通，DB 索引 / AST / 三轴路径在没有规模化和复杂命令场景前是纯成本。

---

## 三层判定：ALLOW / DENY / ASK

`PermissionLevel` 是三个字符串常量（不是枚举），对应三种控制流——和 [`hook-design.md`](./hook-design.md) §控制流信号一一对应：

| 决策 | Hook 控制流信号 | 效果 |
|---|---|---|
| `ALLOW` | no-op | 工具正常执行 |
| `DENY` | `ctx.request_force_finish(deny_message)` | `@hook` 短路，deny 消息直接作为 `tool_result` 回灌 LLM |
| `ASK` | `raise HookInterrupt(approval_id, tool, args, ...)` | 挂起 run_stream，等人类审批后恢复 |

```python
# hooks/builtin/permission_hook.py —— before_tool_call
decision = self._engine.check(tool=inputs.name, args=inputs.args,
                              channel=get_permission_channel(), ...)
if decision.level == "deny":
    ctx.request_force_finish(decision.deny_message)        # 短路
elif decision.level == "ask":
    raise HookInterrupt(message="approval required",
        data={"approval_id": str(uuid.uuid4()), "tool": ...,
              "args": ..., "tool_call_id": ..., "reason": decision.reason})
# allow → no-op
```

`approval_id` 在这一步由 `uuid.uuid4()` 新生成——它是后续挂起/恢复的**承载标识**（见 §挂起/恢复）。

### bypass：恢复后不再拦截

ASK 恢复后 agent 会**重调**同一个工具调用。为避免 `before_tool_call` 又判定一次 ASK 进入死循环，`PermissionHook` 入口先查 bypass：

```python
if inputs.tool_call_id in ctx.extra.get("_approved_tool_call_ids", set()):
    # 已批准过本次——直接放行，不再 engine.check
    if ctx.extra.get("_approval_decision") == "allow_always":
        await self._engine.persist_allow_always(...)   # 顺带持久化覆盖
    return
```

`_approved_tool_call_ids` 由 `agent_loop` 在恢复时写进 `ctx.extra`（见 §挂起/恢复），跨 Hook 通信走 `extra` 字典而非 ContextVar——因为同一次 `execute()` 调用链共享同一个 `HookContext` 对象。

---

## 配置

单一环境变量 `TWINKLE_PERMISSIONS`，读 JSON：

```json
{
  "enabled": false,                       // 总开关，false = 系统关：全 ALLOW 不审计
  "enabled_channels": ["web"],            // 仅这些通道走判定；其余通道全 ALLOW passthrough
  "global_default": "allow",              // 档位表里没列的工具的兜底档位
  "tools": {
    "command_exec": "require-approval",    // → 归一化为 ASK
    "web_fetch": "allow",
    "web_search": "allow",
    "todo_create": "allow", "todo_complete": "allow", "todo_list": "allow"
  },
  "rules": [],                             // 用户自定义 deny 规则 {tool, pattern, reason, id}
  "approval_overrides": {}                 // 仅默认值占位；运行时覆盖来自独立文件
}
```

三种形态：bare `true`/`false` 快速开关，或完整 JSON 对象 merge 到默认值；坏 JSON 静默回退默认（永不崩）。

派生常量（`config.py`）：

| 常量 | 用途 |
|---|---|
| `PERMISSIONS_ENABLED` | 总开关布尔 |
| `PERMISSIONS_ENABLED_CHANNELS` | 通道白名单 set |
| `PERMISSIONS_GLOBAL_DEFAULT` | 兜底档位 |
| `PERMISSIONS_TOOLS` | per-tool 档位 dict |
| `PERMISSIONS_RULES` | 用户 deny 规则 list |
| `PERMISSION_OVERRIDES_FILE` | allow_always 运行时覆盖文件（mtime 热重载） |
| `PERMISSION_AUDIT_FILE` | 审计 JSONL 路径 |

**档位值**是字符串标签：`"allow"` / `"deny"` / `"require-approval"`。其中 `require-approval` 在 `policy.check` 内部被归一化成 `ASK`——引擎永远看不到 `require-approval` 这个 level，只看 `allow` / `deny` / `ask`。没有单独的 read/write/dangerous 三轴分类，"危险"信号只有 `command_exec` 的 builtin deny + `require-approval` 档位两种。

---

## 判定流：policy 四层合并

`PermissionEngine.check` 只做**通道门控 + 审计 + 委派**：未启用或通道不在白名单 → 直接 ALLOW passthrough（不写审计）；否则委派给 `PermissionPolicy.check` 做四层合并判定，并写一条审计。

```
              tool call (name, args, channel)
                       │
            ┌──────────▼──────────┐
            │  PermissionEngine    │
            │  enabled 且 channel  │
            │  ∈ enabled_channels? │
            └─────┬─────────┬──────┘
              否  │         │ 是
                  ▼         ▼
            ALLOW         PermissionPolicy.check(tool, args)
          (passthrough,      │
           不审计)     ┌────┴─────────────────────────────┐
                   ① allow_always override 命中?           │  ← mtime 热重载文件
                      是 → ALLOW  (source=override)        │
                      否 ↓                                  │
                   ② command_exec 走 builtin_rules.matches?│  → DENY (source=rule)
                      否 ↓                                  │
                   ③ 用户 deny rules 正则匹配 str(args)?    │  → DENY (source=rule)
                      否 ↓                                  │
                   ④ tier = tools[name] 或 global_default   │
                      "require-approval" → ASK               │
                      "allow"/"deny"      → 对应 level      │
                      └────────────────────────────────────┘
                                │
                                ▼
                  PermissionDecision{level, reason, source,
                                     rule_id?, deny_message?}
```

四层的**优先级是固定的**：覆盖 > 拒绝 > 档位。这意味着 allow_always 能盖过 deny 规则——这是刻意的（用户明确"永久放行"就是要绕过档位），但 `command_exec` 的 allow_always 有额外护栏（见 §allow_always）。

`builtin_rules.matches()` 是 `command_exec` 专用的 deny 单一来源（17 条正则，如 `rm -rf`、`dd`、`mkfs`、`chmod 000` 等）；它同时被 `command_exec` 工具本身和 policy 读，保证两边判定一致。

---

## 挂起 / 恢复：完整时序

这是审批机制最复杂的场景，也是它和普通 Hook 的区别：`HookInterrupt` 不只是"中断"，而是"挂起等人"。

### 承载标识：approval_id 而非 request_id

审批响应是一个**独立的浏览器请求**，有自己的 `request_id`（记作 **R2**），和被挂起的那条 `chat.send`（`request_id` 记作 **R**）不同。如果 Future 用 `request_id` 做 key，R2 的 `approval.respond` 根本找不到 R 的挂起流。

所以 Future 用 **`approval_id`** 做 key——它由 `PermissionHook` 在抛 `HookInterrupt` 时生成，贯穿 `e2a.ask` 帧 → 前端卡片 → `approval.respond` params → `ApprovalRegistry.resolve`，是唯一能跨 R / R2 两侧关联的东西。

```
 Browser            Gateway              AgentServer(ws_handler)       AgentLoop._inner_run_stream
   │                  │                        │                              │
   │── chat.send(id=R)──►                      │                              │
   │                  │── E2AEnvelope(req=R) ──►                              │
   │                  │                        │── create_task(run_task) ──► │
   │                  │                        │                   ... tool_call
   │                  │                        │                   → before_tool_call(PermissionHook)
   │                  │                        │                   → engine.check == ASK
   │                  │                        │                   → raise HookInterrupt(approval_id)
   │                  │                        │                   except: APPROVAL_REGISTRY.register(approval_id)→Future
   │                  │                        │                   yield e2a.ask{approval_id, tool, args, ...}  (req=R)
   │◄── event approval.ask (req=R) ────────────│◄────────────────────────────│
   │                  │                        │                              │
   │  ApprovalCard 渲染; inputDisabled=true    │                              │  ★ decision = await future
   │  用户点 [放行一次/永久放行/拒绝]           │                              │      (挂起 —— 不阻塞 ws_handler)
   │                  │                        │                              │
   │── req(id=R2, method=approval.respond,     │                              │
   │       params={approval_id, decision, ...})►                              │
   │                  │── E2AEnvelope(req=R2) ─►│                              │
   │                  │                        │ handle_respond: resolve(approval_id, decision)
   │                  │                        │   fut.set_result(decision) ─────► 唤醒 await
   │                  │                        │ ◄── e2a.result ack (req=R2, is_final) │
   │◄── event result (req=R2) ─────────────────│                              │
   │                  │                        │                              │  decision ∈ {allow, allow_always}?
   │                  │                        │                              │    是 → extra 标记 bypass，
   │                  │                        │                              │         重调 _hooked_tool_call → 放行
   │                  │                        │                              │    否 → result = "[tool denied by user] ..."
   │                  │                        │                              │  tool_result → store.append → 继续 ReAct
   │                  │                        │◄── e2a.chunk / e2a.complete (req=R) ─│
   │◄── chat.delta / chat.final (req=R) ───────│                              │
```

### 关键不变式

- **挂起的 run_stream 在原 R 上恢复** —— 恢复后产出的 `e2a.chunk` / `e2a.complete` 仍带 `request_id=R`，前端 rid 守卫（`rid !== lastRequestId` 则丢）才会把它们正确放进同一条聊天气泡。
- **审批响应用独立的 R2** —— `webClient.respond()` 故意绕过 `send()`，避免 `lastRequestId` 被改成 R2 而让后续 R 的 delta 被丢弃。
- **`approval_id` 是 Future key** —— 跨 R/R2 关联的唯一标识；`original_request_id` 字段前端发了但后端不读（仅前端用于绕开 `lastRequestId`）。
- **ws_handler 并发 per-request task** —— `chat.send` 每条 `asyncio.create_task(run_task)`，所以 `await future` 挂起时 ws 读循环仍在转，`approval.respond` 能被立刻处理。
- **`approval.respond` 路由优先** —— 在 `ws_handler` 里它排在 session-RPC 和"该 session 已有活跃请求"守卫**之前**，所以审批响应永远不会被"请求进行中"挡掉。

### 断连清理

`ws_handler` 的 `finally` 块取消所有活跃 task 并调 `APPROVAL_REGISTRY.cancel_all()`——所有未决 Future 被 cancel，挂起的 `await future` 抛 `CancelledError`，run_task 静默退出。这是目前唯一的"超时"路径：**主动断连**。静默离开 / 进程重启没有 TTL 回收（见 §设计决策回顾 的取舍）。

---

## 数据模型

### 出站：`e2a.ask` 帧

`E2AResponse` 没有专用的 ask 模型，就是 `response_kind="e2a.ask"` + 一个 body：

```json
{
  "approval_id": "uuid4",
  "tool": "command_exec",
  "args": {"command": "rm -rf /tmp/x"},
  "tool_call_id": "call_abc",
  "reason": "tier:require-approval"
}
```

`is_final=false`、`status="in_progress"`——run 还在挂着，没结束。`sequence` 严格递增。

### 入站：`approval.respond` 信封

```json
{
  "request_id": "apr_...",          // R2, 浏览器生成
  "method": "approval.respond",
  "params": {
    "approval_id": "uuid4",
    "decision": "allow" | "allow_always" | "deny",
    "original_request_id": "R",      // 前端发, 后端不读
    "session_id": "..."
  }
}
```

后端 `handle_respond` 只读 `approval_id` + `decision` 两个字段，其余忽略。

### 决策值

`"allow"` / `"allow_always"` / `"deny"`——这是**代码里的真实值**（`webClient.ts` 类型、`agent_loop` 分支、`ApprovalCard` 按钮一致）。注意文档和架构图里常把"放行一次"写成 `allow_once`，那是文案名，不是代码值；实际发出的字符串是 `allow`。

### ack：`e2a.result`

`handle_respond` 回一条 `response_kind="e2a.result"`、`is_final=true`、`request_id=R2` 的帧，body `{"type":"approval.respond","approval_id":...,"accepted":bool}`。失败（`approval_id` 未知 / 已过期）则 `status="failed"`、`accepted=false`、带 `error`。

### 前端卡片

`ApprovalCard.vue` 三个按钮 → `decide('allow' | 'allow_always' | 'deny')`：放行一次 / 永久放行 / 拒绝。决策后按钮换成结果标签；`useSessions.markApprovalDecided()` 原地翻 `decided` 字段并解除 `inputDisabled`。

---

## allow_always 持久化 + 热重载

"永久放行"要把决策存下来下次直接放行，避免每次都问。存进 `PERMISSION_OVERRIDES_FILE`（默认 `<WORKSPACE>/.twinkle_data/permission_overrides.json`）。

**热重载**：`PermissionPolicy._load_overrides()` stat 文件 mtime，变了才重读 JSON 进缓存。`persist_allow_always` 写完把 `self._mtime = -1.0` 强制下次重载。好处：外部手编这个文件立即生效，无需重启；多 worker 各自 stat 也最终一致。

**command_exec 的护栏**：bless 一个命令会生成 `head + " *"` 的 glob 模式（`head` = 命令前两个空白分隔 token，故意不用 `shlex` 以免把 Windows 路径 `C:\Users` 嚼成 `C:Users`）。但**拒绝 bless 含 shell 元字符的命令**：

```python
_SHELL_METACHARS = frozenset(";&|<>`$\n")
```

原因：否则一个持久化的 `"npm run *"` 会 bless `"npm run build && rm -rf /"`——glob 匹配到了但后半段是危险链。含元字符的命令 fall through 给 deny 规则 / 档位，不进覆盖表。

非 command_exec 工具的 allow_always 直接写 `ovr[tool] = "allow"`。

**写入路径**：`engine.persist_allow_always` → `policy.persist_allow_always` → `asyncio.to_thread(write_text)`（不阻塞事件循环）→ 强制 mtime 重载。引擎把持久化委托给 policy，自己只管"判 + 审计"。

---

## 审计

`ToolPermissionLog` 往 `permission_audit.jsonl` 追加 JSONL。`ToolPermissionLogEntry` 字段：`tool, decision, source, rule_id, reason, user_decision, channel, session_id, request_id, ts`（`to_dict()` 即 `asdict`，不含 `args` / `approval_id`）。

每次 `engine.check` 写一行（check 行），`user_decision=null`：

```json
{"ts": 1753512000.0, "tool": "command_exec", "decision": "ask",
 "source": "tier", "rule_id": null, "reason": "tier:require-approval",
 "user_decision": null, "channel": "web", "session_id": "...", "request_id": "..."}
```

`engine.persist_allow_always` 会再写一行（persist 行），`user_decision="allow_always"`、`source="override"`、`reason="allow_always persisted"`。各路径的审计行数：

| 决策路径 | 审计行数 |
|---|---|
| ALLOW / DENY | 1（check 行） |
| ASK → allow（一次）/ ASK → deny | 1（check 行） |
| ASK → allow_always | 2（check 行 + persist 行） |

fail-soft：写盘出错只 `log.warning`，不阻断判定。审计**只追加、无查询面**——没有 RPC、没有 UI，只能直接看文件，这是 jiuwenswarm DB 审计被裁掉后的取舍。

---

## 文件地图

| 文件 | 角色 |
|---|---|
| `agentserver/permissions/models.py` | `PermissionLevel` + `PermissionDecision` + `ToolPermissionLogEntry` 纯数据 |
| `agentserver/permissions/builtin_rules.py` | `COMMAND_DENY_PATTERNS`（17 条正则）+ `matches()`——command_exec deny 单一来源 |
| `agentserver/permissions/policy.py` | `PermissionPolicy`：四层合并 + allow_always 持久化 + mtime 热重载 |
| `agentserver/permissions/engine.py` | `PermissionEngine`：通道门 + 审计 + 委派 policy + 委派 persist |
| `agentserver/permissions/audit.py` | `ToolPermissionLog`：JSONL 审计，fail-soft |
| `agentserver/permissions/approval_registry.py` | `ApprovalRegistry` + `APPROVAL_REGISTRY` 单例：approval_id → Future |
| `agentserver/permissions/__init__.py` | re-export + `permission_engine()` 装配器 |
| `agentserver/permission_context.py` | `APPROVAL_CHANNEL` ContextVar + `get/set_permission_channel()` |
| `agentserver/hooks/builtin/permission_hook.py` | `PermissionHook`（priority=100）：bypass / check / 三决策分派 |
| `agentserver/agent_loop.py` | 工具调用段（~218-279）：捕获 `HookInterrupt` → 注册 Future → yield `e2a.ask` → `await future` → 恢复重调 |
| `agentserver/server.py` | `ws_handler` 并发路由：`approval.respond` 优先直派 `handle_respond`；`finally` → `cancel_all()` |
| `e2a/models.py` | `E2AResponse` 的 `response_kind` 含 `e2a.ask`；`E2AEnvelope.params` 自由 dict |
| `gateway/message_handler.py` | `e2a.ask` → `approval.ask` 事件翻译 |
| `gateway/web_channel.py` | `approval.ask` 广播 / `approval.respond` 入站建 Message |
| `web/src/components/ApprovalCard.vue` | 三按钮审批卡 |
| `web/src/services/webClient.ts` | `respond()` 绕过 `send()`，避免污染 `lastRequestId` |
| `web/src/composables/useSessions.ts` | `onApprovalAsk` 推审批气泡 / `markApprovalDecided` 翻状态 |
| `config.py` | `TWINKLE_PERMISSIONS` 解析 + 派生常量 |

---

## 与 jiuwenclaw 的差异

| | jiuwenswarm | Twinkle |
|---|---|---|
| 审计存储 | DB 表（可索引查询） | JSONL 追加文件（无查询面） |
| 命令安全 | shell AST 解析 | 17 条正则 deny + 工作区 confinement |
| 路径 confinement | 3-axis 路径模型 | 不实现（command_exec 靠 `workdir` 沙箱） |
| 覆盖持久化 | DB / 配置中心 | 磁盘 JSON + mtime 热重载 |
| 挂起信号 | `ToolInterruptException` | `HookInterrupt`（语义同，名字不同） |
| Future key | （对照项） | `approval_id`（不是 request_id），跨 R/R2 关联 |
| 通道 | 多通道 | 仅 `web`（通道门控已留接口，非 web 通道无审批 UX） |

砍掉 DB / AST / 3-axis 的原因：学习型重实现优先把"判定 → 挂起 → 恢复"主链跑通；这些能力在没有规模化审计查询、复杂链式命令、多通道审批场景前是纯成本。

---

## 设计决策回顾

### 为什么审批是 Hook 而非硬编码进 agent_loop

权限判定是典型的"横切需求"——它在工具执行前后介入，但 ReAct 循环本身（思考 → 调工具 → 读结果 → 再思考）不该知道它。挂成 `before_tool_call` Hook 后，`agent_loop` 只需处理 `HookInterrupt` 这一个信号，权限策略变化（加规则、改档位、换通道）不动核心循环。详见 [`hook-design.md`](./hook-design.md)。

### 为什么 Future 用 approval_id 而非 request_id

审批是独立请求（R2），和被挂起的 `chat.send`（R）不同 id。用 `request_id` 做 Future key，R2 的响应找不到 R 的挂起流。`approval_id` 是 `PermissionHook` 抛中断时生成的 uuid，贯穿两侧，是唯一能跨 R/R2 关联的标识。`ApprovalRegistry` 的 docstring 把这一点钉死了。

### 为什么用 R / R2 双 request_id

恢复后产出的 `e2a.chunk` / `e2a.complete` 必须带原 R，前端 rid 守卫才认、才放进同一条气泡。如果审批响应也复用 R，它和恢复后的流式帧会抢同一个 `request_id`，前端 demux 会把 ack 当成 delta 混进气泡。R2 让审批 ack 走独立的 `result` 事件、独立的 pending resolver，不污染聊天流。

### 为什么 ws_handler 并发化

挂起的 `await future` 如果在 ws 读循环里，`approval.respond` 永远进不来，死锁。`asyncio.create_task(run_task)` 把每个 `chat.send` 丢成独立 task，读循环始终在转，`approval.respond` 被立刻路由进 `handle_respond`。代价是要管 `active[sid]` 字典（每 session 一个活跃 task）和 `finally` 清理——但这是 HITL 的硬需求。

### 为什么 mtime 热重载而非进程内缓存

allow_always 覆盖要跨重启存活（存磁盘），又要能即时生效（手编文件不重启）。mtime stat 是最便宜的"变了就重读"机制，单进程内无锁、多 worker 各自 stat 最终一致。代价是每次 check 一次 `os.path.getmtime`——审批路径本就低频，可忽略。

### 为什么 command_exec 的 allow_always 拒绝 shell 元字符

bless 是把"命令头 + ` *`"写进覆盖表，下次同头的命令直接放行。如果允许 bless 含 `;&|` 的命令，`"npm run *"` 会 bless `"npm run build && rm -rf /"`——glob 匹配到了，后半段危险链却绕过了 deny 规则。元字符命令 fall through 给 deny 规则 / 档位重新判定，不进覆盖表，是这个风险的唯一闸门。

### 取舍：没有审批超时 TTL

目前唯一的"超时"是**主动断连**（`finally` → `cancel_all()`）。用户静默离开但不断连、或进程重启，挂起的 Future 不会被回收——run_task 会一直挂着等一个永不到来的 decision。这是当前设计的取舍：回收路径只挂在 ws 生命周期上，没有 per-approval 的 TTL。
