# MCP 工具清单"请求边界 + TTL 刷新"设计

> 日期：2026-08-25
> 状态：设计已与用户确认，待写实现计划
> 关联记忆：[[phase15-mcp-landed]] [[progressive-tool-visibility-landed]]

## 1. 背景与目标

Twinkle 当前 MCP 工具是 eager 单例：进程启动时 `McpManager.startup()` 一次性 `connect + list_tools` 拉全量存进 dict（`mcp/manager.py:31-48`），`register_into` 复制进 ToolManager，此后不再刷新。server 端后续新增/删除的工具，本地永远看不到。

**目标**：MCP server 端工具增删后，能在**请求边界**（每个用户请求进 `run()` 时）被发现并反映到 LLM 工具集；请求内 schemas 仍冻结，保 prefix cache 命中。

**对齐 jiuwenswarm**：借鉴其 `refresh_tool_server` 的 TTL 节流思路（`openjiuwen/.../resources_manager/tool_manager.py:204-224`：每轮检查、TTL 过期才真拉 `list_tools`、复用 client 不重连），但：
- 触发点放**请求边界**而非 jiuwenswarm 的**每轮**——Twinkle 已为 prefix cache 付"每步冻结 schemas"代价（`agent.py:497-499` 注释明说），学每轮会破坏 cache。
- 修 jiuwenswarm 的缺陷：`_inner_refresh_mcp_tools`（`tool_manager.py:226-240`）重拉后直接 `add_tool`、对已存在 id 抛 `ValueError`、且不删 server 端已移除的工具。Twinkle 用覆盖式 `register` + diff 删旧。

## 2. 核心决策（与用户确认）

1. **TTL 默认 = 300 秒（默认开启）**。MCP 设计哲学即 server 动态；默认 None 等于功能不生效。300s 工程合理：TTL 内请求秒返回零网络开销，真拉走已建 client 调 `list_tools`（不重连）单次几十 ms 分摊到 5 分钟一次可忽略。`config.tool_refresh_ttl` 可调；`None` = opt-out 永不自动刷新。
2. **失败降级 = 沿用旧清单 + log.warning**。MCP 是辅助能力，不因某 server 抽风连累主请求。对齐 jiuwenswarm startup 连不上 server 也只 `log.warning` 跳过。
3. **progressive 联动 = refresh 后重跑 `_force_protected_eager`**。保持 #1 审批门守卫 intact（progressive enabled + 新增非 allow 档工具才触发，窄但真实，不留理论绕过点）。

## 3. 方案选择：解耦 McpManager ↔ ToolManager

**方案 B（选定）**：`McpManager.refresh_all()` 返回 `list[_ToolDiff]`（added/removed），agent.py 应用到 tm + 触发 progressive。McpManager 不持 tm 引用。

| 方案 | McpManager→ToolManager | 评价 |
|---|---|---|
| A1（弃） | 持 `_tm` 引用，直接 register/unregister | McpManager 从一次性注入变长期持改 ToolManager，紧耦合 |
| **B（选）** | 不持 tm，返回 diff | 子系统互不知晓，协调集中 agent |

**为什么 B**：McpManager 职责纯化为"MCP 协议交互 + diff 计算"（管自己的 `_tools` 视图 + `_server_resources`），ToolManager 保持通用容器不渗 MCP 语义，ProgressiveToolHook 只管 eager。三者互不直接引用，agent 作协调者（它本就装配三者）。代价：agent.py 多约 4 行协调代码（应用 diff + 触发 recompute），显式且合理。

## 4. 组件改动

### 4.1 McpManager（`twinkle/agentserver/mcp/manager.py`）
- 新增 `_McpServerResource` dataclass：`name: str, client: McpClient, tool_names: set[str], last_update: float, expiry: float | None`
- 新增 `_server_resources: dict[str, _McpServerResource]`；`startup()` 填充它（连同现有 `_tools`/`_clients`）
- 新增 `_ToolDiff` dataclass：`added: list[Tool], removed: list[str]`
- 新增 `async def refresh_all(force: bool = False) -> list[_ToolDiff]`：per-server TTL 判断，过期/force 才 `await asyncio.wait_for(client.list_tools(), call_timeout)` 重拉、diff 计算、更新自己 `_tools` 视图、返回 diff；失败降级（不返回该 server diff）
- `register_into(tm)` 不变（仍用于初始全量注入）

### 4.2 ProgressiveToolHook（`hooks/builtin/progressive_tool_hook.py` + `tools/progressive.py`）
- 构造时存 `tm` + `permissions` 引用（`apply_progressive_tools(tm, config, permissions)` 本就传入）
- 新增 `recompute_eager()` 无参方法：复用 `_force_protected_eager` 逻辑重算 eager 集合（新 MCP 工具非 allow 档拉回 eager）

### 4.3 agent.py
- `create_agent` 保留 progressive hook 引用（`self._progressive_hook`）
- `run()` 在 `agent.py:499` 取 schemas 之前：
  ```python
  for d in await mcp_manager.refresh_all():
      for name in d.removed: self._tool_manager.unregister(name)
      for tool in d.added:   self._tool_manager.register(tool)
  if self._progressive_hook is not None:
      self._progressive_hook.recompute_eager()
  ```

### 4.4 配置（`twinkle/config/schema.py` + `twinkle/resources/config.yaml`）
- `McpConfig` 加 `tool_refresh_ttl: float | None = 300.0`
- `config.yaml` mcp 段加 `tool_refresh_ttl: 300` + 注释

## 5. 数据流

```
请求进 run()
  ├─ await mcp_manager.refresh_all()              # agent.py:499 之前
  │    ├─ per-server TTL 判断（TTL 内秒返回）
  │    ├─ 过期 → wait_for(list_tools, call_timeout) 重拉 + diff + 更新 _tools 视图
  │    ├─ 失败 → log.warning，不返回 diff（tm 不变）
  │    └─ 返回 list[_ToolDiff]
  ├─ agent 应用 diff 到 tm（unregister removed / register added）
  ├─ progressive_hook.recompute_eager()（若 progressive enabled）
  ├─ tool_schemas = self._tool_manager.schemas()   # 现有 line 499，整轮冻结
  └─ for _step ...                                  # 每步复用 schemas → prefix cache 命中
```

## 6. 错误处理

- `list_tools` 超时/失败：`log.warning` + 沿用旧清单（不更新 `res`，不返回 diff，下次请求再试）
- startup 时连不上的 server：不在 `_server_resources`，`refresh_all` 天然跳过
- `force=True`：调试入口，绕过 TTL
- `tool_refresh_ttl=None`：永不自动刷新（opt-out），仅 force 触发

## 7. 测试计划（TDD 先行）

1. TTL 内不调 `list_tools`（mock client，断言不被调）
2. TTL 过期调 `list_tools` + register added + unregister removed
3. `list_tools` 失败/超时 → 降级，tm 内容不变，返回空 diff
4. `force=True` 强刷（绕过 TTL）
5. progressive enabled + 新增非 allow 档 MCP 工具 → `recompute_eager` 拉回 eager
6. 默认 `ttl=300` 行为（config 默认值生效）
7. 现有 MCP 测试不回归（`startup/register_into/release`）——现有 mock 的 `list_tools` 需可重入

## 8. 不做的事（取舍）

- 不抄 jiuwenswarm 每轮重算（破坏 prefix cache）
- 不抄 `add_tool` 重复抛异常（用覆盖式 register）
- 不抄文件 watch/auto-reload（两参考都没做；本地 @tool 是 .py 函数不适合）
- 不抄 RPC `tools.add` / 请求级热替换 / 扩展插件（jiuwenswarm 企业特性，Twinkle out of scope）
- 不做工具 version 管理（两参考都没有，无可借鉴）
- 不做 MCP `tools/list_changed` 通知订阅（两参考都没用，且依赖 server 发通知；TTL 轮询够用）

## 9. 改动文件清单

| 文件 | 改动 |
|---|---|
| `twinkle/agentserver/mcp/manager.py` | `_McpServerResource` + `_ToolDiff` + `_server_resources` + `refresh_all()`；`startup` 填充新结构 |
| `twinkle/agentserver/agent.py` | `:499` 前调 `refresh_all()` + 应用 diff + `recompute_eager()`；`create_agent` 存 progressive hook 引用 |
| `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py` + `tools/progressive.py` | `recompute_eager()`；`_force_protected_eager` 提为可复用 |
| `twinkle/config/schema.py` | `McpConfig.tool_refresh_ttl: float | None = 300.0` |
| `twinkle/resources/config.yaml` | mcp 段加 `tool_refresh_ttl: 300` |
| `tests/test_mcp_manager.py`（新建/扩） | §7 的 7 条 TDD |
