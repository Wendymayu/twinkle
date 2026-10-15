# MCP 运行时动态接入 + 动态权限设计

> 日期：2026-09-28
> 状态：设计已与用户确认，待写实现计划
> 关联记忆：[[phase15-mcp-landed]] [[progressive-tool-visibility-landed]] [[mcp-tool-refresh-landed]]
> 关联文档：[tool-system-design.md](../../design/tool-system-design.md) §工具渐进式加载、§权限

## 1. 背景与目标

Twinkle 当前 MCP 是**静态配置模型**：`mcp.servers` 写在 `config.yaml`（[config.yaml:158](../../../twinkle/resources/config.yaml#L158)），启动时 `McpManager.__init__` 遍历 `config.servers` 连接（[manager.py:50](../../../twinkle/agentserver/mcp/manager.py#L50)），`permissions.tools` 同样启动时读死（[schema.py:183](../../../twinkle/config/schema.py#L183)），运行时不可改（只有 `allow_always` override 文件 mtime 热重载，[policy.py:47-60](../../../twinkle/agentserver/permissions/policy.py#L47-L60)）。加一个 MCP server 要改 config + 重启。

但真实部署形态是 **agent 常驻运行，运行时通过扩展机制引入 MCP server**——这时 config.yaml 早加载完，改不了也不该重启。且接入前不知道 server 有哪些工具、哪些危险哪些只读，没法预配 `permissions.tools` 逐项。

**不对称**：Skill 已是运行时动态扩展模型（`skills.install`/`uninstall` RPC，[skills/rpc.py:20](../../../twinkle/agentserver/skills/rpc.py#L20)，后台任务、不碰 config），MCP 还停在静态。

**目标**：agent 运行时动态接入/移除 MCP server + 运行时配审批档位 + 接入即按 MCP annotations 自动分级危险/只读，全程不碰 `config.yaml`、不重启。

## 2. 核心决策（与用户确认）

1. **MCP 学 Skill 的运行时扩展模式**：新增 `mcp.add_server` / `mcp.remove_server` / `mcp.list_servers` / `mcp.set_tool_permission` RPC，经 ws_handler 路由，建连/断连走后台任务（对齐 `skills.install`），不碰 config.yaml。
2. **权限配置真源从静态 config 转运行时 store**：`PermissionPolicy` 加 `set_tier(tool, tier)` / `update_tools(mapping)` 运行时改 `_tools` dict 并热生效。`config.yaml` 退居"启动基线"（内置工具 + 静态 MCP server），运行时引入的 MCP server 权限全走运行时 store。
3. **server 级通配匹配**：`policy.check` 改"先精确名、再 `{server}.*` 前缀通配、再 `global_default`"三层。接入前不知道工具名也能配（`"alipay.*": require-approval`）。
4. **MCP annotations 自动分级**：`list_tools` 保留 `readOnlyHint`/`destructiveHint`（当前 [client.py:55-57](../../../twinkle/agentserver/mcp/client.py#L55-L57) 丢弃了），接入时自动定档——`readOnlyHint=true`→`allow`、`destructiveHint=true` 或 `readOnlyHint=false`→`require-approval`、未标注→走 server 通配兜底。落地"少数危险审、多数只读免审"不用手配。
5. **`allow_always` 保留定位为低风险免审沉淀**：对 `require-approval`（可免审）档和只读工具生效，override 文件热重载机制不变（[policy.py:126](../../../twinkle/agentserver/permissions/policy.py#L126)）。
6. **不做"必须每次审"的 always 档**：危险工具走 `require-approval`，用户可点 `allow_always` 免审（整工具级，靠用户自觉）。不新增 `require-approval-always` 档（用户已决定不要这个强制）。

## 3. 方案选择

### 3.1 McpManager ↔ ToolManager 解耦（沿用 refresh spec 方案 B）

`McpManager.add_server` 不持 `tm` 引用，返回工具列表（含 annotations），由 agent 协调 `register` + 自动分级 + `recompute_eager`。对齐 [2026-08-25-mcp-tool-refresh-design.md](./2026-08-25-mcp-tool-refresh-design.md) §3 方案 B：McpManager 职责纯化"MCP 协议交互 + 自己的 `_tools` 视图"，ToolManager 保持通用容器不渗 MCP 语义，agent 作协调者。

### 3.2 annotations 自动分级放哪

| 方案 | 评价 |
|---|---|
| A. `policy.check` 时读工具 annotations 动态定档 | ❌ check 每次要拿工具对象，annotations 不该是 PermissionPolicy 职责 |
| **B. 接入时一次性 `set_tier` 存进 `_tools`** | ✅ 接入时定档一次，check 正常查 `_tools`；用户可 `set_tool_permission` RPC 覆盖 |

### 3.3 协调点放哪

| 方案 | 评价 |
|---|---|
| A. RPC handler 协调（拿 tm + policy） | RPC 层渗入 agent 内部对象，耦合 |
| **B. agent 加 `add_mcp_server`/`remove_mcp_server` 方法封装协调** | ✅ 协调集中在 agent（它本就装配三者），RPC handler 只调 agent 方法 |
| C. McpManager 持 tm + policy | ❌ 违背 refresh spec 解耦 |

选 B：agent 加协调方法，RPC handler 调它。agent 需在 `create_agent` 时存 `permission_policy` 引用（当前未直接持，从 `PermissionHook` 取或构造时传入）。

## 4. 组件改动

### 4.1 McpManager（`mcp/manager.py`）
- 新增 `async def add_server(self, server_config: McpServerConfig) -> list[McpToolCard]`：建连（复用现有 `_connect_one` 逻辑）+ `list_tools`（带 annotations）+ 更新自己 `_tools`/`_clients`/`_server_resources` 视图 + 返回工具列表。不碰 tm。
- 新增 `async def remove_server(self, name: str) -> list[str]`：断连 + 清自己视图 + 返回要 unregister 的工具名列表（`{name}.*`）。
- 新增 `def list_servers(self) -> list[dict]`：返回当前已接入 server 摘要（name/transport/tool_count），供 RPC 查询。
- `startup()` / `register_into(tm)` / `refresh_all()` 不变（静态启动路径向后兼容）。

### 4.2 McpToolCard + list_tools（`mcp/tool.py` + `mcp/client.py`）
- `McpToolCard` 加 `annotations: dict | None = None`（保留 MCP 协议的 `readOnlyHint`/`destructiveHint`/`idempotentHint`）。
- `client.py:52` `list_tools` 取 `getattr(t, "annotations", None) or {}` 填进 `McpToolCard`。

### 4.3 PermissionPolicy（`permissions/policy.py`）
- `check(tool, args)` 的 tier 查找从 `self._tools.get(tool, self._global_default)` 改为三层：
  ```python
  def _tier_for(self, tool: str) -> str:
      if tool in self._tools:                      # 1. 精确名
          return self._tools[tool]
      prefix = tool.split(".", 1)[0] + ".*"        # 2. {server}.* 前缀通配
      if prefix in self._tools:
          return self._tools[prefix]
      return self._global_default                  # 3. 兜底
  ```
  `allow_always` override 仍精确名（[policy.py:62-74](../../../twinkle/agentserver/permissions/policy.py#L62-L74)），不做通配——override 是用户对具体工具点的决策。
- 新增 `def set_tier(self, tool: str, tier: str)` / `def update_tools(self, mapping: dict)`：运行时改 `_tools` dict，立即生效（无 mtime，直接改内存）。供 annotations 自动分级 + `set_tool_permission` RPC 调用。
- 新增 `def clear_server(self, server: str)`：移除某 server 的所有精确名 + `"server.*"` 条目（remove_server 时清理）。

### 4.4 annotations 自动分级（agent 协调层）
- agent `add_mcp_server` 拿到工具列表后，对每个工具读 annotations 定档并 `set_tier`：
  ```python
  def _auto_tier(self, annotations: dict) -> str:
      if annotations.get("readOnlyHint"):           return "allow"
      if annotations.get("destructiveHint") is not None:  return "require-approval"
      return None  # 未标注 → 不 set,走 server 通配/global_default 兜底
  ```
  `destructiveHint` 显式 true 或 `readOnlyHint` 显式 false → `require-approval`；未标注 → 不 `set_tier`，落 server 通配兜底。
- 用户可经 `mcp.set_tool_permission` RPC 覆盖自动分级结果（`set_tier` 覆盖 `_tools` 里的值）。

### 4.5 agent（`agent.py`）
- `create_agent` 时存 `self._permission_policy` 引用（从 `PermissionHook` 取或构造时传入）；已有 `self._progressive_hook`（refresh spec 加的）。
- 新增 `async def add_mcp_server(self, server_config)`：`tools = await mcp_manager.add_server(server_config)` → 逐个 `tm.register(tool)` + 自动分级 `set_tier` → `progressive_hook.recompute_eager()`（若启用）。
- 新增 `async def remove_mcp_server(self, name)`：`names = await mcp_manager.remove_server(name)` → 逐个 `tm.unregister(n)` + `policy.clear_server(name)` → `recompute_eager()`。
- 新增 `def set_tool_permission(self, tool, tier)`：`policy.set_tier(tool, tier)` + `recompute_eager()`（审批级↔deferred 可能变位）。

### 4.6 MCP RPC（`mcp/rpc.py`，学 `skills/rpc.py` 模式）
- `handles_mcp_rpc(method)` / `dispatch_mcp_rpc` / `run_mcp_rpc`（后台任务版）。
- 方法表：

| RPC method | 内联/后台 | 职责 |
|---|---|---|
| `mcp.list_servers` | 内联 | 返回已接入 server 摘要 |
| `mcp.add_server` | 后台任务 | 建连+注册+自动分级，完成发 `e2a.result` |
| `mcp.remove_server` | 后台任务 | 断连+注销+清权限，完成发 `e2a.result` |
| `mcp.set_tool_permission` | 内联 | 运行时改某工具档位，热生效 |

- `server.py` ws_handler 路由加 `handles_mcp_rpc` 分支（对齐 [server.py:189](../../../twinkle/agentserver/server.py#L189) skill RPC 路由模式）：内联方法直接 dispatch，`add_server`/`remove_server` 起后台任务。

### 4.7 配置（`config/schema.py` + `config.yaml`）
- `permissions.tools` 的 `PermissionTier` 值允许 `"alipay.*"` 形式 key（已是 `dict[str, str]`，无需改类型，只是 check 逻辑支持通配）。
- `config.yaml` 注释补 `"server.*": require-approval` 用法示例。
- 不新增配置项——运行时引入的 server 权限不进 config.yaml。

## 5. 数据流

### 运行时接入支付宝 server

```
前端发 mcp.add_server RPC: {name:"alipay", transport:"streamable-http", url:"..."}
  └─ ws_handler 路由 → 后台任务 run_mcp_rpc
     └─ agent.add_mcp_server(server_config)
        ├─ mcp_manager.add_server(cfg)
        │   ├─ 建连(client_factory) + list_tools(带 annotations)
        │   └─ 更新 _tools/_clients/_server_resources + 返回 list[McpToolCard]
        ├─ for tool in tools:
        │   ├─ tm.register(tool)                          # 工具进 ToolManager
        │   └─ policy.set_tier(tool.card.name, auto_tier) # annotations 自动分级
        │       transfer_money  destructive → require-approval
        │       query_balance   readOnly    → allow
        │       (未标注的不 set → 走 server 通配/global_default)
        └─ progressive_hook.recompute_eager()              # 审批级拉回 eager,其余落 deferred
  └─ 完成发 e2a.result {ok:true, server:"alipay", tool_count:N}
```

### 模型调用新接入的工具

```
模型调 alipay.transfer_money (eager,因 require-approval 被 _force_protected_eager 拉回)
  → _tool_call → before_tool_call → PermissionHook
  → policy.check("alipay.transfer_money", args)
      ├─ allow_always override? 否
      ├─ _tier_for: 精确名 "alipay.transfer_money" 在 _tools → require-approval → ASK
      └─ (若精确名不在,查 "alipay.*" 通配,再 global_default)
  → ASK → 前端审批 → allow / allow_always(持久化,热重载) / deny
```

## 6. 错误处理

- `add_server` 建连失败：后台任务捕获，发 `e2a.result {ok:false, error:...}`，不污染 tm/policy（McpManager 视图回滚）。
- `add_server` 部分工具 `list_tools` 失败：整 server 视为接入失败（不半接入），对齐 `refresh_all` 的失败降级（[mcp-tool-refresh spec](./2026-08-25-mcp-tool-refresh-design.md) §6）。
- `remove_server` 未知名：no-op + 返回空列表。
- `set_tool_permission` 未知名：`set_tier` 仍写入（容许预配未知工具），不报错。
- annotations 缺失：未标注工具不 `set_tier`，走兜底（server 通配 / global_default），仍安全。
- 运行时改档位后，已挂起的 ASK 不受影响（档位变更对下一个 tool_call 生效）。

## 7. 范围

**In scope**：
- MCP server 运行时 add/remove（RPC + agent 协调 + McpManager API）。
- 运行时 permissions store（`set_tier`/`update_tools`/`clear_server` + 前缀通配 check）。
- MCP annotations 自动分级（readOnly/destructive → 档位）。
- `mcp.set_tool_permission` RPC 覆盖自动分级。
- progressive 联动（add/remove 后 `recompute_eager`）。

**Out of scope（不做）**：
- `require-approval-always` 档 / 禁止 allow_always（用户已决定不要强制每次审）。
- 工具级 args 粒度审批（allow_always 仍整工具级）。
- MCP server 鉴权/凭证管理（连接信息含 url/transport，凭证管理另做）。
- 运行时编辑内置工具档位（聚焦 MCP；内置仍走 config.yaml）。
- 前端管理 UI（spec 只定义 RPC 协议，UI 另做）。
- 动态接入 server 的持久化（重启后运行时接入的 server 丢失——若要跨重启存活，后续把运行时 store 落盘，本 spec 不做）。

## 8. 成功标准

- **运行时接入**：agent 常驻运行时，前端发 `mcp.add_server` RPC → 新 server 工具立即可用（模型能在下一轮调用），不碰 config.yaml、不重启。
- **自动分级**：接入后 `readOnlyHint=true` 工具自动 `allow`（免审）、`destructiveHint=true` 自动 `require-approval`（审批），无需手配。
- **运行时改档**：`mcp.set_tool_permission` RPC 改某工具档位 → 下次调用热生效（不重启）。
- **server 通配**：`"alipay.*": require-approval` 在 config.yaml 和运行时 store 都生效；精确名覆盖通配（`alipay.query_balance: allow`）。
- **审批门 intact**：progressive enabled 时，运行时接入的审批级 MCP 工具被 `_force_protected_eager` 拉回 eager，走 `_tool_call` → `before_tool_call` → 审批，不被 `invoke_tool` 绕过。
- **向后兼容**：静态 config 接入路径（`mcp.servers` + 启动 `startup`）不受影响；现有测试全绿；progressive 关闭时行为等同现状。
- **prefix cache**：运行时接入后，同一 invoke 内各步 `tools` 列表仍逐字节相同（接入在请求边界外，不破坏冻结）。
- **allow_always 热重载**：override 文件 mtime 热重载机制不变，运行时点 allow_always 持久化免审跨重启存活。

## 9. 与现有机制的关系

- **progressive tool visibility**：add/remove server 后调 `recompute_eager()`，对齐 [mcp-tool-refresh spec](./2026-08-25-mcp-tool-refresh-design.md) §4.2 的 refresh 联动。新 MCP 工具默认落 deferred（不在 builtin_names），审批级拉回 eager。
- **MCP 工具刷新**：运行时接入的 server 同样进 `_server_resources`，`refresh_all` 的 TTL 刷新对它生效（server 端后续增删工具仍能发现）。
- **allow_always override**：运行时权限 store 改的是 `_tools`（tier），override 是独立的 `_cache`（allow_always 持久化），两者正交，check 时 override 优先（[policy.py:80](../../../twinkle/agentserver/permissions/policy.py#L80)）。
- **Skill 模式复用**：MCP RPC 的后台任务 + `e2a.result` 完成通知模式，直接照搬 `skills.install`（[skills/rpc.py:62-107](../../../twinkle/agentserver/skills/rpc.py#L62-L107)）。
