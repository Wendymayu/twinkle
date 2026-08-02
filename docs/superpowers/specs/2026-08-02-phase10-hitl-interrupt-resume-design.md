# Phase 10 — HITL 中断/恢复（跨请求断点续跑）设计文档

## 一、概述

Phase 10 给 Twinkle 的审批系统加上**断点续跑**能力：用户审批中断后关闭浏览器，重新打开时能看到 pending 审批卡片并继续操作。核心改动是审批状态持久化 + 重连后主动查询 + 前端恢复审批卡片。

### 范围

- **做**：浏览器断连重连后恢复审批卡片（服务器不重启）
- **不做**：服务器重启后恢复（Phase 12）、WebSocket 自动重连（前端手动刷新即可）

### 对齐 jiuwenswarm

jiuwenswarm 的 `PermissionInterruptRail` 把中断状态存到 session KV（`__react_agent_interruption__` + `jiuwenclaw_pending_permission_contexts`），下次 invoke 时检测并恢复。Twinkle 选更轻量的方案：磁盘文件 + 主动查询 RPC，因为 Twinkle 的 `run_stream` 是单请求 async generator，`await future` 挂起时协程仍在内存中，不需要 jiuwenswarm 的 invoke-重入恢复模式。

---

## 二、设计决策

### 2.1 持久化位置：`<session_dir>/.approval_pending.json`

与 `history.json`、`metadata.json` 同目录，不污染 metadata，不需要新模块级 store。路径通过 `SESSIONS_DIR` config 常量获取（与 `SessionStore` 同源）。

### 2.2 重连恢复机制：前端主动查询 `approval.check_pending`

不修改 Gateway 的连接握手（避免增加复杂度）。前端 WebSocket 连接建立后，发 `approval.check_pending` RPC 查询当前 session 的 pending approval。如果返回非空，push 审批卡片到 messages。

### 2.3 持久化与内存 Future 的关系

两者是独立的：
- **内存 Future**：`await future` 挂起 agent loop 协程——这是实际的控制流机制
- **磁盘文件**：审批元数据——用于重连后让前端知道有 pending approval

生命周期：
- ASK 时：`register(Future)` + `save_pending()` 同时执行
- resolve 时：`clear_pending()` 清除磁盘文件
- `run_stream` 结束时：`clear_all_pending()` 安全网清除

---

## 三、数据模型

```json
// <session_dir>/.approval_pending.json
[{
  "approval_id": "uuid-string",
  "tool": "command_exec",
  "args": {"command": "rm -rf /"},
  "tool_call_id": "tc_123",
  "reason": "requires approval",
  "request_id": "req_abc",
  "session_id": "sess_xyz",
  "created_at": 1722592800.0
}]
```

数组格式，正常最多 1 条（agent loop 挂在单个 `await future` 上）。

---

## 四、改动清单

### 4.1 `twinkle/agentserver/permissions/approval_registry.py`

- 新增 `ApprovalPendingRecord` dataclass
- 新增 `_pending_path(session_id)` → `<SESSIONS_DIR>/<session_id>/.approval_pending.json`
- 新增 `save_pending(session_id, record)` — 原子写（`.tmp` + `os.replace`）
- 新增 `clear_pending(session_id, approval_id)` — 删除指定记录，空则删文件
- 新增 `get_pending(session_id)` — 读取 pending 列表
- 新增 `clear_all_pending(session_id)` — 删除整个文件
- 修改 `handle_respond()` — resolve 后调 `clear_pending()`
- `cancel_all()` — 清除内存 Future（持久化文件由 `run_stream` finally 清理）

### 4.2 `twinkle/agentserver/agent_loop.py`

- `except HookInterrupt as hi` 块中，`register()` 后加 `save_pending()`
- `run_stream` 的 `finally` 块加 `clear_all_pending()` 安全网

### 4.3 `twinkle/agentserver/server.py`

- `ws_handler` 消息循环加 `approval.check_pending` 路由（与 `approval.respond` 同级）

### 4.4 `web/src/services/webClient.ts`

- 新增 `checkPendingApprovals(sessionId)` — 用 `request()` 发 `approval.check_pending`

### 4.5 `web/src/composables/useSessions.ts`

- 新增 `checkAndRestorePendingApproval()` — 查询 pending 并 push 审批卡片（去重）
- `init()` 中 session 加载后调用

---

## 五、边界情况

| 场景 | 处理 |
|---|---|
| 重复审批卡片 | `checkAndRestorePendingApproval` 检查已有同 `approvalId` 的卡片则跳过 |
| 审批已被另一个客户端 resolve | `check_pending` 返回空列表，不推卡片 |
| 服务器重启后残留 `.approval_pending.json` | `run_stream` finally `clear_all_pending`；`resolve()` 返回 False 时前端优雅处理 |
| `approval.respond` 发给已过期的 approval_id | `resolve()` 返回 False，ack 为 failed |

---

## 六、验收

- 用户审批中断 → 关闭浏览器 → 重新打开 → 看到 pending 审批卡片 → 点击允许 → agent 从断点继续执行
