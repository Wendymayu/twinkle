# MCP 运行时动态接入 + 动态权限 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agent 常驻运行时通过 RPC 动态接入/移除 MCP server + 运行时配审批档位 + 接入即按 MCP annotations 自动分级危险/只读,全程不碰 `config.yaml`、不重启。

**Architecture:** McpManager 加 `add_server`/`remove_server`/`list_servers`(不持 tm,返回 `list[McpTool]`,agent 协调 register);PermissionPolicy 加运行时 store(`set_tier`/`update_tools`/`clear_server`)+ 前缀通配 `_tier_for`(精确名 → `{server}.*` → global_default)+ `tools`/`global_default` property(让 progressive 守卫读运行时档位);agent 加 `add_mcp_server`/`remove_mcp_server`/`set_tool_permission`/`_auto_tier` 协调方法;新建 `mcp/rpc.py`(学 `skills/rpc.py` 后台任务+`e2a.result` 模式);`server.py` 加 `handles_mcp_rpc` 路由。

**Tech Stack:** Python asyncio, pydantic, pytest(无 pytest-asyncio,用 `asyncio.run()`)

**项目规则(必须遵守):** 所有 git commit 步骤执行前**先问用户确认**([[no-direct-github-push]])。每个 Task 末尾的 commit step 执行时暂停,问用户"是否 commit"后再执行 `git commit`。

**Spec:** `docs/superpowers/specs/2026-09-28-mcp-runtime-extension-design.md`

---

## 关键修正(spec vs 现实,执行者必读)

spec §4.5 假设"已有 `self._progressive_hook`(refresh spec 加的)"——**与现实不符**:agent.py 实际未存 `_progressive_hook`,`_refresh_mcp_tools(ctx)` 也不调 recompute。但 `ProgressiveToolHook.before_invoke` **每请求**调 `_force_protected_eager` 重算 eager,故 add/remove server 后**不显式调 recompute**——下一请求 `before_invoke` 自动把非 allow 档拉回 eager(审批门 intact)。

但有个真实 gap(本 plan 修):`ProgressiveToolHook` 当前持 `PermissionsConfig`(静态 config),不持 `PermissionPolicy`(运行时 store)。运行时 `set_tier` 改 `policy._tools`,`_force_protected_eager` 读 config.tools **看不到** → 审批级工具若被改成 require-approval 仍留 deferred → `invoke_tool` 绕过审批门。本 plan Task 1 给 `PermissionPolicy` 加 `tools`/`global_default` property + Task 4 让 `ProgressiveToolHook` 持 policy(经 `apply_progressive_tools` 传运行时 policy),链路才通。

---

## 文件结构

| 文件 | 责任 | 改动 |
|---|---|---|
| `twinkle/agentserver/permissions/policy.py` | 档位+tier 查找 | 加 `tools`/`global_default` property;`_tier_for` 三层;`set_tier`/`update_tools`/`clear_server`;`check` 用 `_tier_for` |
| `twinkle/agentserver/permissions/engine.py` | 权限引擎 | 加 `policy` property(暴露所持 PermissionPolicy) |
| `twinkle/agentserver/mcp/tool.py` | MCP 工具 card | `McpToolCard` 加 `annotations: dict \| None = None` |
| `twinkle/agentserver/mcp/client.py` | MCP 传输客户端 | `list_tools` 保留 `annotations` |
| `twinkle/agentserver/mcp/manager.py` | MCP 进程单例 | 加 `add_server`/`remove_server`/`list_servers`;import 加 `McpToolCard` |
| `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py` | progressive 可见性 hook | `__init__` 注释说明持 PermissionPolicy(代码不变,语义变) |
| `twinkle/agentserver/tools/progressive.py` | progressive builder | `apply_progressive_tools` docstring 改(参数语义:policy 优先) |
| `twinkle/agentserver/agent.py` | ReActAgent | `__init__` 加 `permission_policy` 参数;加 `_auto_tier`/`add_mcp_server`/`remove_mcp_server`/`set_tool_permission` |
| `twinkle/agentserver/server.py` | AgentServer 入口 | `create_agent` 取 policy 传 progressive + 存 agent;`ws_handler` 加 `handles_mcp_rpc` 路由;import mcp.rpc |
| `twinkle/agentserver/mcp/rpc.py` | MCP RPC dispatch | **新建**(学 `skills/rpc.py`) |
| `twinkle/resources/config.yaml` | 用户配置 | permissions.tools 段补 `"server.*"` 通配注释示例 |
| `tests/test_permissions_policy.py` | policy 测试 | 加 set_tier/wildcard/clear_server/property 测试 |
| `tests/test_mcp_manager.py` | McpManager 测试 | `_FakeClient` 支持 annotations;加 add/remove/list_servers 测试 |
| `tests/test_progressive_tool_hook.py` | progressive hook 测试 | **新建**(测持 policy 反映 set_tier) |
| `tests/test_agent_loop.py` | agent 测试 | 加 `_permission_policy` 存储 + 协调方法测试 |
| `tests/test_mcp_rpc.py` | MCP RPC 测试 | **新建** |

---

### Task 1: PermissionPolicy 运行时 store + 前缀通配 check + property

**Files:**
- Modify: `twinkle/agentserver/permissions/policy.py`(property + `_tier_for` + `check` + `set_tier`/`update_tools`/`clear_server`)
- Test: `tests/test_permissions_policy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_permissions_policy.py` 末尾加:

```python
def test_set_tier_reflects_in_check(tmp_path):
    p = _policy(tmp_path, tools={"echo": "allow"})
    assert p.check("echo", {}).level == "allow"
    p.set_tier("echo", "require-approval")
    assert p.check("echo", {}).level == "ask"


def test_tier_server_wildcard_matches(tmp_path):
    p = _policy(tmp_path, tools={"alipay.*": "require-approval"})
    assert p.check("alipay.transfer", {}).level == "ask"
    assert p.check("alipay.query", {}).level == "ask"


def test_tier_precise_name_overrides_wildcard(tmp_path):
    p = _policy(tmp_path, tools={"alipay.*": "require-approval", "alipay.query": "allow"})
    assert p.check("alipay.transfer", {}).level == "ask"
    assert p.check("alipay.query", {}).level == "allow"


def test_clear_server_removes_exact_and_wildcard(tmp_path):
    p = _policy(tmp_path, tools={"alipay.*": "require-approval", "alipay.query": "allow",
                                  "other.x": "allow"})
    p.clear_server("alipay")
    assert "alipay.*" not in p.tools
    assert "alipay.query" not in p.tools
    assert "other.x" in p.tools


def test_policy_tools_property_reflects_set_tier(tmp_path):
    # gap C 修复:_force_protected_eager 读 policy.tools(运行时),set_tier 立即反映
    p = _policy(tmp_path, tools={"mcp.safe": "allow"})
    assert p.tools["mcp.safe"] == "allow"
    p.set_tier("mcp.safe", "require-approval")
    assert p.tools["mcp.safe"] == "require-approval"


def test_update_tools_batch(tmp_path):
    p = _policy(tmp_path, tools={})
    p.update_tools({"a.x": "allow", "b.y": "require-approval"})
    assert p.tools == {"a.x": "allow", "b.y": "require-approval"}


def test_global_default_property(tmp_path):
    p = _policy(tmp_path, default="require-approval")
    assert p.global_default == "require-approval"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_permissions_policy.py -k "set_tier_reflects or server_wildcard or precise_name_overrides or clear_server or tools_property or update_tools or global_default_property" -v`
Expected: FAIL with `AttributeError: 'PermissionPolicy' object has no attribute 'set_tier'` / `'_tier_for'` / `'tools'`(property)

- [ ] **Step 3: 加 property(在 `__init__` 后、`_load_overrides` 前)**

`twinkle/agentserver/permissions/policy.py`,把:

```python
        self._cache: dict[str, Any] = dict(approval_overrides)

    # --- override 加载(mtime 缓存热重载) ---
```

改为:

```python
        self._cache: dict[str, Any] = dict(approval_overrides)

    # --- 运行时 store 暴露(_force_protected_eager 读 policy.tools/global_default) ---

    @property
    def tools(self) -> dict[str, str]:
        """运行时 tier store(供 _force_protected_eager 读取;set_tier 改动立即反映)。"""
        return self._tools

    @property
    def global_default(self) -> str:
        return self._global_default

    # --- override 加载(mtime 缓存热重载) ---
```

- [ ] **Step 4: 加 `_tier_for`(在 `check` 前)**

把:

```python
    # --- check ---

    def check(self, tool: str, args: dict) -> PermissionDecision:
```

改为:

```python
    # --- tier 查找 ---

    def _tier_for(self, tool: str) -> str:
        """三层 tier 查找:1.精确名 2.{server}.* 前缀通配 3.global_default。
        支持接入前未知工具名时按 server 配档("alipay.*": require-approval)。"""
        if tool in self._tools:
            return self._tools[tool]
        prefix = tool.split(".", 1)[0] + ".*"
        if prefix in self._tools:
            return self._tools[prefix]
        return self._global_default

    # --- check ---

    def check(self, tool: str, args: dict) -> PermissionDecision:
```

- [ ] **Step 5: `check` 用 `_tier_for`**

把:

```python
        # tier —— 将 "require-approval" 档位标签归一化为标准 ASK level
        tier = self._tools.get(tool, self._global_default)
        level = PermissionLevel.ASK if tier == "require-approval" else tier
        return PermissionDecision(level=level, reason=f"tier:{tier}", source="tier")
```

改为:

```python
        # tier —— 三层查找(精确名 → {server}.* 通配 → global_default);
        # "require-approval" 档位标签归一化为标准 ASK level
        tier = self._tier_for(tool)
        level = PermissionLevel.ASK if tier == "require-approval" else tier
        return PermissionDecision(level=level, reason=f"tier:{tier}", source="tier")
```

- [ ] **Step 6: 加 `set_tier`/`update_tools`/`clear_server`(文件末尾,`persist_allow_always` 后)**

把:

```python
        self._mtime = -1.0  # 下次 check 时强制重载
        self._cache = ovr
```

改为:

```python
        self._mtime = -1.0  # 下次 check 时强制重载
        self._cache = ovr

    # --- 运行时 tier 改写(供 annotations 自动分级 + set_tool_permission RPC) ---

    def set_tier(self, tool: str, tier: str) -> None:
        """运行时改单工具 tier,立即生效(改 _tools 内存,无 mtime)。"""
        self._tools[tool] = tier

    def update_tools(self, mapping: dict[str, str]) -> None:
        """批量运行时改 tier。"""
        self._tools.update(mapping)

    def clear_server(self, server: str) -> None:
        """移除某 server 的所有精确名 + '{server}.*' 通配条目(remove_server 时清理)。"""
        prefix = server + ".*"
        for key in list(self._tools.keys()):
            if key == prefix or key.startswith(server + "."):
                self._tools.pop(key, None)
```

- [ ] **Step 7: 跑测试验证通过**

Run: `python -m pytest tests/test_permissions_policy.py -v`
Expected: PASS(7 条新测试 + 现有不回归)

- [ ] **Step 8: Commit(先问用户)**

```bash
git add twinkle/agentserver/permissions/policy.py tests/test_permissions_policy.py
```
暂停问用户"是否 commit Task 1(PermissionPolicy 运行时 store + 通配)"。同意后:
```bash
git commit -m "feat(permissions): runtime tier store + server wildcard + tools property"
```

---

### Task 2: McpToolCard + list_tools 保留 annotations

**Files:**
- Modify: `twinkle/agentserver/mcp/tool.py:10-13`(`McpToolCard`)
- Modify: `twinkle/agentserver/mcp/client.py:52-59`(`list_tools`)
- Modify: `tests/test_mcp_manager.py`(`_FakeClient.list_tools` 支持 annotations)
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_manager.py` 末尾加:

```python
def test_list_tools_preserves_annotations() -> None:
    from twinkle.agentserver.mcp.tool import McpToolCard
    fake = _FakeClient("alipay", tools=[
        ("transfer", "d", {}, {"readOnlyHint": False, "destructiveHint": True}),
        ("query", "d", {}, {"readOnlyHint": True}),
        ("plain", "d", {}),  # 三元组,无 annotations
    ])
    cards = asyncio.run(fake.list_tools())
    assert cards[0].annotations == {"readOnlyHint": False, "destructiveHint": True}
    assert cards[1].annotations == {"readOnlyHint": True}
    assert cards[2].annotations is None


def test_mcp_tool_card_default_annotations_none() -> None:
    from twinkle.agentserver.mcp.tool import McpToolCard
    c = McpToolCard(name="s.t", server_name="s", description="d", parameters={})
    assert c.annotations is None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_manager.py::test_list_tools_preserves_annotations tests/test_mcp_manager.py::test_mcp_tool_card_default_annotations_none -v`
Expected: FAIL(`McpToolCard` 无 `annotations` 字段 → `TypeError`/`AttributeError`;`_FakeClient.list_tools` 三元组解包)

- [ ] **Step 3: `McpToolCard` 加 annotations 字段**

`twinkle/agentserver/mcp/tool.py`,把:

```python
@dataclass
class McpToolCard(ToolCard):
    """带 server_name 的 ToolCard。name 形如 '{server}.{tool}'。"""
    server_name: str
```

改为:

```python
@dataclass
class McpToolCard(ToolCard):
    """带 server_name 的 ToolCard。name 形如 '{server}.{tool}'。
    annotations 保留 MCP 协议的 readOnlyHint/destructiveHint(供自动分级)。"""
    server_name: str
    annotations: dict | None = None
```

> dataclass 字段顺序:父类 `name`/`description`/`parameters`(无默认)→ `server_name`(无默认)→ `annotations`(有默认)。无默认在前、有默认在后,合法。

- [ ] **Step 4: `client.py list_tools` 取 annotations**

`twinkle/agentserver/mcp/client.py`,把:

```python
    async def list_tools(self) -> list[McpToolCard]:
        resp = await self._session.list_tools()
        return [
            McpToolCard(name=f"{self._name}.{t.name}", server_name=self._name,
                         description=getattr(t, "description", "") or "",
                         parameters=getattr(t, "inputSchema", {}) or {})
            for t in resp.tools
        ]
```

改为:

```python
    async def list_tools(self) -> list[McpToolCard]:
        resp = await self._session.list_tools()
        return [
            McpToolCard(name=f"{self._name}.{t.name}", server_name=self._name,
                         description=getattr(t, "description", "") or "",
                         parameters=getattr(t, "inputSchema", {}) or {},
                         annotations=getattr(t, "annotations", None) or None)
            for t in resp.tools
        ]
```

- [ ] **Step 5: `_FakeClient.list_tools` 支持 annotations(向后兼容三元组)**

`tests/test_mcp_manager.py` 的 `_FakeClient.list_tools`,把:

```python
    async def list_tools(self):
        self.list_tools_call_count += 1
        if self.list_tools_exc:
            raise self.list_tools_exc
        from twinkle.agentserver.mcp.tool import McpToolCard
        return [McpToolCard(name=f"{self.name}.{t}", server_name=self.name,
                            description=d, parameters=s)
                for t, d, s in self._tools]
```

改为:

```python
    async def list_tools(self):
        self.list_tools_call_count += 1
        if self.list_tools_exc:
            raise self.list_tools_exc
        from twinkle.agentserver.mcp.tool import McpToolCard
        cards = []
        for entry in self._tools:
            t, d, s = entry[0], entry[1], entry[2]
            ann = entry[3] if len(entry) > 3 else None
            cards.append(McpToolCard(name=f"{self.name}.{t}", server_name=self.name,
                                     description=d, parameters=s, annotations=ann))
        return cards
```

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: PASS(2 条新测试 + 现有 11 条不回归;`_FakeClient` 改向后兼容,现有三元组测试仍过)

- [ ] **Step 7: Commit(先问用户)**

```bash
git add twinkle/agentserver/mcp/tool.py twinkle/agentserver/mcp/client.py tests/test_mcp_manager.py
```
暂停问用户。同意后:
```bash
git commit -m "feat(mcp): preserve tool annotations in McpToolCard + list_tools"
```

---

### Task 3: McpManager add_server / remove_server / list_servers

**Files:**
- Modify: `twinkle/agentserver/mcp/manager.py`(import 加 `McpToolCard`;加三个方法)
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_manager.py` 末尾加:

```python
def test_add_server_connects_and_returns_tools() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {}), ("write", "d", {})])
    mgr = McpManager(_cfg([]), client_factory=_factory([fake]))
    tools = asyncio.run(mgr.add_server(srv))
    assert fake.connected
    assert [t.card.name for t in tools] == ["fs.read", "fs.write"]
    assert "fs.read" in mgr._tools
    assert mgr._server_resources["fs"].tool_names == {"fs.read", "fs.write"}


def test_add_server_preserves_annotations_in_cards() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="alipay", transport="streamable-http", url="http://x")
    fake = _FakeClient("alipay", tools=[("transfer", "d", {}, {"destructiveHint": True})])
    mgr = McpManager(_cfg([]), client_factory=_factory([fake]))
    tools = asyncio.run(mgr.add_server(srv))
    assert tools[0].card.annotations == {"destructiveHint": True}


def test_remove_server_disconnects_and_clears_view() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([]), client_factory=_factory([fake]))
    asyncio.run(mgr.add_server(srv))
    assert fake.connected
    removed = asyncio.run(mgr.remove_server("fs"))
    assert removed == ["fs.read"]
    assert not fake.connected
    assert "fs.read" not in mgr._tools
    assert "fs" not in mgr._server_resources


def test_remove_unknown_server_returns_empty() -> None:
    mgr = McpManager(_cfg([]), client_factory=_factory([]))
    assert asyncio.run(mgr.remove_server("nope")) == []


def test_list_servers_returns_summary() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {}), ("write", "d", {})])
    mgr = McpManager(_cfg([]), client_factory=_factory([fake]))
    asyncio.run(mgr.add_server(srv))
    assert mgr.list_servers() == [{"name": "fs", "tool_count": 2}]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_manager.py -k "add_server or remove_server or list_servers" -v`
Expected: FAIL with `AttributeError: 'McpManager' object has no attribute 'add_server'`

- [ ] **Step 3: import 加 `McpToolCard`**

`twinkle/agentserver/mcp/manager.py`,把:

```python
from twinkle.agentserver.mcp.tool import McpTool
```

改为:

```python
from twinkle.agentserver.mcp.tool import McpTool, McpToolCard
```

- [ ] **Step 4: 加三个方法(在 `refresh_all` 后、`release` 前)**

把:

```python
        return diffs

    async def release(self) -> None:
```

改为:

```python
        return diffs

    async def add_server(self, server_config) -> list[McpTool]:
        """运行时接入一个 MCP server:建连 + list_tools(带 annotations) + 更新自身视图。
        不碰 tm(方案B 解耦):返回 McpTool 列表,由 agent 协调 register + annotations 自动分级。
        失败抛异常(调用方捕获);不半接入(对齐 refresh_all 失败降级语义)。"""
        lock = self._locks.setdefault(server_config.name, asyncio.Lock())
        async with lock:
            client = self._client_factory(
                server_config, self._config.connect_timeout, self._config.call_timeout,
                self._config.reconnect_attempts)
            await client.connect()
            cards = await client.list_tools()
            self._clients.append(client)
            tool_names: set[str] = set()
            tools: list[McpTool] = []
            for card in cards:
                tool = McpTool(client=client, card=card)
                self._tools[tool.card.name] = tool
                tool_names.add(tool.card.name)
                tools.append(tool)
                log.info("mcp tool registered: %s", tool.card.name)
            self._server_resources[server_config.name] = _McpServerResource(
                name=server_config.name, client=client, tool_names=tool_names,
                last_update=time.time(), expiry=self._config.tool_refresh_ttl,
            )
            return tools

    async def remove_server(self, name: str) -> list[str]:
        """运行时移除一个 MCP server:断连 + 清自身视图 + 返回要 unregister 的工具名列表。
        未知名:no-op 返回空列表。"""
        res = self._server_resources.pop(name, None)
        if res is None:
            return []
        async with self._locks.setdefault(name, asyncio.Lock()):
            try:
                await res.client.disconnect()
            except Exception as exc:
                log.warning("mcp server %s disconnect error: %s", name, exc)
            for tool_name in res.tool_names:
                self._tools.pop(tool_name, None)
            if res.client in self._clients:
                self._clients.remove(res.client)
        return list(res.tool_names)

    def list_servers(self) -> list[dict]:
        """返回当前已接入 server 摘要(供 RPC 查询)。"""
        return [
            {"name": res.name, "tool_count": len(res.tool_names)}
            for res in self._server_resources.values()
        ]

    async def release(self) -> None:
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: PASS(5 条新测试 + 现有 11 条不回归)

- [ ] **Step 6: Commit(先问用户)**

```bash
git add twinkle/agentserver/mcp/manager.py tests/test_mcp_manager.py
```
暂停问用户。同意后:
```bash
git commit -m "feat(mcp): add_server/remove_server/list_servers runtime API"
```

---

### Task 4: ProgressiveToolHook 持 PermissionPolicy + create_agent/agent 接线

**Files:**
- Modify: `twinkle/agentserver/permissions/engine.py`(加 `policy` property)
- Modify: `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py:30-36`(`__init__` 注释)
- Modify: `twinkle/agentserver/tools/progressive.py:26-31`(`apply_progressive_tools` docstring)
- Modify: `twinkle/agentserver/server.py:107-112`(`create_agent` 取 policy 传 progressive + 存 agent)
- Modify: `twinkle/agentserver/agent.py:17`(typing import 加 `Any`)+ `:376-393`(`__init__` 加 `permission_policy` 参数)
- Test: `tests/test_progressive_tool_hook.py`(新建),`tests/test_agent_loop.py`

- [ ] **Step 1: 写失败测试 A — progressive hook 持 policy 反映 set_tier(gap C)**

新建 `tests/test_progressive_tool_hook.py`:

```python
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.base import ToolCard


def _make_tool(name: str) -> LocalFunction:
    async def _fn(args): return "ok"
    return LocalFunction(ToolCard(name=name, description="d", parameters={}), _fn)


def test_progressive_hook_reads_runtime_policy_set_tier():
    """gap C: ProgressiveToolHook 持 PermissionPolicy,set_tier 改档立即反映到 _force_protected_eager。"""
    policy = PermissionPolicy(tools={"mcp.safe": "allow"}, rules=[], approval_overrides={},
                              global_default="allow", overrides_file=None)
    tm = ToolManager()
    tm.register(_make_tool("mcp.safe"))
    eager = set()
    hook = ProgressiveToolHook(eager, policy)
    from twinkle.agentserver.tools.progressive import _force_protected_eager
    _force_protected_eager(tm, hook.eager_names, hook._permissions)
    assert "mcp.safe" not in hook.eager_names  # allow 档 → 留 deferred
    policy.set_tier("mcp.safe", "require-approval")  # 运行时改档
    _force_protected_eager(tm, hook.eager_names, hook._permissions)
    assert "mcp.safe" in hook.eager_names  # 非允许 → 拉回 eager(审批门 intact)
```

> 若 `LocalFunction` 构造签名不同,查 `twinkle/agentserver/tools/local_function.py` 对齐;或用最小 `_FakeTool`:`class _T:\n  def __init__(s,c): s._c=c\n  @property\n  def card(s): return s._c\n  async def invoke(s,a): return ""`,构造 `_T(ToolCard(name="mcp.safe", description="d", parameters={}))`。

- [ ] **Step 2: 写失败测试 B — agent __init__ 存 permission_policy**

在 `tests/test_agent_loop.py` 加(若该文件有现成 agent 构造 fixture,复用;否则用最小构造):

```python
def test_agent_init_stores_permission_policy() -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    policy = object()  # 任意引用,只验存储
    agent = ReActAgent(llm=None, store=None, tools=ToolManager(), hooks=(), permission_policy=policy)
    assert agent._permission_policy is policy


def test_agent_init_permission_policy_defaults_none() -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    agent = ReActAgent(llm=None, store=None, tools=ToolManager(), hooks=())
    assert agent._permission_policy is None
```

> `asyncio`/`monkeypatch` 由 conftest 提供。`llm=None, store=None` 仅构造不调用,不碰 llm/store。

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/test_progressive_tool_hook.py tests/test_agent_loop.py::test_agent_init_stores_permission_policy tests/test_agent_loop.py::test_agent_init_permission_policy_defaults_none -v`
Expected: FAIL(`PermissionEngine` 无 `policy` property → AttributeError;`ReActAgent.__init__` 无 `permission_policy` 参数 → TypeError)

- [ ] **Step 4: `engine.py` 加 `policy` property**

`twinkle/agentserver/permissions/engine.py`,把:

```python
        self._policy = policy
        self._audit = audit
        self._enabled = enabled
        self._channels = set(enabled_channels)

    def check(
```

改为:

```python
        self._policy = policy
        self._audit = audit
        self._enabled = enabled
        self._channels = set(enabled_channels)

    @property
    def policy(self) -> PermissionPolicy:
        """暴露所持 PermissionPolicy(供 create_agent 取引用存进 agent 协调层)。"""
        return self._policy

    def check(
```

> `PermissionPolicy` 已在 engine.py line 11 import,可直接用作返回注解。

- [ ] **Step 5: `progressive_tool_hook.py` `__init__` 注释说明持 policy**

`twinkle/agentserver/hooks/builtin/progressive_tool_hook.py`,把:

```python
        # 非 set 入参转 set(_force_protected_eager 需 .add)。
        self.eager_names = eager_names if isinstance(eager_names, set) else set(eager_names)
        self.eager_names |= META_NAMES   # before_model_call 过滤需含 meta 名
        self._permissions = permissions
```

改为:

```python
        # 非 set 入参转 set(_force_protected_eager 需 .add)。
        self.eager_names = eager_names if isinstance(eager_names, set) else set(eager_names)
        self.eager_names |= META_NAMES   # before_model_call 过滤需含 meta 名
        # permissions = PermissionPolicy(运行时 store,_force_protected_eager 读
        # policy.tools/global_default,set_tier 改档立即反映 before_invoke 重算);
        # fallback PermissionsConfig(静态,无 PermissionHook 装配/测试用)。
        self._permissions = permissions
```

> 赋值代码不变,只加注释说明语义(permissions 现在是 PermissionPolicy,经 create_agent 传入)。`_force_protected_eager` 用 `getattr(permissions, "tools", {})` / `getattr(permissions, "global_default", "allow")`,PermissionPolicy 经 Task 1 的 property 暴露这两个属性,透明工作。

- [ ] **Step 6: `progressive.py` docstring 改**

`twinkle/agentserver/tools/progressive.py`,把:

```python
def apply_progressive_tools(tm: Any, config: Any, permissions: Any):
    """启用渐进可见:注册 meta-tool + 返回 hook;否则返回 None。

    permissions: PermissionsConfig,用于权限守卫(读 permissions.tools[name] 或
    global_default 判定静态档位;非 allow 档的工具强制 eager)。
    """
```

改为:

```python
def apply_progressive_tools(tm: Any, config: Any, permissions: Any):
    """启用渐进可见:注册 meta-tool + 返回 hook;否则返回 None。

    permissions: PermissionPolicy(运行时 store,优先)或 PermissionsConfig(静态 fallback)。
    _force_protected_eager 读 permissions.tools/global_default——PermissionPolicy 经
    property 暴露,set_tier 运行时改档立即反映到 before_invoke 重算(审批级工具拉回 eager)。
    """
```

- [ ] **Step 7: `agent.py` typing import 加 `Any`**

`twinkle/agentserver/agent.py:17`,把:

```python
from typing import AsyncIterator, Protocol
```

改为:

```python
from typing import Any, AsyncIterator, Protocol
```

> `agent.py:8` 有 `from __future__ import annotations`,注解延迟求值;但补 `Any` 让 type checker 与可读性一致。

- [ ] **Step 8: `agent.py` `__init__` 加 `permission_policy` 参数**

把:

```python
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        *,
        hooks: tuple[AgentHook, ...] = (),
        inbox: _Inbox | None = None,
        base_sections: list[PromptSection] | None = None,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        for h in hooks:
            self._hook_manager.register_hook(h)
        self._inbox = inbox
        self._base_sections = base_sections  # None → 按 mode 取 normal/leader;list → member/subagent
```

改为:

```python
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        *,
        hooks: tuple[AgentHook, ...] = (),
        inbox: _Inbox | None = None,
        base_sections: list[PromptSection] | None = None,
        permission_policy: Any = None,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        for h in hooks:
            self._hook_manager.register_hook(h)
        self._inbox = inbox
        self._base_sections = base_sections  # None → 按 mode 取 normal/leader;list → member/subagent
        self._permission_policy = permission_policy  # 运行时权限 store(add_mcp_server set_tier 用);None=无权限装配
```

- [ ] **Step 9: `server.py` create_agent 取 policy 传 progressive + 存 agent**

`twinkle/agentserver/server.py`,把:

```python
    # Progressive tool visibility(opt-in;默认关 = no-op)
    from twinkle.agentserver.tools.progressive import apply_progressive_tools
    progressive_hook = apply_progressive_tools(tools, settings.progressive_tool, settings.permissions)
    if progressive_hook is not None:
        all_hooks.append(progressive_hook)
    return ReActAgent(llm, store, tools, hooks=tuple(all_hooks))
```

改为:

```python
    # 运行时权限 store:从 hooks 取 PermissionHook → engine.policy(若有),供 progressive
    # 守卫读运行时档位 + agent 协调层(add_mcp_server set_tier)用。无 PermissionHook 时 fallback config。
    permission_policy = None
    for h in (hooks or []):
        from twinkle.agentserver.hooks.builtin import PermissionHook
        if isinstance(h, PermissionHook):
            permission_policy = h._engine.policy
            break
    # Progressive tool visibility(opt-in;默认关 = no-op)
    from twinkle.agentserver.tools.progressive import apply_progressive_tools
    progressive_source = permission_policy or settings.permissions
    progressive_hook = apply_progressive_tools(tools, settings.progressive_tool, progressive_source)
    if progressive_hook is not None:
        all_hooks.append(progressive_hook)
    return ReActAgent(llm, store, tools, hooks=tuple(all_hooks), permission_policy=permission_policy)
```

> `PermissionHook` 已在 `twinkle.agentserver.hooks.builtin` 包(line 89-97 的 import 块附近),局部 import 避循环。`h._engine.policy` 用 Task 4 Step 4 加的 property。测试装配传 `hooks=()` 时 `permission_policy=None`,progressive fallback `settings.permissions`(行为同现状)。

- [ ] **Step 10: 跑测试验证通过**

Run: `python -m pytest tests/test_progressive_tool_hook.py tests/test_agent_loop.py -v`
Expected: PASS(progressive hook 1 条 + agent init 2 条;现有不回归)

- [ ] **Step 11: 跑 progressive + permissions 回归**

Run: `python -m pytest tests/test_progressive_tool_hook.py tests/test_permissions_policy.py tests/test_mcp_permissions.py -v`
Expected: PASS(progressive 持 policy 不破坏现有权限测试;`_force_protected_eager` 对 PermissionsConfig fallback 仍工作——getattr 透明)

- [ ] **Step 12: Commit(先问用户)**

```bash
git add twinkle/agentserver/permissions/engine.py twinkle/agentserver/hooks/builtin/progressive_tool_hook.py twinkle/agentserver/tools/progressive.py twinkle/agentserver/agent.py twinkle/agentserver/server.py tests/test_progressive_tool_hook.py tests/test_agent_loop.py
```
暂停问用户。同意后:
```bash
git commit -m "feat(progressive): hold PermissionPolicy + wire runtime policy to agent"
```

---

### Task 5: agent 协调方法 add_mcp_server / remove_mcp_server / set_tool_permission / _auto_tier

**Files:**
- Modify: `twinkle/agentserver/agent.py`(在 `_refresh_mcp_tools` 后加四个方法)
- Test: `tests/test_agent_loop.py`

- [ ] **Step 1: 写失败测试 A — `_auto_tier` 分级**

在 `tests/test_agent_loop.py` 加:

```python
def test_auto_tier_classification() -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    agent = ReActAgent(llm=None, store=None, tools=ToolManager(), hooks=())
    assert agent._auto_tier({"readOnlyHint": True}) == "allow"
    assert agent._auto_tier({"readOnlyHint": False, "destructiveHint": True}) == "require-approval"
    assert agent._auto_tier({"readOnlyHint": False}) == "require-approval"
    assert agent._auto_tier({"destructiveHint": True}) == "require-approval"
    assert agent._auto_tier(None) is None
    assert agent._auto_tier({}) is None
    assert agent._auto_tier({"idempotentHint": True}) is None  # 未标注 readOnly/destructive
```

- [ ] **Step 2: 写失败测试 B — `add_mcp_server` 注册 + 自动分级**

```python
def test_add_mcp_server_registers_and_auto_tiers(monkeypatch) -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.mcp.tool import McpTool, McpToolCard
    from twinkle.agentserver.permissions.policy import PermissionPolicy
    from twinkle.config.schema import McpServerConfig

    policy = PermissionPolicy(tools={}, rules=[], approval_overrides={},
                              global_default="allow", overrides_file=None)
    tm = ToolManager()
    agent = ReActAgent(llm=None, store=None, tools=tm, hooks=(), permission_policy=policy)

    transfer = McpToolCard(name="alipay.transfer", server_name="alipay", description="d",
                           parameters={}, annotations={"destructiveHint": True})
    query = McpToolCard(name="alipay.query", server_name="alipay", description="d",
                        parameters={}, annotations={"readOnlyHint": True})
    plain = McpToolCard(name="alipay.plain", server_name="alipay", description="d",
                       parameters={}, annotations=None)
    client_stub = object()

    class _FakeMgr:
        async def add_server(self, sc):
            return [McpTool(client=client_stub, card=transfer),
                    McpTool(client=client_stub, card=query),
                    McpTool(client=client_stub, card=plain)]
    from twinkle.agentserver.mcp import manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda *a, **k: _FakeMgr())

    result = asyncio.run(agent.add_mcp_server(
        McpServerConfig(name="alipay", transport="streamable-http", url="http://x")))
    assert result == {"ok": True, "server": "alipay", "tool_count": 3}
    names = {t.card.name for t in tm.list()}
    assert {"alipay.transfer", "alipay.query", "alipay.plain"} <= names
    assert policy.tools["alipay.transfer"] == "require-approval"
    assert policy.tools["alipay.query"] == "allow"
    assert "alipay.plain" not in policy.tools  # 未标注 → 不 set,走 server 通配/global_default 兜底
```

- [ ] **Step 3: 写失败测试 C — `remove_mcp_server` 注销 + 清权限**

```python
def test_remove_mcp_server_unregisters_and_clears_policy(monkeypatch) -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.permissions.policy import PermissionPolicy
    from twinkle.agentserver.tools.local_function import LocalFunction
    from twinkle.agentserver.tools.base import ToolCard

    policy = PermissionPolicy(tools={"alipay.transfer": "require-approval",
                                     "alipay.*": "require-approval"},
                              rules=[], approval_overrides={}, global_default="allow",
                              overrides_file=None)
    tm = ToolManager()
    agent = ReActAgent(llm=None, store=None, tools=tm, hooks=(), permission_policy=policy)
    async def _fn(args): return "ok"
    tm.register(LocalFunction(ToolCard(name="alipay.transfer", description="d", parameters={}), _fn))

    class _FakeMgr:
        async def remove_server(self, name):
            return ["alipay.transfer"]
    from twinkle.agentserver.mcp import manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda *a, **k: _FakeMgr())

    result = asyncio.run(agent.remove_mcp_server("alipay"))
    assert result["ok"] is True
    assert "alipay.transfer" not in {t.card.name for t in tm.list()}
    assert "alipay.transfer" not in policy.tools
    assert "alipay.*" not in policy.tools  # clear_server 清通配
```

- [ ] **Step 4: 写失败测试 D — `set_tool_permission`**

```python
def test_set_tool_permission_calls_policy_set_tier() -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.permissions.policy import PermissionPolicy

    policy = PermissionPolicy(tools={"x.y": "allow"}, rules=[], approval_overrides={},
                              global_default="allow", overrides_file=None)
    agent = ReActAgent(llm=None, store=None, tools=ToolManager(), hooks=(), permission_policy=policy)
    result = agent.set_tool_permission("x.y", "require-approval")
    assert result == {"ok": True, "tool": "x.y", "tier": "require-approval"}
    assert policy.tools["x.y"] == "require-approval"


def test_set_tool_permission_no_policy_returns_error() -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    agent = ReActAgent(llm=None, store=None, tools=ToolManager(), hooks=())
    result = agent.set_tool_permission("x.y", "require-approval")
    assert result["ok"] is False
    assert "error" in result
```

- [ ] **Step 5: 跑测试验证失败**

Run: `python -m pytest tests/test_agent_loop.py -k "auto_tier_classification or add_mcp_server_registers or remove_mcp_server_unregisters or set_tool_permission" -v`
Expected: FAIL with `AttributeError: 'ReActAgent' object has no attribute '_auto_tier'` / `'add_mcp_server'` / `'set_tool_permission'`

- [ ] **Step 6: 实现四个协调方法**

`twinkle/agentserver/agent.py`,把(`_refresh_mcp_tools` 末尾 + 空行 + `# -- 公共入口` 注释):

```python
            for tool in diff.added:
                self._tool_manager.register(tool)

    # -- 公共入口 -------------------------------------------------
```

改为:

```python
            for tool in diff.added:
                self._tool_manager.register(tool)

    # -- MCP 运行时扩展(协调层:agent 装配 tm + policy,RPC 调本层方法) ----------

    def _auto_tier(self, annotations: dict | None) -> str | None:
        """MCP annotations 自动分级:readOnlyHint=true→allow;destructiveHint 或 readOnlyHint=false
        →require-approval;未标注→None(不 set,走 server 通配/global_default 兜底)。"""
        if not annotations:
            return None
        if annotations.get("readOnlyHint") is True:
            return "allow"
        if annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False:
            return "require-approval"
        return None

    async def add_mcp_server(self, server_config) -> dict:
        """运行时接入 MCP server(协调):mcp_manager.add_server → tm.register + annotations 自动分级 set_tier。
        不显式调 progressive recompute——下一请求 before_invoke 自动重算 eager(非 allow 档拉回,#1 守卫 intact)。
        返回 {ok, server, tool_count}。"""
        from twinkle.agentserver.mcp.manager import get_mcp_manager
        mgr = get_mcp_manager()
        tools = await mgr.add_server(server_config)
        for tool in tools:
            self._tool_manager.register(tool)
            if self._permission_policy is not None:
                tier = self._auto_tier(tool.card.annotations)
                if tier is not None:
                    self._permission_policy.set_tier(tool.card.name, tier)
        return {"ok": True, "server": server_config.name, "tool_count": len(tools)}

    async def remove_mcp_server(self, name: str) -> dict:
        """运行时移除 MCP server(协调):mcp_manager.remove_server → tm.unregister + policy.clear_server。"""
        from twinkle.agentserver.mcp.manager import get_mcp_manager
        mgr = get_mcp_manager()
        names = await mgr.remove_server(name)
        for tool_name in names:
            self._tool_manager.unregister(tool_name)
        if self._permission_policy is not None:
            self._permission_policy.clear_server(name)
        return {"ok": True, "server": name, "removed": names}

    def set_tool_permission(self, tool: str, tier: str) -> dict:
        """运行时改某工具档位(协调):policy.set_tier。下一请求 before_invoke 反映(eager↔deferred 可能变位)。"""
        if self._permission_policy is None:
            return {"ok": False, "error": "permission policy not configured"}
        self._permission_policy.set_tier(tool, tier)
        return {"ok": True, "tool": tool, "tier": tier}

    # -- 公共入口 -------------------------------------------------
```

> `unregister` 方法在 ToolManager 应已存在(refresh spec 的 `_refresh_mcp_tools` 已用 `self._tool_manager.unregister(name)`)。若不存在,查 `tools/manager.py` 加;但 refresh 已落地应已有。

- [ ] **Step 7: 跑测试验证通过**

Run: `python -m pytest tests/test_agent_loop.py -k "auto_tier_classification or add_mcp_server_registers or remove_mcp_server_unregisters or set_tool_permission" -v`
Expected: PASS(5 条新测试)

- [ ] **Step 8: 跑 agent 全套回归**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: PASS(新 5 条 + 现有不回归;`_refresh_mcp_tools` 路径不受影响——协调方法独立)

- [ ] **Step 9: Commit(先问用户)**

```bash
git add twinkle/agentserver/agent.py tests/test_agent_loop.py
```
暂停问用户。同意后:
```bash
git commit -m "feat(agent): add_mcp_server/remove_mcp_server/set_tool_permission + _auto_tier"
```

---

### Task 6: MCP RPC(mcp/rpc.py 新建 + server.py 路由)

**Files:**
- Create: `twinkle/agentserver/mcp/rpc.py`
- Modify: `twinkle/agentserver/server.py`(import + `ws_handler` 路由)
- Test: `tests/test_mcp_rpc.py`(新建)

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mcp_rpc.py`:

```python
import asyncio
from twinkle.agentserver.mcp.rpc import handles_mcp_rpc, dispatch_mcp_rpc, run_mcp_rpc
from twinkle.e2a.models import E2AEnvelope


def _envelope(method, params=None, request_id="r1"):
    return E2AEnvelope(
        request_id=request_id, method=method, session_id="s1",
        channel="web", params=params or {})


def test_handles_mcp_rpc_recognizes_methods():
    assert handles_mcp_rpc("mcp.list_servers")
    assert handles_mcp_rpc("mcp.add_server")
    assert handles_mcp_rpc("mcp.remove_server")
    assert handles_mcp_rpc("mcp.set_tool_permission")
    assert not handles_mcp_rpc("skills.search")
    assert not handles_mcp_rpc("session.list")


def test_dispatch_list_servers_inline():
    from twinkle.agentserver.mcp.manager import _set_mcp_manager, McpManager
    from twinkle.config.schema import McpConfig
    _set_mcp_manager(McpManager(McpConfig(enabled=True, servers=[])))
    frames = []
    async def _run():
        async for f in dispatch_mcp_rpc(_envelope("mcp.list_servers"), agent=None):
            frames.append(f)
    asyncio.run(_run())
    assert len(frames) == 1
    assert frames[0].body["type"] == "mcp.list_servers"
    assert frames[0].body["servers"] == []
    _set_mcp_manager(None)


def test_dispatch_set_tool_permission_inline():
    class _FakeAgent:
        def set_tool_permission(self, tool, tier):
            return {"ok": True, "tool": tool, "tier": tier}
    frames = []
    async def _run():
        async for f in dispatch_mcp_rpc(
                _envelope("mcp.set_tool_permission", {"tool": "x.y", "tier": "require-approval"}),
                agent=_FakeAgent()):
            frames.append(f)
    asyncio.run(_run())
    assert frames[0].body["ok"] is True
    assert frames[0].body["tool"] == "x.y"


def test_dispatch_set_tool_permission_missing_args():
    frames = []
    async def _run():
        async for f in dispatch_mcp_rpc(
                _envelope("mcp.set_tool_permission", {}), agent=None):
            frames.append(f)
    asyncio.run(_run())
    assert frames[0].status == "failed"
    assert "error" in frames[0].body


def test_run_add_server_background_calls_agent():
    class _FakeAgent:
        async def add_mcp_server(self, sc):
            return {"ok": True, "server": sc.name, "tool_count": 2}
    sent = []
    async def _send(resp): sent.append(resp)
    asyncio.run(run_mcp_rpc(
        _envelope("mcp.add_server", {"name": "alipay", "transport": "streamable-http", "url": "http://x"}),
        _send, _FakeAgent()))
    assert len(sent) == 1
    assert sent[0].body["ok"] is True
    assert sent[0].body["server"] == "alipay"


def test_run_remove_server_background_calls_agent():
    class _FakeAgent:
        async def remove_mcp_server(self, name):
            return {"ok": True, "server": name, "removed": ["a.b"]}
    sent = []
    async def _send(resp): sent.append(resp)
    asyncio.run(run_mcp_rpc(
        _envelope("mcp.remove_server", {"name": "alipay"}),
        _send, _FakeAgent()))
    assert sent[0].body["ok"] is True
    assert sent[0].body["removed"] == ["a.b"]


def test_run_add_server_invalid_config_sends_error():
    sent = []
    async def _send(resp): sent.append(resp)
    # 缺 url,streamable-http 校验失败 → McpServerConfig ValidationError
    asyncio.run(run_mcp_rpc(
        _envelope("mcp.add_server", {"name": "alipay", "transport": "streamable-http"}),
        _send, agent=None))
    assert sent[0].status == "failed"
    assert "error" in sent[0].body
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_rpc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.rpc'`

- [ ] **Step 3: 新建 `mcp/rpc.py`**

创建 `twinkle/agentserver/mcp/rpc.py`:

```python
"""MCP 运行时扩展 RPC dispatch(学 skills/rpc.py 模式)。

- mcp.list_servers / mcp.set_tool_permission:内联(纯本地),dispatch_mcp_rpc yield 单个 e2a.result。
- mcp.add_server / mcp.remove_server:非内联 —— server.py 用 asyncio.create_task 起 run_mcp_rpc,
  完成后用连接 send() 发一个 e2a.result。不阻塞读循环(建连慢)。
失败帧 body 带 error,前端 request() 因 payload.error reject(同 dispatch_skill_rpc)。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.agentserver.mcp.rpc")

_MCP_METHODS = {"mcp.list_servers", "mcp.add_server", "mcp.remove_server", "mcp.set_tool_permission"}


def handles_mcp_rpc(method: str) -> bool:
    return method in _MCP_METHODS


def _result(envelope: E2AEnvelope, body: dict, succeeded: bool = True) -> E2AResponse:
    return E2AResponse(
        request_id=envelope.request_id,
        sequence=0,
        is_final=True,
        status="succeeded" if succeeded else "failed",
        response_kind="e2a.result",
        body=body,
    )


async def dispatch_mcp_rpc(envelope: E2AEnvelope, agent) -> AsyncIterator[E2AResponse]:
    """内联 MCP RPC(仅 list_servers / set_tool_permission)。add/remove 由 run_mcp_rpc 处理。"""
    method = envelope.method
    try:
        if method == "mcp.list_servers":
            from twinkle.agentserver.mcp.manager import get_mcp_manager
            servers = get_mcp_manager().list_servers()
            yield _result(envelope, {"type": "mcp.list_servers", "servers": servers})
        elif method == "mcp.set_tool_permission":
            tool = (envelope.params.get("tool") or "").strip()
            tier = (envelope.params.get("tier") or "").strip()
            if not tool or not tier:
                yield _result(envelope, {"type": method, "error": "tool and tier required"}, succeeded=False)
                return
            result = agent.set_tool_permission(tool, tier)
            yield _result(envelope, {"type": method, **result}, succeeded=result.get("ok", False))
        else:
            return  # add/remove 非内联 —— server.py 走 run_mcp_rpc
    except Exception as exc:
        log.exception("mcp rpc %s failed: %s", method, exc)
        yield _result(envelope, {"type": method, "error": str(exc)}, succeeded=False)


async def run_mcp_rpc(envelope: E2AEnvelope, send, agent) -> None:
    """非内联 MCP RPC(add/remove):后台任务跑,完成发一个 e2a.result。"""
    method = envelope.method
    try:
        if method == "mcp.add_server":
            from twinkle.config.schema import McpServerConfig
            sc = McpServerConfig(**(envelope.params or {}))
            result = await agent.add_mcp_server(sc)
            await send(_result(envelope, {"type": method, **result}, succeeded=result.get("ok", False)))
        elif method == "mcp.remove_server":
            name = (envelope.params.get("name") or "").strip()
            if not name:
                await send(_result(envelope, {"type": method, "error": "name required"}, succeeded=False))
                return
            result = await agent.remove_mcp_server(name)
            await send(_result(envelope, {"type": method, **result}, succeeded=result.get("ok", False)))
        else:
            await send(_result(envelope, {"type": method, "error": f"unknown mcp method: {method}"}, succeeded=False))
    except Exception as exc:
        log.exception("mcp rpc %s failed: %s", method, exc)
        await send(_result(envelope, {"type": method, "error": str(exc)}, succeeded=False))
```

> `McpServerConfig(**params)`:params 须只含 McpServerConfig 字段(name/transport/url|command/args/env/auth_headers/auth_query_params/timeout),`_StrictModel` extra="forbid" 会拒多余字段。前端发对字段即可;多余字段或缺失必填(如 streamable-http 缺 url)→ ValidationError,被 except 捕获发 error 帧。

- [ ] **Step 4: `server.py` import 加 mcp.rpc**

`twinkle/agentserver/server.py`,把:

```python
from twinkle.agentserver.skills.rpc import dispatch_skill_rpc, handles_skill_rpc, run_skill_rpc
from twinkle.agentserver.tools import tool_manager
```

改为:

```python
from twinkle.agentserver.skills.rpc import dispatch_skill_rpc, handles_skill_rpc, run_skill_rpc
from twinkle.agentserver.mcp.rpc import dispatch_mcp_rpc, handles_mcp_rpc, run_mcp_rpc
from twinkle.agentserver.tools import tool_manager
```

- [ ] **Step 5: `server.py` ws_handler 加 mcp rpc 路由(在 skill rpc 分支后)**

把(skill rpc 分支末尾 `continue` + 后续 `session_id = ...`):

```python
                    continue
                session_id = envelope.session_id or envelope.request_id
```

改为:

```python
                    continue
                if handles_mcp_rpc(envelope.method):
                    if envelope.method in ("mcp.list_servers", "mcp.set_tool_permission"):
                        async for frame in dispatch_mcp_rpc(envelope, agent):
                            await send(frame)
                    else:
                        # add/remove server: 非内联,后台任务;建连慢不阻塞读循环。
                        # 复用 skill_tasks 集合管理生命周期(cancel/cleanup)。
                        task = asyncio.create_task(run_mcp_rpc(envelope, send, agent))
                        skill_tasks.add(task)
                        task.add_done_callback(skill_tasks.discard)
                    continue
                session_id = envelope.session_id or envelope.request_id
```

> old_string 的 `                    continue\n                session_id = ...` 在 server.py 唯一(skill rpc 分支后紧跟)。复用 `skill_tasks` 集合(仅 task 容器,名字无关功能;避免新增 mcp_tasks 集合重复 cancel 逻辑)。

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_rpc.py -v`
Expected: PASS(7 条新测试)

- [ ] **Step 7: Commit(先问用户)**

```bash
git add twinkle/agentserver/mcp/rpc.py twinkle/agentserver/server.py tests/test_mcp_rpc.py
```
暂停问用户。同意后:
```bash
git commit -m "feat(mcp): runtime extension RPC (add/remove/list_servers/set_tool_permission)"
```

---

### Task 7: config.yaml 注释 + 全套回归

**Files:**
- Modify: `twinkle/resources/config.yaml`(permissions.tools 段注释)
- Test: 全套 `python -m pytest tests/ -v`

- [ ] **Step 1: config.yaml 补 server 通配注释示例**

`twinkle/resources/config.yaml` 的 `permissions.tools` 段(line 102 `read_memory: allow` 后),把:

```yaml
    read_memory: allow
  rules: []                               # 用户规则(同 jiuwenswarm rules[] 形状;v1 可空)
```

改为:

```yaml
    read_memory: allow
    # MCP server 级通配:接入前未知工具名时按 server 配档(运行时 check 三层:精确名 → {server}.* → global_default)
    # "alipay.*": require-approval        # 例:支付宝 server 全部审批;精确名覆盖通配("alipay.query": allow)
  rules: []                               # 用户规则(同 jiuwenswarm rules[] 形状;v1 可空)
```

- [ ] **Step 2: 全套回归**

Run: `python -m pytest tests/ -v`
Expected: 全套 PASS。重点确认:
- `tests/test_permissions_policy.py`(Task 1 的 7 条 + 现有 11 条)
- `tests/test_mcp_manager.py`(Task 2 的 2 条 + Task 3 的 5 条 + 现有 11 条)
- `tests/test_progressive_tool_hook.py`(Task 4 的 1 条)
- `tests/test_agent_loop.py`(Task 4 的 2 条 + Task 5 的 5 条 + 现有不回归)
- `tests/test_mcp_rpc.py`(Task 6 的 7 条)
- `tests/test_mcp_permissions.py` / `test_mcp_integration.py` / `test_mcp_reconnect.py` 不回归
- `tests/test_approval_flow.py`(权限审批流不回归)

> 若 `test_mcp_integration.py` 等因 mock 的 `list_tools` 返回旧三元组 McpToolCard(无 annotations)失败:Task 2 Step 5 已让 `_FakeClient.list_tools` 向后兼容(三元组 → annotations=None),McpToolCard.annotations 默认 None,现有测试应仍过。如 fail,按错误调整 mock。

- [ ] **Step 3: Commit(先问用户)**

```bash
git add twinkle/resources/config.yaml
```
暂停问用户。同意后:
```bash
git commit -m "docs(config): document MCP server wildcard permission tier"
```

---

## Self-Review

**1. Spec coverage(逐条对照 spec §2-§9):**

- §2 决策① MCP 学 Skill RPC 模式 → Task 6(`mcp/rpc.py` + `handles_mcp_rpc`/`dispatch_mcp_rpc`/`run_mcp_rpc`,后台任务+`e2a.result`)✓
- §2 决策② 权限真源转运行时 store → Task 1(`set_tier`/`update_tools`/`clear_server`)✓
- §2 决策③ server 级通配 → Task 1(`_tier_for` 三层)✓
- §2 决策④ annotations 自动分级 → Task 2(保留 annotations)+ Task 5(`_auto_tier`)✓
- §2 决策⑤ allow_always 保留 → 不动 override 机制(`persist_allow_always`/`_load_overrides`/`_matches_override` 不变),✓
- §2 决策⑥ 不做 always 档 → 无 require-approval-always,✓
- §4.1 McpManager add/remove/list_servers → Task 3 ✓
- §4.2 McpToolCard + list_tools annotations → Task 2 ✓
- §4.3 PermissionPolicy set_tier/clear_server/前缀通配 → Task 1 ✓
- §4.4 annotations 自动分级 → Task 5 `_auto_tier` ✓
- §4.5 agent add_mcp_server/remove_mcp_server/set_tool_permission + 存 policy 引用 → Task 4(存引用)+ Task 5(协调方法)✓
- §4.6 MCP RPC 方法表 → Task 6(list_servers 内联/add_server 后台/remove_server 后台/set_tool_permission 内联)✓
- §4.7 配置(permissions.tools 允许 "server.*" key)→ Task 1 check 支持通配 + Task 7 config.yaml 注释 ✓
- §5 数据流(接入→register+set_tier→下轮调用走审批)→ Task 5 `add_mcp_server` + Task 4 progressive 持 policy(下轮 before_invoke 拉回 eager)✓
- §6 错误处理(add 失败回滚/remove 未知名 no-op/set 未知名容错/annotations 缺失走兜底)→ Task 3 `add_server` 抛异常(调用方捕获)+ `remove_server` 返回空 + Task 5 `_auto_tier` 返回 None 不 set ✓
- §7 In scope 全覆盖 ✓
- §7 Out of scope 不做 ✓
- §8 成功标准:运行时接入(Task 3+5+6)✓ / 自动分级(Task 5)✓ / 运行时改档(Task 1+5)✓ / server 通配(Task 1)✓ / 审批门 intact(Task 4 progressive 持 policy)✓ / 向后兼容(静态路径不动)✓ / prefix cache(接入在请求边界外 RPC,不动 schemas 冻结)✓ / allow_always 热重载(不动)✓
- §9 与现有机制关系:progressive 联动(Task 4 持 policy,下轮 before_invoke 自动)+ refresh_all(运行时接入的 server 进 _server_resources,TTL 刷新对它生效)+ Skill 模式复用(Task 6 照搬)✓

**2. Placeholder scan:** 无 TBD/TODO。Task 4 Step 1 的 `_FakeTool` fallback 是对齐指引(带完整代码),非占位。所有 Edit step 有逐字 old_string + new_string。所有测试 step 有完整测试代码。✓

**3. Type consistency:**
- `PermissionPolicy.set_tier(tool: str, tier: str)`(Task 1)→ Task 5 `self._permission_policy.set_tier(tool.card.name, tier)` 一致 ✓
- `PermissionPolicy.clear_server(server: str)`(Task 1)→ Task 5 `self._permission_policy.clear_server(name)` 一致 ✓
- `PermissionPolicy.tools` property 返回 `dict[str, str]`(Task 1)→ Task 4 `_force_protected_eager` 读 `getattr(permissions, "tools", {})` 透明 ✓
- `McpManager.add_server(server_config) -> list[McpTool]`(Task 3)→ Task 5 `tools = await mgr.add_server(...)` + `for tool in tools: tm.register(tool)` 一致 ✓
- `McpManager.remove_server(name: str) -> list[str]`(Task 3)→ Task 5 `names = await mgr.remove_server(name)` + `for tool_name in names` 一致 ✓
- `McpManager.list_servers() -> list[dict]`(Task 3)→ Task 6 `get_mcp_manager().list_servers()` 一致 ✓
- `agent.add_mcp_server(server_config) -> dict`(Task 5)→ Task 6 `result = await agent.add_mcp_server(sc)` 一致 ✓
- `agent.remove_mcp_server(name) -> dict`(Task 5)→ Task 6 一致 ✓
- `agent.set_tool_permission(tool, tier) -> dict`(Task 5)→ Task 6 `agent.set_tool_permission(tool, tier)` 一致 ✓
- `agent._auto_tier(annotations) -> str | None`(Task 5)→ Task 5 内 `set_tier` 仅当非 None ✓
- `engine.policy` property(Task 4)→ Task 4 `h._engine.policy` 一致 ✓
- `ReActAgent.__init__(..., permission_policy=None)`(Task 4)→ Task 4 测试 + Task 5 测试 + server.py create_agent 传参一致 ✓

**修正点(相对 spec §4.5):** spec 说"已有 self._progressive_hook(refresh spec 加的)"——与现实不符(agent 未存,`_refresh_mcp_tools` 不调 recompute)。本 plan **不存 progressive_hook 引用、不调 recompute_eager**——靠 `ProgressiveToolHook.before_invoke` 每请求自动重算 eager(已落地机制)。但补了一个 spec 未提的真实 gap:**ProgressiveToolHook 持 PermissionPolicy 而非 PermissionsConfig**(Task 4),否则运行时 `set_tier` 改的 `_tools` progressive 看不到(`_force_protected_eager` 读 config.tools 静态),审批级工具仍留 deferred 被 `invoke_tool` 绕过审批门。Task 1 加 `tools`/`global_default` property + Task 4 接线传 policy 修复此链路。已在"关键修正"段 + Task 4 体现。✓

**修正点 2:** spec §4.1 说 `add_server` 返回"工具列表(含 annotations)"未明确类型。本 plan 明确返回 `list[McpTool]`(非 `McpToolCard`),因 agent 要 register 的是 `McpTool`(含 client 引用),McpManager 内部已造 McpTool 存进 `_tools`,直接返回避免重复构造/钻私有取 client。annotations 在 `tool.card.annotations` 上。Task 3 体现。✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-28-mcp-runtime-extension.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派 fresh subagent,任务间 review,快速迭代。

**2. Inline Execution** — 本会话内用 executing-plans 批量执行 + 检查点 review。

哪个?
