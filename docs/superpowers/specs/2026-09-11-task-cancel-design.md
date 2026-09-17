# 用户取消正在运行的 agent 任务 — 设计

> 日期：2026-09-11
> 状态：设计已与用户确认，待写实现计划
> 参考：jiuwenswarm（`D:\code\opensource\gitcode\jiuwenswarm`，框架 `openjiuwen` pip 包 + 应用层 `jiuwenclaw/`）

## 1. 背景与目标

Twinkle 当前**不支持**用户主动取消正在运行的 agent 任务。agent loop 一旦开始，只能等它自然结束（LLM 给出最终回答），或被内部循环防护 `force_finish`（重复检测 / 权限拒绝）触发。

而 `force_finish` **不能**被"用户取消"复用——它只在"循环边界"检查（`before_model_call` / `on_model_exception`），检查不到两个真正的阻塞点：LLM 流式 `await` 和 tool 执行 `await`。

本设计实现：**用户按"停止"按钮，立即停止正在运行的 agent 任务**，参考 jiuwenswarm 的实现。

### 成功标准

- 用户在任务运行中按"停止"→ 前端立即 `busy=false` + 显示"已取消"提示
- agent loop 被中断，history 追加"请求被取消"标记，checkpoint 保留最近状态
- 用户续发消息能 resume 续跑（复用 P1 崩溃恢复）
- 无活跃 task 时取消幂等 no-op
- agent.py **零改动**（复用已有 CancelledError 传播 + interrupt snapshot + checkpoint）

## 2. 需求决策

| # | 决策点 | 选择 | 理由 |
|---|---|---|---|
| 1 | 范围 | 只做 `cancel`（不做 pause/resume/supplement 完整 interrupt 接口） | Twinkle 已有 P1 崩溃 resume；用户表述"终止"=cancel；YAGNI |
| 2 | 取消时机 | 立即停（`asyncio.Task.cancel()` 在 await 处打断） | 复用已有 CancelledError 传播 + interrupt snapshot + checkpoint；最简 |
| 3 | 断连行为 | 浏览器断连**不**自动取消 | 对齐 jiuwenswarm；最简；只做按钮取消 |
| 4 | 历史续跑 | 丢半截可续跑 | 复用已有 interrupt snapshot + checkpoint + P1 resume；不加 todo 快照/recovery |
| 5 | 实现方案 | 方案 A 两段式（gateway 合成 final + 转发 cancel） | 前端立即响应；不改 run_task 异常路径；对齐 jiuwenswarm |

## 3. 参考实现摘要（jiuwenswarm）

### 数据流

```
前端 Cancel 按钮 → chat.interrupt{intent:"cancel"} (同一浏览器 ws)
  → Gateway 按 session_id 定位流式任务 → task.cancel() + 转发全新 interrupt 信封(fire-and-forget)
  → AgentServer process_interrupt(cancel) → rail.abort() 置标志 + instance.abort() 触发框架 task.cancel
  → agent loop 检查点(before_model/before_tool) 抛 CancelledError，或进行中 await 被 cancel
  → Gateway 合成 chat.final + interrupt_result + processing_status(false) 告知前端
```

### 关键设计点

- **定位**：按 `session_id`（不用原 request_id；转发给 AgentServer 用全新 interrupt rid）
- **中断**：流式主聊天用 flag+Event 检查点（`stream_event_rail` 的 `before_model_call`/`before_tool_call`）+ 框架 `TaskScheduler.cancel_task` 兜底打断进行中 await
- **streaming 停止**：E2AResponse 无 `canceled` 状态，靠 gateway 合成 `chat.final`+`interrupt_result`+`processing_status`
- **状态**：取消时 checkpoint + todo 快照/隔离 + `recovery.json`（为智能 resume，重）
- **断连**：浏览器 ws 关闭不自动取消；CLI/TUI 断连 + AgentServer 进程侧 ws 断连才取消

### 框架层 vs 应用层

- 框架层 `openjiuwen`：`TaskScheduler.cancel_task` + 分层 CancelledError 处理（react_agent 重抛 / ability_manager 吞单 tool 为 ToolMessage / rail 跳 after / workflow 置 CANCELLED）
- 应用层 `jiuwenclaw`：`JiuClawStreamEventRail` 的 `_abort_requested`+`_pause_event` 检查点 + checkpoint/recovery 增强

> 注：Twinkle 是精简学习版，不照搬框架层 `TaskScheduler`/rail 检查点；走单一 `asyncio.Task.cancel()` 路径，agent 层零改动。

## 4. 现状摘要（Twinkle）

### 可复用基础设施

- `asyncio.Task` per session 在 `active: dict[str, asyncio.Task]`（`server.py:134`，session_id→task），task 可 `.cancel()`
- `CancelledError` 在 agent loop、`@hook` 装饰器、LLM stream 中正确传播（不吞）
- `_build_interrupt_snapshot()`（`agent.py:817-847`）已预留"请求被取消"语义（`finally` 块 `completed_normally==False` 时调用）
- 每步 `save_checkpoint()`（`agent.py:535`）保证取消后状态可恢复
- `request_id` demux 在 AgentClient（`_queues: dict[str, asyncio.Queue]`，`agent_client.py:28`）完备
- `APPROVAL_REGISTRY.cancel_all()` ws 断连清理（`server.py:220`）

### 关键代码确认

- `server.py:158` `except Exception` **不捕获 CancelledError**（Py3.8+ CancelledError 是 BaseException 子类）→ task.cancel 后 run_task **不发 error 帧**，CancelledError 进 finally（L163-164 减计数）后 task 自然结束。**方案 A 不用改 run_task。**
- `message_handler.py:40` `asyncio.create_task(self._process_stream(envelope, msg))` **fire-and-forget，不存 task 引用** → 方案 A gateway 侧需补 task 注册表
- `force_finish`（`RepeatToolCallDetectorHook`/`PermissionHook` 触发）是内部循环防护，只在循环边界检查，**非用户取消，不复用**

## 5. 设计

### 5.1 协议层

- 新增 E2A method **`task.cancel`**，params: `{session_id}`。走现有 `E2AEnvelope`，不加新字段。
- **不新增 response_kind**（不加 `e2a.cancelled`）。取消的结束信号由 **gateway 合成 `chat.final` 帧**给前端（对齐 jiuwenswarm）。
- AgentServer 对 `task.cancel` 回一个 `e2a.result` ack，但 gateway fire-and-forget 不等它。

### 5.2 前端

**`web/src/services/webClient.ts`**

- 加 `cancel()` 方法：用 `cancel_` 前缀 id（仿现有 `respond()` 绕过 `lastRequestId` 污染，避免取消帧触发 delta/final 的 `rid !== getLastRequestId()` 守卫误判），发 `{type:'req', id:'cancel_<ts>_<seq>', method:'task.cancel', params:{session_id}}`，fire-and-forget（不注册 pending resolver，不等 result）。

**`web/src/composables/useSessions.ts`**

- 加 `cancelQuery()`：调 `client.cancel()`；主动 push 一条 `{role:'assistant', content:'[已取消]'}` 提示消息 + `busy.value = false`（双保险，不依赖 gateway final content，防 final 帧丢失/竞态）。导出 `cancelQuery`。

**`web/src/components/ChatPanel.vue`**

- `busy` 时"发送"按钮变"停止"按钮（`@click="cancelQuery"`，文案/图标切换）。复用现有 footer 布局，不加新按钮位。
- 从 `useSessions` 解构 `cancelQuery`。

### 5.3 Gateway

**`twinkle/gateway/message_handler.py`**

- 加 `_stream_tasks: dict[str, asyncio.Task]`（`session_id → task`）+ `_stream_rids: dict[str, str]`（`session_id → 原 chat.send rid`，用于 drain）。因单 session 同时只允许一个活跃 task，用 session_id 直接做 key（比 jiuwenswarm 的 rid→task+session 反查更简）。
- `handle_message` 加分流：`if msg.method == "task.cancel": await self.handle_cancel(msg); return`（不走 `_process_stream`）。WebChannel 不用改（所有 req 仍进 `handle_message`，由其内部分流）。
- `handle_message` 仅对 `method == "chat.send"` 存 task 到 `_stream_tasks[session_id]` + rid 到 `_stream_rids[session_id]`，task done 时 callback 清理两个 dict。
- 新增 `handle_cancel(msg)`（用 `msg.session_id` 定位）：
  1. `task = _stream_tasks.get(session_id)`；若在跑则 `task.cancel()`（触发 `_process_stream` 的 CancelledError）
  2. `rid = _stream_rids.get(session_id)`；若 rid 存在调 `agent_client.drain_request(rid)` 标记丢弃 AgentServer 残余帧
  3. 构造 `task.cancel` E2AEnvelope（带 `session_id`）→ `agent_client.send_cancel(envelope)` fire-and-forget 转发给 AgentServer
  4. 无活跃 task 时仍执行 ③（幂等转发，agentserver 侧 no-op）
- `_process_stream` 加 `except asyncio.CancelledError`：合成 `chat.final`（content 空，仅作结束信号）入 outbound Queue + 清理 `_stream_tasks`/`_stream_rids` + `raise`（对齐 jiuwenswarm `_publish_stream_cancelled_final`）。CancelledError 是 BaseException 本就不会被现有 `except Exception` 捕获，显式分支仅为合成 final。

**`twinkle/gateway/agent_client.py`**

- 加 `send_cancel(envelope)`：fire-and-forget 直接 `await self._ws.send(envelope.model_dump_json())`，不建 Queue、不等响应（cancel 不需要收 AgentServer 回的 ack）。
- 加 `drain_request(request_id)`：标记 rid 丢弃后续帧 + 清空对应 `_queues[rid]`（避免 AgentServer 残余帧积压），仿 jiuwenswarm `_drain_and_remove_queue`（简化掉 2s 延迟清理，直接清 + discard）。
- `_recv_loop` 路由时检查被标记 rid，静默丢弃。

### 5.4 AgentServer

**`twinkle/agentserver/server.py`**

- 在 method dispatch 链（L174 附近，approval 分支前后）加：
  ```python
  if envelope.method == "task.cancel":
      sid = envelope.session_id or ""
      cur = active.get(sid)
      cancelled = False
      if cur is not None and not cur.done():
          cur.cancel()
          cancelled = True
      await send(E2AResponse(
          request_id=envelope.request_id, sequence=0, is_final=True,
          status="succeeded", response_kind="e2a.result",
          body={"type": "task.cancel", "session_id": sid, "cancelled": cancelled},
      ))
      continue
  ```
- **不改 `run_task`**：CancelledError 绕过 `except Exception`，进 finally 减计数，task 自然结束。

### 5.5 Agent loop（零改动，复用已有）

- `agent.run` 的 LLM stream `await` / tool execute `await` 处抛 CancelledError
- → `finally` 块（`agent.py:464-482`）`completed_normally == False` → `_build_interrupt_snapshot()` 追加"请求被取消"到 history + `save_checkpoint()`（每步已存最近状态）
- 用户续发消息 → `load_checkpoint` → set_cache 灌回 → P1 resume 续跑（绕过 history）
- 这是"立即停"路线的核心红利：agent 层零改动。

### 5.6 错误处理 / 边界

- **幂等**：cancel 时 session 无活跃 task / task 已 done → gateway 与 agentserver 均静默 no-op，仍回 ack。
- **竞态**：gateway 合成 final 与 agentserver 残余帧 → AgentClient `drain_request` 标记 rid 丢弃后续。
- **tool 副作用**：`task.cancel` 打断 bash 子进程 / 文件写等 tool 执行，接受副作用（用户主动终止），不特殊 kill 子进程。对齐 jiuwenswarm（其 ability_manager 吞单 tool CancelledError 为 ToolMessage，但 Twinkle 走整 task cancel，不需要单 tool 吞）。
- **断连**：保持现状，浏览器断连不传播取消（决策 3）。
- **同 session 新请求**：保持现状"拒绝新请求"（`server.py:201-207`）；用户想换话题先 cancel 再发。

### 5.7 测试

- **gateway**：
  - `handle_cancel` 有活跃 task：合成 final + 转发 envelope + cancel task
  - `handle_cancel` 无活跃 task：no-op（幂等）
  - `_process_stream` CancelledError 路径：合成 final 入 Queue + 清理 `_stream_tasks`
  - AgentClient `drain_request`：标记 rid 后残余帧被丢弃
- **agentserver**：
  - `task.cancel` dispatch 有活跃 task：cancel + ack `cancelled:true`
  - `task.cancel` 无活跃 task：ack `cancelled:false`（no-op）
- **agent**：
  - task.cancel 后 finally 走 interrupt snapshot + checkpoint（复用/补 cancel 场景）
- **端到端**：
  - 发 query → 中途 cancel → 前端收 final + busy=false + 显示"已取消"
  - 续发消息能 resume 续跑

测试约定：`asyncio.run()` + `free_port`/`port_factory`（`tests/conftest.py`），不用 `pytest-asyncio`。

## 6. 不做的（YAGNI）

- `pause`/`resume`/`supplement` 多 intent（只 `cancel`）
- 浏览器断连自动取消
- todo 快照 / `recovery.json` 智能恢复
- tool 子进程特殊 kill
- 新增 `e2a.cancelled` response_kind
- `force_finish` 复用/改造（它是循环防护，与用户取消是两套机制）
- flag+Event 检查点（立即停不需要循环边界检查）

## 7. 改动范围

| 层 | 文件 | 改动 |
|---|---|---|
| 前端 | `web/src/services/webClient.ts` | 加 `cancel()` |
| 前端 | `web/src/composables/useSessions.ts` | 加 `cancelQuery()` |
| 前端 | `web/src/components/ChatPanel.vue` | busy 时发送按钮变停止 |
| Gateway | `twinkle/gateway/message_handler.py` | 加 `_stream_tasks` + `handle_cancel` + `_process_stream` CancelledError 分支 |
| Gateway | `twinkle/gateway/agent_client.py` | 加 `drain_request` + fire-and-forget 发送 |
| AgentServer | `twinkle/agentserver/server.py` | 加 `task.cancel` dispatch 分支 |
| Agent | `twinkle/agentserver/agent.py` | **零改动** |

净增约 120 行，无现有逻辑破坏。

## 8. 数据流（Twinkle 实现）

```
[前端]
 ChatPanel busy 时"停止"按钮 → cancelQuery() → client.cancel(sessionId)
   发 ws req: {type:req, id:"cancel_<ts>_<seq>", method:"task.cancel", params:{session_id}}
        │ (同一浏览器 ws)
        ▼
[Gateway]  MessageHandler.handle_message 识别 task.cancel → handle_cancel(msg.session_id):
  ① _stream_tasks[session_id].cancel()
     → _process_stream 捕获 CancelledError:
        合成 chat.final{content:""} 入 outbound Queue → ChannelManager → 前端
        清理 _stream_tasks/_stream_rids[session_id]
  ② _stream_rids[session_id] → AgentClient.drain_request(rid) 标记丢弃 AgentServer 残余帧
  ③ 构造 task.cancel E2AEnvelope → AgentClient.send_cancel() fire-and-forget 发给 AgentServer
        ▼
[AgentServer server.py]  dispatch task.cancel:
  active[session_id].cancel() → 回 e2a.result ack{cancelled:true}
  → agent.run 的 await 处抛 CancelledError
  → finally 块: interrupt snapshot("请求被取消"追加 history) + save_checkpoint(已有最近状态)
  → run_task finally 减计数, task 结束 (不发 error 帧)
        ▼
[前端]  收 chat.final(原 rid, 守卫通过) → onFinal → busy=false
  cancelQuery 已主动 push "[已取消]" 提示消息
  续发消息 → load_checkpoint → P1 resume 续跑
```
