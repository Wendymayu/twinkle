# 崩溃恢复设计

## 一句话

Twinkle 的崩溃恢复是**请求驱动**的——不是后台守护进程主动重启，而是「下一次同 session 的请求到来时，`_run_react_loop` 开头从 `checkpoint.json` 灌回 cache 快照 + `_fill_missing_tool_results` 修孤儿 tool_call，从中断处续跑 ReAct」。`checkpoint.json` 存的是 **cache 全量快照**（OpenAI 原生 messages，非压缩窗口），每步 `before_model_call` 之前 + 正常完成时各落盘一次，原子写、损坏 fail-soft 退化成从 `history.json` 冷加载。

---

## 为什么需要崩溃恢复

ReAct 循环是长流程：一次 `run()` 可能跨数十步模型调用 + 工具执行，期间进程随时可能死。三个现实约束决定了恢复机制长什么样：

1. **进程会死，且死法不可控** —— `kill -9` / OOM / 断电 / Python 解释器崩溃都不会跑 `finally`。靠 `finally` 落盘的方案对硬杀无效，必须每步主动落盘。
2. **OpenAI tool 协议要求配对** —— 每个 `assistant.tool_calls[i]` 必须有对应的 `role=tool, tool_call_id=...` 消息。崩溃若发生在「assistant 发了 tool_calls、工具未执行」之间，会留下**孤儿 tool_call**，下次喂模型直接报错「tool call X has no result」。
3. **ASK 挂起是内存态** —— 权限审批把 run_stream 挂在进程内 `asyncio.Future` 上（`APPROVAL_REGISTRY`）。进程不崩时这是优雅的内存态恢复；进程崩了，Future 随之消失，pending approval 信息全丢，孤儿 tool_call 必须能退化处理。

所以三条底线：**落盘到每步**（硬杀也能续）、**孤儿必补**（协议不破）、**恢复是请求驱动而非自启动**（无后台 watchdog，靠下次请求触发）。这与 [`failure-handling-design.md`](./failure-handling-design.md) 讲的「失败处理」是两条线——失败处理管单步内异常的软/硬分流，崩溃恢复管跨进程的状态续接。

---

## 持久化三层：history / cache / checkpoint

每个 session 一个目录（见 [store.py:1-18](../twinkle/agentserver/sessions/store.py#L1-L18)），布局：

```
<sessions_dir>/<session_id>/
    metadata.json    # 会话元数据(title/created_at/message_count/...)
    history.json     # JSONL,append-only 全量消息 record(带 metadata 字段)
    checkpoint.json  # 运行态快照(崩溃恢复用): {messages, request_id, updated_at}
```

三层各司其职，**不冗余**：

| 层 | 形态 | 职责 | 写时机 |
|---|---|---|---|
| `history.json` | JSONL record（带 `id`/`request_id`/`timestamp`/`reasoning`/`event_type` 等） | append-only 全量真源；冷加载；前端 `get_history` 展示气泡；`message_count` 推导 | 每次 `append` |
| `_cache`（内存 `dict`） | OpenAI 原生 message 列表 | 热读喂模型；`get_messages` 命中即返 | 每次 `append` |
| `checkpoint.json` | OpenAI 原生 messages 数组 | **跨进程恢复**：灌回 cache 绕过 history 重建 | 每步 + 正常完成 |

关键区别：`history` 存的是 **record**（带元数据，`_record_to_openai` 转换后才喂模型），`cache`/`checkpoint` 存的是 **OpenAI 原生 message**（可直接喂模型，免去转换 + 免去读全量 JSONL）。`append`（[store.py:262-299](../twinkle/agentserver/sessions/store.py#L262-L299)）同时写 cache + history + metadata，三者保持一致。

**一致性边界**：正常运行时 `cache` 持久驻留内存，不需要 checkpoint；只有「新进程 / cache 被 evict / 跨请求恢复」时 cache 是空的，才从磁盘恢复。此时 checkpoint 提供了「OpenAI 格式现成快照」，比从 history 冷加载 + 逐条转换快。

> **注释措辞提醒**：`save_checkpoint` / `set_cache` / agent.py 的注释把 checkpoint 描述为「压缩窗口快照」，这是 jiuwenswarm 术语遗留（jiuwenswarm 的 `save_state` 存的是滑动窗口 `_message_buffer`）。Twinkle 实际存的是 **cache 全量**——`test_cross_turn_remembers_context`（[test_agent_loop.py:97](../tests/test_agent_loop.py#L97)）证实：若真存压缩窗口，跨 turn 会漏早期上下文，测试就红。下文「设计决策」详述。

---

## 保存时机：三个落盘点

`run()` → `_run_react_loop` 里有三个时机写 checkpoint / 相关状态（[agent.py:445-482](../twinkle/agentserver/agent.py#L445-L482)、[agent.py:526-535](../twinkle/agentserver/agent.py#L526-L535)）：

### 1. 每步 save —— 硬杀恢复的核心

```python
for _step in itertools.count():
    msgs = self._session_store.get_messages(session_id)
    ...
    # 每步 save 当前 messages 窗口(含到上步的 append)到 checkpoint.json
    # (崩溃恢复 checkpoint;硬杀 finally 跑不到,靠这步续)
    self._session_store.save_checkpoint(session_id, msgs, request_id)
    # -- BEFORE_MODEL_CALL -- #
```

位置在「取到 cache → `before_model_call` 之前」，即**本步模型调用之前**。存的是「到上一步 append 为止的完整 cache 全量」。这是硬杀恢复的命脉——`kill -9` 时 `finally` 跑不到，但上一轮循环已经落盘了。

### 2. 正常完成 save —— 多轮续接

```python
finally:
    if completed_normally:
        # 正常完成:save 最终 messages(含最后 append)供下次多轮续接
        self._session_store.save_checkpoint(
            session_id, self._session_store.get_messages(session_id), request_id)
```

`run()` 的 `finally` 块，循环正常走完时存最终 messages（含最后一步的 assistant 回复 + tool_result）。下次同 session 请求 `load_checkpoint` 拿到的是上次完整对话——这就是**多轮续接**（非崩溃场景）。

### 3. 中断 snapshot —— 被取消但没崩

```python
if not completed_normally:
    try:
        snapshot = await self._build_interrupt_snapshot(ctx, session_id)
        await self._session_store.append(
            session_id,
            {"role": "assistant", "content": snapshot},
            request_id=request_id)
    except asyncio.CancelledError: ...
```

非正常完成（模型异常 / 被取消）但进程没崩时，不写 checkpoint，而是向 **history append 一条 interrupt snapshot**（[agent.py:817-847](../twinkle/agentserver/agent.py#L817-L847)）——说明中断原因（异常类型+消息 / 「请求被取消」）+ 中断前正在执行的工具 + Todo 进度。这条给前端展示「任务中断」状态用，不参与 resume 灌回（灌回靠 checkpoint）。

> 注意：**硬杀（`finally` 跑不到）时不写 snapshot**——它依赖 `finally`。所以硬杀后的 history 里没有中断说明，恢复纯靠 checkpoint + `_fill_missing`。

---

## resume 流程：请求驱动续跑

恢复发生在每次 `_run_react_loop` 开头（[agent.py:502-508](../twinkle/agentserver/agent.py#L502-L508)），**每次请求都试**，不区分是崩溃后首请求还是正常多轮：

```python
# resume:若存在 checkpoint(上次未完成 run 的快照),灌回 cache 使后续
# get_messages 命中快照而非重读全量 history
checkpoint = self._session_store.load_checkpoint(session_id)
if checkpoint and checkpoint.get("messages"):
    self._session_store.set_cache(session_id, checkpoint["messages"])
await self._fill_missing_tool_results(session_id, request_id)

# 然后才 append 本轮 user 消息,进入 ReAct 循环
await self._session_store.append(session_id, {"role": "user", "content": request.query}, ...)
```

三步：

1. **`load_checkpoint`**（[store.py:230-239](../twinkle/agentserver/sessions/store.py#L230-L239)）—— 读 `checkpoint.json`，**文件缺失/损坏返回 `None`（fail-soft）**，不抛。
2. **`set_cache`**（[store.py:224-228](../twinkle/agentserver/sessions/store.py#L224-L228)）—— 把 checkpoint 的 messages 直接写进内存 cache，**不写 history.json**。后续 `get_messages` 命中 cache（返回 checkpoint 视图）而非重读全量 history。对齐 jiuwenswarm `load_state` 灌回 `_message_buffer`，不存 offload。
3. **`_fill_missing_tool_results`** —— 修孤儿（见下节）。

然后 append 本轮 user、进 ReAct 循环。崩溃前最后一步的产出会重新调模型生成（见「已知缺口」）。

**为什么灌 checkpoint 而非从 history 重建**？两者都能恢复，但 checkpoint 是 OpenAI 原生格式现成快照，免去 (a) 读全量 JSONL + (b) 逐条 `_record_to_openai` 转换。更关键的是**忠于运行时视图**：cache 在运行时是 agent 真正喂模型的状态，checkpoint 是它的精确快照；history 是 append-only record 流，形态不同。`test_resume_restores_from_checkpoint_snapshot`（[test_checkpoint_resume.py:50](../tests/test_checkpoint_resume.py#L50)）专门验证「resume 灌回 checkpoint 视图而非 history 全量」。

---

## 孤儿 tool_result 填充

`_fill_missing_tool_results`（[agent.py:851-885](../twinkle/agentserver/agent.py#L851-L885)）解决 OpenAI 协议约束：崩溃若发生在 assistant 发了 tool_calls、工具未执行/未 append tool_result 之间，会留下无配对 tool_result 的孤儿，下次喂模型必报错。

```python
async def _fill_missing_tool_results(self, session_id, request_id):
    msgs = self._session_store.get_messages(session_id)
    if not msgs:
        return
    last_assistant = None
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            last_assistant = m; break
    if last_assistant is None:
        return
    pending = APPROVAL_REGISTRY.get_pending(session_id)
    pending_map = {p["tool_call_id"]: p for p in pending if p.get("tool_call_id")}
    for tc in last_assistant["tool_calls"]:
        tc_id = tc.get("id")
        if tc_id and not any(m.get("role")=="tool" and m.get("tool_call_id")==tc_id
                            for m in msgs):
            # 合成一条 [interrupted: ... result unknown.] 的 tool_result append
            ...
```

逻辑：

1. 反向找最后一条带 `tool_calls` 的 assistant 消息。
2. 对每个 `tool_call`，检查是否存在配对的 `role=tool, tool_call_id` 消息。
3. 缺配对的，合成一条 `tool_result`（`[interrupted: {tool_name} was interrupted, result unknown. Args: {preview}.]`）append 进 session。
4. **若该 tool_call 在 ASK pending**（`APPROVAL_REGISTRY.get_pending`），附加 `Approval was pending (reason: ...)`——但这是进程**没崩**时的内存态（见下节交集）。

合成消息刻意说「result unknown」而非伪造结果——把「不知道」如实告诉模型，让模型自行决定重试还是换路。覆盖测试：[test_orphan_cleanup.py](../tests/test_orphan_cleanup.py)、[test_agent_loop.py:440](../tests/test_agent_loop.py#L440)。

---

## 崩溃场景与恢复矩阵

| 崩溃点 | checkpoint 状态 | history 状态 | 恢复行为 | 丢失 |
|---|---|---|---|---|
| 模型调用中（step N） | step N 开头存（到 step N-1 的 append） | ≤ step N-1 的 append | load checkpoint + 续调模型 step N | 无（step N 未产出） |
| 工具执行中（assistant 已发 tool_calls） | 同上（含孤儿 tool_calls） | 多了 assistant 的 tool_calls append | load checkpoint + `_fill_missing` 补合成 tool_result | 工具的真实结果（重做） |
| 正常完成后、下次请求前 | 最终 messages 快照 | = checkpoint | 下次请求 load = 多轮续接 | 无 |
| 硬杀（`finally` 跑不到） | 最后一步快照 | 可能多最后几条 append | load checkpoint + 续跑 | 最后一步产出（靠下步 save 落盘的，见缺口） |
| checkpoint.json 损坏 | ✗ | 全量 | `load_checkpoint` 返 None → fail-soft → 从 history 冷加载重建 | 无（退化，不崩） |
| 无 checkpoint（新 session） | ✗ | 空/全量 | 从 history 冷加载（[store.py:212-220](../twinkle/agentserver/sessions/store.py#L212-L220)） | 无 |

注意第二行：工具执行中崩溃时，history 多了 assistant 的 tool_calls（append 在工具执行前），但 checkpoint 没有（checkpoint 是本步开头存的，append 还没发生）。resume 灌回 checkpoint 视图 → cache 里**没有**那条 assistant tool_calls → `_fill_missing` 找不到孤儿（因为 checkpoint 里压根没那条 assistant 消息）→ 直接续调模型。模型会重新生成 tool_calls（可能不同）——这是「重做」语义。若崩溃发生在 assistant tool_calls 已 append 但 checkpoint 未更新，那条会丢。

---

## 进程级 in-flight 状态

[server.py](../twinkle/agentserver/server.py) 的 `ws_handler` 用内存 `active: dict[session_id, asyncio.Task]` 管理在途请求（[server.py:200-210](../twinkle/agentserver/server.py#L200-L210)）：

- **同 session 去重**：已有未完成 task 时，新请求直接回 `e2a.error`（"a request is already in progress"），防并发写同一 SessionStore。
- **`finally` 清理**（[server.py:211-220](../twinkle/agentserver/server.py#L211-L220)）：断连时 cancel 所有 active task + `APPROVAL_REGISTRY.cancel_all()`，防 Future 泄漏。
- **进程崩后**：`active` dict 是内存态，随进程消失。新进程无在途记录——恢复**不靠**「重启后自动续」，而是靠「用户下次发请求到同 session」触发 `load_checkpoint`。这就是「请求驱动」的真义：没有 watchdog / 没有重启钩子，恢复是被动触发的。

`run_task`（[server.py:144-164](../twinkle/agentserver/server.py#L144-L164)）是每个请求的 task 包装：`_inflight_count` inc/dec 供 Dreaming busy-backoff 判据；`except Exception` 兜底发 `e2a.error` 帧给用户（模型硬失败的最终出口）。

---

## ASK 挂起 vs 崩溃恢复：两条恢复线

这俩容易混，必须分清（architecture.md §4.9 把 ASK 流放在 permissions 章，这里点出它与崩溃恢复的边界）：

| | ASK 挂起恢复 | 崩溃恢复 |
|---|---|---|
| 触发 | 权限 ASK → `HookInterrupt` → `await asyncio.Future` 挂起 | 进程死 → 下次请求 |
| 状态载体 | 进程内 `APPROVAL_REGISTRY`（`approval_id → Future`） | 磁盘 `checkpoint.json` |
| 进程存活 | 必须（Future 在内存） | 不要求（重启后） |
| 恢复方 | `approval.respond` resolve Future → 原地续 run_stream | `load_checkpoint` 灌回 cache → 续跑 |
| 持久化 | ✗（纯内存） | ✓（每步落盘） |

**交集场景**：ASK 挂起时进程崩溃。`APPROVAL_REGISTRY` 随进程消失，pending approval 信息全丢。`_fill_missing_tool_results` 在新进程里 `get_pending` 返空，该 tool_call 退化成普通 `[interrupted: ... result unknown.]`（不带 `Approval was pending` 说明）。这是已知限制：审批挂起状态**不跨进程持久化**。若要支持「崩后恢复审批」，需把 pending approval 落盘——当前不做，接受退化。

---

## 对照 jiuwenswarm

聚焦崩溃恢复这条线（参考调研 [docs/superpowers/research/2026-08-27-jiuwenswarm-crash-recovery.md](../superpowers/research/2026-08-27-jiuwenswarm-crash-recovery.md)）：

| | jiuwenswarm | Twinkle |
|---|---|---|
| 恢复载体 | `checkpointer`（必需组件） | `checkpoint.json`（每 session 一文件） |
| 存什么 | `save_state` 的 messages = 滑动窗口 `_message_buffer` + offload | cache 全量 messages，**不 offload** |
| 弱恢复代价 | buffer resize 丢老消息 | 无 resize、存全量，不丢（代价：文件可能大） |
| 触发恢复 | 框架自动 `load_state` 灌回 | 请求驱动（下次 `run()` 开头 load） |
| 命名 | `load_state` / `save_state` | `load_checkpoint` / `save_checkpoint`（刻意改名，避免「state」歧义 + 更准） |
| iteration/step | 存（有界循环配额） | **不存**（Twinkle 无界 `itertools.count()`，无配额，旧记「iteration 必需」作废） |

jiuwenswarm 是「弱恢复 + 必需 checkpointer + 滑动窗口丢老消息」；Twinkle 是「全量快照 + 请求驱动 + 不丢」。Twinkle 的 `cache 转 stateful buffer`（`set_cache` 让 cache 从纯缓存变成可被 checkpoint 灌回的有状态缓冲）是对齐 jiuwenswarm `_message_buffer` 灌回机制的关键一笔。

---

## 设计决策回顾

### 为什么存 cache 全量而非压缩窗口

曾经试过存「压缩窗口快照」（对齐 jiuwenswarm 的滑动窗口语义）。**被 `test_cross_turn_remembers_context` 暴露**：若 checkpoint 只存压缩窗口（丢早期摘要后的视图），turn 1 正常完成 save 的 checkpoint 已是瘦身版，turn 2 load 后看不到 turn 1 的完整早期上下文 → 跨 turn 失忆。**改成存 cache 全量**后通过：cache 在运行时就是所有 append 的完整消息（压缩只发生在 `ctx.inputs.messages`——模型调用的临时输入副本，不持久化到 cache/history）。所以 checkpoint 存全量 = 运行时 cache 的精确镜像，跨 turn 不丢。

代价：长对话的 checkpoint.json 可能较大（全量 messages）。可接受——单用户单进程，磁盘不是瓶颈。

### 为什么不存 iteration / step

Twinkle 循环无界（`itertools.count()`，`agent.max_steps` 已 DEPRECATED 见 [failure-handling-design.md](./failure-handling-design.md) §循环防护），没有「第几步」的配额概念。存 step 既无意义（没有 max_steps 可对照）也无必要——恢复时从 checkpoint 的 messages 末尾续调模型即可，不需要知道「原本跑到第几步」。jiuwenswarm 存 step 是因为它有界循环 + 配额，Twinkle 不存在这个前提。

### 为什么每步 save 在模型调用之前

把 save 放在 `before_model_call` 之前（取到 cache 之后），存的是「即将喂本步模型的输入」。这样硬杀发生在模型调用中、工具执行中时，checkpoint 是「本步开始前的稳定状态」——续跑时重调本步模型，语义干净（不会把半截模型输出当既成事实）。

**已知 gap**：本步的产出（assistant 回复 + tool_result 的 append）要等到**下一步循环开头**才 save。若崩溃发生在「本步 append 之后、下一步 save 之前」，本步产出会丢——恢复时从本步开头重做。未补「append 后立即 save」（每步两次 save 的开销），接受重做。记忆 [[p1-checkpoint-resume-landed]] 记录了这个取舍。

### 为什么原子写 + fail-soft

`save_checkpoint` 写 `.tmp` 再 `os.replace`（[store.py:254-257](../twinkle/agentserver/sessions/store.py#L254-L257)）——`os.replace` 是原子操作，避免半写损坏（崩溃发生在写一半时留下截断 JSON）。`load_checkpoint` 对损坏文件返 `None`（[store.py:235-239](../twinkle/agentserver/sessions/store.py#L235-L239)），退化成从 history 冷加载——**恢复永远不因 checkpoint 损坏而失败**，只是慢一点（[test_corrupt_checkpoint_falls_back](../tests/test_checkpoint_resume.py#L115) 验证）。

### 为什么灌 checkpoint 而非 history 重建

见上「resume 流程」。一句话：OpenAI 原生格式现成、免转换、忠于运行时 cache 视图。

---

## 已知缺口

1. **本步产出未即时落盘** —— 每步 save 在模型调用前，本步产出靠下一步 save。崩溃在 append 后、下步 save 前会丢最后一步，接受重做。未补「append 后再 save」以省开销。
2. **ASK pending 不跨进程持久化** —— `APPROVAL_REGISTRY` 纯内存，进程崩即丢，孤儿 tool_call 退化成普通 interrupted（不带审批说明）。要支持崩后审批恢复需落盘 pending approval，当前不做。
3. **无自动重启触发** —— 恢复是请求驱动，没有 watchdog/重启钩子主动续。进程崩后必须等用户下次发请求到同 session 才触发 resume。若用户不发，崩前的 run 永远不续（但 checkpoint 已落盘，随时可续）。
4. **checkpoint 与 history 可能短暂不一致** —— 硬杀时 history 可能比 checkpoint 多最后几条 append（append 先于下步 save）。resume 灌回 checkpoint 视图会丢那几条（见崩溃矩阵第二行）。这是「忠于 checkpoint」设计的已知代价。

---

## 测试覆盖

| 测试 | 验证 |
|---|---|
| [test_resume_restores_from_checkpoint_snapshot](../tests/test_checkpoint_resume.py#L50) | resume 灌回 checkpoint 视图（from_checkpoint）而非 history 全量（from_history）+ `_fill_missing` 修孤儿 |
| [test_save_checkpoint_each_step](../tests/test_checkpoint_resume.py#L83) | 每步 save；最后快照含上一步 tool_result |
| [test_fallback_no_checkpoint_reads_history](../tests/test_checkpoint_resume.py#L102) | 无 checkpoint → 从 history 冷加载（不回归） |
| [test_corrupt_checkpoint_falls_back](../tests/test_checkpoint_resume.py#L115) | 损坏 checkpoint → fail-soft 返 None → fallback history |
| [test_cross_turn_remembers_context](../tests/test_agent_loop.py#L97) | 跨 turn 续接不丢上下文（隐含验证 checkpoint 存全量而非压缩窗口） |
| [test_interrupt_snapshot_on_model_exception](../tests/test_agent_loop.py#L399) | 模型异常时写 interrupt snapshot |
| [test_no_interrupt_snapshot_on_normal_completion](../tests/test_agent_loop.py#L421) | 正常完成不写 snapshot |
| [test_sanitize_orphan_tool_calls_includes_tool_name_and_args](../tests/test_agent_loop.py#L440) | 孤儿 tool_result 含工具名 + 参数预览 |
| [test_orphan_assistant_tool_calls_sanitized](../tests/test_orphan_cleanup.py#L23) | 孤儿 assistant tool_calls 被合成 tool_result 修复 |
| [test_mid_batch_orphan_sanitized](../tests/test_orphan_cleanup.py#L47) | 批量工具调用中的孤儿也被修 |
