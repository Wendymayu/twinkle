# 周期性任务（cron）设计 — Phase 6

> 日期：2026-07-29
> 参考：jiuwenswarm `enterprise_dev` 分支 `jiuwenclaw/gateway/cron/` + `jiuwenclaw/agentserver/tools/cron_tools.py` + `docs/en/ScheduledTasks.md`
> 范围：两阶段 wake→push 核心 + agent 工具 CRUD（不做前端 UI）

## 1. 背景与目标

当前 Twinkle 的 AgentServer 是**纯被动**进程：[server.py](../../../twinkle/agentserver/server.py) `main()` 里 `await asyncio.Future()` 阻塞等连接，只有收到 `E2AEnvelope` 才跑 agent。这导致 agent **只能由用户输入触发**，无法定时自主执行。

**目标**：让 agent 能被定时唤醒执行任务，结果推送到通道。忠实复刻 jiuwenswarm 的**两阶段 wake→push** 设计思想（gateway 是时钟），在 Twinkle 双进程架构上落地，去掉多通道企业特性。

**success criteria**（对齐 [roadmap.md](../../../roadmap.md) §Phase 6 验收）：注册一个 cron 任务 → 到点 wake 唤醒 agent 执行 → 结果 push 到 web 通道；支持单次任务（`delete_after_run`）与立即触发（`cron_run_now`）。

## 2. 现状与接入点

gateway 有运行中的 event loop，是天然的时钟位。出/入站链路现成可复用：

| cron 阶段 | 复用的现有接口 | 文件 |
|---|---|---|
| 时钟驱动 | gateway `main()` 里加一个 asyncio task | [__main__.py](../../../twinkle/gateway/__main__.py) |
| wake 跑 agent | `agent_client.send_request_stream(E2AEnvelope)` | [agent_client.py:77](../../../twinkle/gateway/agent_client.py#L77) |
| push 推送 | `message_handler.enqueue_outbound(Message)` → ChannelManager → `WebChannel.send` | [message_handler.py:102](../../../twinkle/gateway/message_handler.py#L102) |

**关键洞察**：wake 阶段**不能**直接复用 `MessageHandler.handle_message`（它会跑完 agent 立刻 `enqueue_outbound`，把两阶段拍扁成单阶段——这正是夜间版被判不合格的根因）。wake 必须用 `agent_client` 直接拿结果、存进 `CronRunState`，到 push 时刻才 `enqueue_outbound`。

## 3. 整体架构

**一句话方案**：在 gateway 进程挂 `CronSchedulerService`（时钟 + min-heap），到 `wake` 点用 `agent_client.send_request_stream` 跑 agent、结果存内存 `CronRunState`，到 `push` 点把结果经 `enqueue_outbound` → ChannelManager → `WebChannel.send` 推到浏览器。**AgentServer 零改动**（channel-agnostic，照常 `run_stream`）。

### 3.1 与 jiuwenswarm 的差异

| 维度 | jiuwenswarm | Twinkle |
|---|---|---|
| 时钟位 | gateway `CronSchedulerService` | 同 |
| wake 跑 agent | `agent_client.send_request`（unary） | `send_request_stream`（Twinkle 流式，消费到 `is_final`） |
| push 出口 | `publish_robot_messages`+`CHAT_FINAL`+IMOutboundPipeline | `enqueue_outbound(Message)`+`CHAT_FINAL`（无多通道/数字分身路由，out of scope） |
| 双前端共享 | 双文件 + WS `cron.response` 推送 | **单文件 + mtime 轮询** |
| CronRunState | 内存态（不落盘） | 内存态（同，见 §8 局限） |
| targets | feishu/xiaoyi/wecom/whatsapp/wechat 多通道 | 固定 `"web"`（字段保留待扩展） |
| 丢掉的件 | — | `cron_runtime.py`/`controller.py`/`cron_json_convert.py`/`cron_config.py`/多通道 target 元数据/`chat_type`/`mode` |

### 3.2 两阶段 wake→push 时序

```
       wake_dt = push_dt - wake_offset_seconds(默认60s)
       |<──── wake_offset ────>|
                              |
 ──────▼──────────────────────▼──────────────────────────► t
       wake                   push
       │                      │
       _on_wake:               _on_push:
       • 建/取 CronRunState    • 取 state
       • create_task(_run_agent)  • result_text 就绪? → 推真实结果, pushed_final=True ✅
         status=running          • 还没跑完? → 推占位"正在执行中…", placeholder_sent=True
         send_request_stream ─────► (agent 仍在跑)
         channel="__cron__"
         session_id="cron_{ts}_{jobid}"
       • 不阻塞 push 调度
                              │   ◄── _run_agent finally ──
                              │   state.result_text = agent 文本
                              │   if placeholder_sent and not pushed_final:
                              │       schedule push_update(now) ──┐
                              │                                   │
                              │   ┌───────────────────────────────▼
                              │   push_update: 推真实结果, pushed_final=True ✅
                              │
                              ├─ delete_after_run? → delete_job
                              └─ else croniter.get_next → 重新 schedule wake+push
                                  (CroniterBadDateError → expired=True/enabled=False)
```

**三种结果路径**：
1. agent 在 push 前跑完 → push 直接发真实结果（无占位无补发）。
2. push 时未跑完 → 发占位 → agent 跑完 finally 安排 `push_update` 补发真实结果。
3. agent 异常 → `result_text` 填错误文案 → push/push_update 照发失败说明。

## 4. 数据模型

落位 `twinkle/gateway/cron/models.py`，三个 dataclass。

### 4.1 `CronJob`（持久化到 `cron_jobs.json`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | uuid4 hex |
| `name` | str | 任务名 |
| `enabled` | bool | 默认 True |
| `cron_expr` | str | 5/7-field cron |
| `timezone` | str | IANA（如 `"Asia/Shanghai"`） |
| `wake_offset_seconds` | int | 默认 **60**（统一 jiuwenswarm 的 300/60 不一致） |
| `description` | str | 喂给 agent 的自然语言指令（→ `params.content`） |
| `expired` | bool | one-shot 耗尽标记 |
| `targets` | str | 默认 `"web"`（保留字段，Twinkle 单通道） |
| `delete_after_run` | bool | 单次任务跑完即删 |
| `created_at` / `updated_at` | float \| None | 时间戳 |

`to_dict()`/`from_dict()` 严格校验 `id/name/cron_expr/timezone` 必填。

> 去掉 jiuwenswarm 的多 bot `session_id`、`chat_type`、`mode`——Twinkle 用不到。

### 4.2 `CronRunState`（内存态，不落盘）

`run_id`、`job_id`、`wake_at_iso`、`push_at_iso`、`status`(pending|running|succeeded|failed)、`placeholder_sent`、`pushed_final`、`started_at`、`finished_at`、`result_text`、`error`。

### 4.3 `_Event`（frozen，min-heap 节点）

`at_ts`(float)、`seq`(单调递增 tie-break)、`kind`(wake|push|push_update)、`job_id`、`run_id`。

### 4.4 cron 表达式（`cron_expr.py`）

懒导入 `croniter`（cron 未启用时系统其余部分照常跑），支持 5-field（循环）与 7-field（one-shot 含 second+year），`ZoneInfo(timezone)` 校验时区，`CroniterBadDateError`（无下一次）→ `expired=True/enabled=False` 并持久化。`_cron_next_push_dt(expr, base_dt)` = `croniter(expr, base).get_next(datetime)`。

## 5. 组件清单与文件落位

### 5.1 `twinkle/gateway/cron/`（gateway 侧：调度 + 持久化）

| 文件 | 职责 | 关键点 |
|---|---|---|
| `models.py` | §4 三个 dataclass | — |
| `store.py` | `CronJobStore` | 单文件 `<workspace>/cron_jobs.json`；`asyncio.Lock` + 原子写(`.tmp`+`replace`)；CRUD(list/get/create/update/delete)；`update_job` 重新 enable/改 expr 时清 `expired` |
| `cron_expr.py` | 表达式校验/算下次 | §4.4；懒导入 `croniter` |
| `scheduler.py` | `CronSchedulerService` | min-heap + `_loop`(`asyncio.wait_for(reload_event, timeout)` + 5s mtime 轮询) + `_on_wake`/`_on_push`/`_on_push_update` + `reload()`(保留 `push_update` 事件) + `trigger_run_now` |

> 去掉的件：`controller.py`（无前端 UI，agent 工具直连 store）、`cron_runtime.py`（无 DeepAgents 桥）、`cron_json_convert.py`、`cron_config.py`（开关——Twinkle **always-on**，对齐 subagent 模式）。前端 API（`CronController` + web RPC）后续做时再加，复用同一 store + 同文件。

### 5.2 agent 工具（`twinkle/agentserver/tools/builtin/cron_tools.py`）

按 CLAUDE.md 约定：`@tool` 装饰 + 注册 `tool_manager()`。**5 个工具**（精简，YAGNI）：

`cron_list_jobs`（返回详情 + `next_run`，并入 get/preview）/ `cron_create_job` / `cron_update_job`（带 `enabled`，并入 toggle）/ `cron_delete_job` / `cron_run_now`（立即触发）。

> **不照搬 jiuwenswarm 的工具数**：核实发现 jiuwenswarm 的 `CronTools.get_tools()`（8 个）与 `CronController.get_tools()`（7 个）**都是死代码从未被调**；agent 真正用的是 `openjiuwen.harness.create_cron_tools()` 产出的 7 个（无 `run_now`），`run_now` 在 jiuwenswarm 是前端 web RPC 能力。Twinkle 无 openjiuwen harness、本次不做前端，agent 工具是唯一创建入口，故按实际需要精简为 5 个（`get`/`toggle`/`preview` 并入 `list`/`update`，`run_now` 留作唯一"立即触发"入口满足 roadmap 验收）。前端后续做时 web RPC 成主路径，agent 工具变备用。

工具内部 `new CronJobStore(<workspace>/cron_jobs.json)` 指向**同一文件**——agent 工具（AgentServer 进程）写完，gateway scheduler 靠 mtime 轮询热加载，无需新通信通道。

## 6. 装配点

- [gateway/__main__.py](../../../twinkle/gateway/__main__.py) `main()`：构造 `CronJobStore` + `CronSchedulerService(agent_client, message_handler, store)`；`channel_manager.start()` 后 `await cron_scheduler.start()`。
- [tools/__init__.py](../../../twinkle/agentserver/tools/__init__.py) `tool_manager()`：注册 8 个 cron 工具。
- `scheduler._run_agent`：构造 `E2AEnvelope(request_id=f"cron-{run_id}", channel="__cron__", session_id=f"cron_{ts}_{jobid}", method="chat.send", params={"content": job.description})`，调 `agent_client.send_request_stream`，消费到 `is_final` 提取 `result_text` 存 `CronRunState`。
- `scheduler._push_to_targets`：构造 `Message(id=f"cron-push-{run_id}", channel_id="web", event_type=CHAT_FINAL, content=result_text)`，`await message_handler.enqueue_outbound(msg)`。

**依赖方向**：`CronSchedulerService(agent_client, message_handler, store)` — 只持这三个；不碰 ChannelManager/WebChannel。与现有 gateway 单向依赖一致。

## 7. 双前端数据共享（单文件 + mtime 轮询）

agent 工具（AgentServer 进程）与 gateway scheduler（gateway 进程）共用 `<workspace>/cron_jobs.json` 一个文件。gateway 每 5s `stat` 文件 mtime，变了就 `reload()` 重读全量、重建调度堆。agent 工具改动自动热加载，无需重启、无需新建进程间通信通道。

mtime = 文件修改时间戳（`os.stat(path).st_mtime`），作"文件变没变"的廉价探针：未变则跳过重读，变了才触发 reload。agent 工具用原子写（`.tmp`+`replace`），CRUD 频率低，跨进程竞态可接受。

## 8. 错误处理与边界

| 场景 | 处理 |
|---|---|
| **cron run 遇 approval（`e2a.ask`）** | 该 run 立即 `status=failed`、`error="cron 任务触发了需审批的工具，已中止"`、`result_text` 填此文案。**不 auto-approve**（无人值守不放宽权限）；scheduler 收到 `e2a.ask` 不等待 respond，直接终结 run |
| agent 异常 / `status=failed` | `result_text` 填错误文案，`status=failed`，push/push_update 照发失败说明（路径③） |
| **`CroniterBadDateError`（无下一次）** | `expired=True`/`enabled=False` 并持久化。`_handle_event` 放行规则：`if not job.enabled and ev.kind != "push_update": return`（push_update 即使 disabled 也必须放行，否则单次任务结果发不出） |
| `delete_after_run` | push 后 `store.delete_job` + 移除内存索引 |
| **push 时 agent 没跑完** | 发占位"[cron] {name} 正在执行中，结果稍后补发"、`placeholder_sent=True`；agent 跑完 finally 若 `placeholder_sent and not pushed_final` → 安排 `push_update(now)` 补发真实结果（路径②） |
| 重复 wake | 该 run 的 task 还在跑 → `_on_wake` 直接返回，不重复启动 |
| **`CronRunState` 跨进程重启** | 内存态丢失 → `push_update` 事件虽保留但 `state is None` 跳过。**已知局限**，可选增强=落盘（YAGNI 暂不做） |
| mtime 轮询竞态 | agent 工具原子写、gateway reload 全量重建 |
| cron run 与用户请求并发 | `session_id="cron_{ts}_{jobid}"` 与用户 session 隔离；[server.py:134-144](../../../twinkle/agentserver/server.py#L134) 的 `active` dict 按 session_id 分开 task，天然支持 |
| demux | cron 用唯一 `request_id=f"cron-{run_id}"`，[agent_client.py:67](../../../twinkle/gateway/agent_client.py#L67) 按 rid 分队列 |

## 9. 关键决策记录

1. **wake 用流式 `send_request_stream`**（非 jiuwenswarm 的 unary）：Twinkle streaming-only，消费到 `is_final` 提取结果。
2. **`wake_offset_seconds` 默认 60**：jiuwenswarm `models.py` 字段写 300 是 bug，`from_dict`/`store`/docs 实际用 60；Twinkle 统一 60 可配。
3. **双前端单文件 + mtime 轮询**（非 jiuwenswarm 双文件 + WS 推送）：Twinkle 双进程单向 WS（gateway→agentserver），新建反向推送通道成本高；单文件 + mtime 热加载是合理简化，不违背两阶段设计思想。
4. **cron run 遇 approval 即 failed、不 auto-approve**：无人值守安全策略（Twinkle 对 jiuwenswarm 未明之点的决策）。
5. **CronRunState 内存不落盘**：同 jiuwenswarm；跨重启丢"已 wake 未补发"结果是边缘场景，YAGNI 暂不增强。
6. **always-on 无开关**：对齐 subagent 模式（[server.py](../../../twinkle/agentserver/server.py) 注释"Subagent is always on"），gateway 启动即跑 scheduler。
7. **targets 固定 `"web"`**：Twinkle 单通道；字段保留待未来扩展。
8. **去掉 `mode`/`chat_type`/多 bot `session_id`/`controller`/`cron_runtime`/`cron_json_convert`/`cron_config`**：Twinkle 无对应场景。
9. **agent 工具精简 5 个**（非照搬 jiuwenswarm 8/7 个）：核实 jiuwenswarm 的 `CronTools.get_tools()`(8)/`CronController.get_tools()`(7) 均为死代码从未被调；agent 真用的是 `openjiuwen.harness.create_cron_tools()` 产出的 7 个（无 `run_now`），`run_now` 是前端 web RPC 能力。Twinkle 无该 harness、本次无前端，agent 工具是唯一创建入口，精简为 list/create/update/delete/run_now（get/toggle/preview 并入）。

## 10. 测试策略

按 CLAUDE.md：`asyncio.run()` + `free_port`/`port_factory` fixtures（[tests/conftest.py](../../../tests/conftest.py)），无 `pytest-asyncio`。

| 层 | 文件 | 要点 |
|---|---|---|
| cron_expr 单元 | `tests/test_cron_expr.py` | 5/7-field 校验、IANA 时区、`CroniterBadDateError`→expired、`_cron_next_push_dt` |
| store 单元 | `tests/test_cron_store.py` | CRUD、原子写、`from_dict` 必填校验、重新 enable/改 expr 清 `expired`、单条 job 解析失败不崩 |
| scheduler 单元 | `tests/test_cron_scheduler.py` | min-heap 顺序、`wake_offset` 计算、三种结果路径、`placeholder_sent`/`pushed_final` 状态机、`delete_after_run`、`expired`、`trigger_run_now`、`reload()` 保留 `push_update`、mtime 变化触发 reload |
| approval 边界 | 同上 | fake agent 发 `e2a.ask` → run `failed` + 推送失败文案 |
| agent 工具 | `tests/test_cron_tools.py` | 5 工具 CRUD 端到端、写 `cron_jobs.json`、与 `CronJobStore` 往返一致 |
| 集成 | `tests/test_cron_integration.py` | 起 AgentServer(注入 scripted LLM)+gateway，注册短间隔 job（`wake_offset=0`+`cron_run_now`），验证 wake→push 端到端推到 web channel |

**测试可控性**：scheduler 构造注入 `now_fn`（默认 `time.time()`，测试注入固定时间戳）；fake `agent_client`（scripted `send_request_stream`）；fake `message_handler`（捕获 `enqueue_outbound`）；单元测试直接调 `_on_wake`/`_on_push`/`_on_push_update`，不跑 `_loop`；集成测试用 `trigger_run_now` + 短超时跑真实 `_loop`。

## 11. 已知局限与可选增强

- **CronRunState 不落盘**：跨进程重启丢"已 wake 未补发"的真实结果。可选增强：落盘 `cron_runs.json`（YAGNI 暂不做）。
- **targets 单通道**：仅 `web`；未来加多通道时扩展 `targets` 解析。
- **cron run 无总超时**：遇 approval 即 failed（无超时缓冲）；如需可加 run 总超时。
