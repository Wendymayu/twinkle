# MCP 工具清单"请求边界 + TTL 刷新"实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MCP server 端新增/删除的工具在请求边界被发现并反映到 LLM 工具集，请求内 schemas 仍冻结保 prefix cache。

**Architecture:** McpManager 加 `refresh_all()` 做 TTL 节流重拉 + diff 计算（不持 tm，返回 `_ToolDiff`），agent 在 `_run_react_loop` 取 schemas 快照前调 `refresh_all()` 应用 diff + 触发 `ProgressiveToolHook.recompute_eager()`。方案 B（解耦 McpManager↔ToolManager）。

**Tech Stack:** Python asyncio, pydantic, pytest（无 pytest-asyncio，用 `asyncio.run()`）

**项目规则（必须遵守）:** 所有 git commit 步骤执行前**先问用户确认**（[[no-direct-github-push]]）。每个 Task 末尾的 commit step 执行时暂停，问用户"是否 commit"后再执行 `git commit`。

**Spec:** `docs/superpowers/specs/2026-08-25-mcp-tool-refresh-design.md`

---

## 文件结构

| 文件 | 责任 | 改动 |
|---|---|---|
| `twinkle/config/schema.py` | 配置 schema | 加 `McpConfig.tool_refresh_ttl` |
| `twinkle/resources/config.yaml` | 用户配置 | mcp 段加 `tool_refresh_ttl` |
| `twinkle/agentserver/mcp/manager.py` | MCP 进程单例 | 加 `_McpServerResource`/`_ToolDiff`/`_server_resources`/`refresh_all()`；`startup` 填充 |
| `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py` | progressive 可见性 hook | 加 `recompute_eager(tm, permissions)` |
| `twinkle/agentserver/agent.py` | ReActAgent | `__init__` 存 `_progressive_hook`；加 `_refresh_mcp_tools()`；`_run_react_loop:499` 前调 |
| `tests/test_mcp_manager.py` | McpManager 测试 | 扩 refresh_all 的 3 条 + 结构 |
| `tests/test_progressive_tool_hook.py` | progressive hook 测试 | 加 recompute_eager 测试（若无此文件则新建） |
| `tests/test_agent_loop.py` | agent 测试 | 加 `_refresh_mcp_tools` 测试 |

---

### Task 1: 配置字段 `tool_refresh_ttl`

**Files:**
- Modify: `twinkle/config/schema.py:283-288`（McpConfig）
- Modify: `twinkle/resources/config.yaml`（mcp 段）
- Test: `tests/test_mcp_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_config.py` 末尾加：

```python
def test_mcp_tool_refresh_ttl_default_300() -> None:
    from twinkle.config.schema import McpConfig
    cfg = McpConfig()
    assert cfg.tool_refresh_ttl == 300.0


def test_mcp_tool_refresh_ttl_none_opts_out() -> None:
    from twinkle.config.schema import McpConfig
    cfg = McpConfig(tool_refresh_ttl=None)
    assert cfg.tool_refresh_ttl is None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_config.py::test_mcp_tool_refresh_ttl_default_300 tests/test_mcp_config.py::test_mcp_tool_refresh_ttl_none_opts_out -v`
Expected: FAIL with `ValidationError`（`tool_refresh_ttl` 未知字段，`extra="forbid"`）

- [ ] **Step 3: 改 schema**

`twinkle/config/schema.py` 的 `McpConfig`（line 283-288）加字段：

```python
class McpConfig(_StrictModel):
    enabled: bool = False
    servers: list[McpServerConfig] = []
    connect_timeout: float = 30.0
    call_timeout: float = 60.0
    reconnect_attempts: int = 3
    tool_refresh_ttl: float | None = 300.0   # 请求边界 TTL 刷新秒;None=opt-out 永不刷;默认 300(MCP 设计哲学=server 动态)
```

- [ ] **Step 4: 改 config.yaml**

`twinkle/resources/config.yaml` 的 mcp 段（line 140 起）加：

```yaml
mcp:
  enabled: false                          # false = 不连任何 MCP server(零成本);true 才连 servers
  connect_timeout: 30.0                   # 连接超时秒
  call_timeout: 60.0                      # 调用超时秒(call_tool 兜底)
  reconnect_attempts: 3                  # 可重试传输错误重连次数
  tool_refresh_ttl: 300                  # 请求边界工具清单刷新秒;None=永不刷;默认 300(server 端增删工具 5 分钟内可见)
  servers: []
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: PASS（含两条新测试）

- [ ] **Step 6: Commit（先问用户）**

```bash
git add twinkle/config/schema.py twinkle/resources/config.yaml tests/test_mcp_config.py
```
暂停问用户"是否 commit Task 1（配置字段）"。用户同意后：
```bash
git commit -m "feat(mcp): add tool_refresh_ttl config (default 300s, None=opt-out)"
```

---

### Task 2: McpManager 数据结构 + startup 填充 `_server_resources`

**Files:**
- Modify: `twinkle/agentserver/mcp/manager.py:1-48`（import + dataclass + McpManager.__init__ + startup）
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_manager.py` 末尾加：

```python
def test_startup_populates_server_resources() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {"type": "object"}), ("write", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    res = mgr._server_resources["fs"]
    assert res.name == "fs"
    assert res.client is fake
    assert res.tool_names == {"fs.read", "fs.write"}
    assert res.expiry == 300.0          # McpConfig 默认
    assert isinstance(res.last_update, float)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_manager.py::test_startup_populates_server_resources -v`
Expected: FAIL with `AttributeError: 'McpManager' object has no attribute '_server_resources'`

- [ ] **Step 3: 加 dataclass + 字段 + startup 填充**

`twinkle/agentserver/mcp/manager.py` 顶部 import 改（加 `time` + `Tool` + `dataclass`）：

```python
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from twinkle.agentserver.mcp.client import McpClient, StdioMcpClient, StreamableHttpMcpClient
from twinkle.agentserver.mcp.tool import McpTool
from twinkle.agentserver.tools.base import Tool
from twinkle.agentserver.tools.manager import ToolManager
```

在 `log = ...` 之后、`class McpManager` 之前加两个 dataclass：

```python
@dataclass
class _McpServerResource:
    name: str
    client: McpClient
    tool_names: set[str]
    last_update: float
    expiry: float | None


@dataclass
class _ToolDiff:
    added: list[Tool]
    removed: list[str]
```

`McpManager.__init__`（line 24-29）加 `_server_resources`：

```python
    def __init__(self, config, client_factory: Callable[..., McpClient] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clients: list[McpClient] = []
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._server_resources: dict[str, _McpServerResource] = {}
```

`startup`（line 31-48）在成功连接后填充 `_server_resources`：

```python
    async def startup(self) -> None:
        for srv in self._config.servers:
            lock = self._locks.setdefault(srv.name, asyncio.Lock())
            async with lock:
                try:
                    client = self._client_factory(
                        srv, self._config.connect_timeout, self._config.call_timeout,
                        self._config.reconnect_attempts)
                    await client.connect()
                    cards = await client.list_tools()
                except Exception as exc:
                    log.warning("mcp server %s connect failed: %s, skipping", srv.name, exc)
                    continue
                self._clients.append(client)
                tool_names: set[str] = set()
                for card in cards:
                    tool = McpTool(client=client, card=card)
                    self._tools[tool.card.name] = tool
                    tool_names.add(tool.card.name)
                    log.info("mcp tool registered: %s", tool.card.name)
                self._server_resources[srv.name] = _McpServerResource(
                    name=srv.name, client=client, tool_names=tool_names,
                    last_update=time.time(), expiry=self._config.tool_refresh_ttl,
                )
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: PASS（含新测试 + 现有 6 条不回归）

- [ ] **Step 5: Commit（先问用户）**

```bash
git add twinkle/agentserver/mcp/manager.py tests/test_mcp_manager.py
```
暂停问用户。同意后：
```bash
git commit -m "feat(mcp): add _McpServerResource + _ToolDiff, populate in startup"
```

---

### Task 3: `refresh_all()` — TTL 不刷 / 过期真拉+diff / 失败降级

三个 TDD 循环在一个 Task（refresh_all 的三条路径）。

**Files:**
- Modify: `twinkle/agentserver/mcp/manager.py`（加 `refresh_all` 方法）
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: 写失败测试 A — TTL 内不刷**

在 `tests/test_mcp_manager.py` 加：

```python
def test_refresh_all_within_ttl_no_fetch() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 把 last_update 设成"刚刷过",TTL(300) 内
    mgr._server_resources["fs"].last_update = time.time()
    diffs = asyncio.run(mgr.refresh_all())
    assert diffs == []
    assert fake.list_tools_call_count == 1   # 仅 startup 调过,refresh 没调
```

> 注：`_FakeClient.list_tools` 需加调用计数（见 Step 3 的 _FakeClient 改动）。

- [ ] **Step 2: 写失败测试 B — TTL 过期真拉 + diff**

```python
def test_refresh_all_expired_fetches_and_diffs() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 模拟 server 端工具变了:read 删了,write 新增
    fake._tools = [("write", "d", {})]
    # 让 TTL 过期
    mgr._server_resources["fs"].last_update = time.time() - 301
    diffs = asyncio.run(mgr.refresh_all())
    assert len(diffs) == 1
    assert diffs[0].removed == ["fs.read"]
    assert [t.card.name for t in diffs[0].added] == ["fs.write"]
    assert "fs.write" in mgr._tools
    assert "fs.read" not in mgr._tools
    assert mgr._server_resources["fs"].tool_names == {"fs.write"}
```

- [ ] **Step 3: 写失败测试 C — 失败降级**

```python
def test_refresh_all_failure_degrades_keep_old() None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 让 list_tools 抛异常 + TTL 过期
    fake.list_tools_exc = RuntimeError("server down")
    mgr._server_resources["fs"].last_update = time.time() - 301
    diffs = asyncio.run(mgr.refresh_all())
    assert diffs == []                       # 该 server 无 diff
    assert "fs.read" in mgr._tools           # 旧清单保留
    assert mgr._server_resources["fs"].tool_names == {"fs.read"}  # 未更新
```

- [ ] **Step 4: 跑测试验证失败**

Run: `python -m pytest tests/test_mcp_manager.py::test_refresh_all_within_ttl_no_fetch tests/test_mcp_manager.py::test_refresh_all_expired_fetches_and_diffs tests/test_mcp_manager.py::test_refresh_all_failure_degrades_keep_old -v`
Expected: FAIL（`refresh_all` 不存在 / `_FakeClient` 无 `list_tools_call_count`/`list_tools_exc`）

- [ ] **Step 5: 改 `_FakeClient` 支持测试**

`tests/test_mcp_manager.py` 的 `_FakeClient.__init__` + `list_tools`：

```python
class _FakeClient:
    def __init__(self, name, tools=None, connect_exc=None, call_text="out",
                 list_tools_exc=None):
        self.name = name
        self._tools = tools or []
        self._connect_exc = connect_exc
        self._call_text = call_text
        self.list_tools_exc = list_tools_exc
        self.list_tools_call_count = 0
        self.connected = False
    async def connect(self):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
    async def disconnect(self):
        self.connected = False
    async def list_tools(self):
        self.list_tools_call_count += 1
        if self.list_tools_exc:
            raise self.list_tools_exc
        from twinkle.agentserver.mcp.tool import McpToolCard
        return [McpToolCard(name=f"{self.name}.{t}", server_name=self.name,
                            description=d, parameters=s)
                for t, d, s in self._tools]
    async def call_tool(self, name, arguments, *, timeout=None):
        return self._call_text
```

顶部加 `import time`（test_mcp_manager.py）。

- [ ] **Step 6: 实现 `refresh_all`**

`twinkle/agentserver/mcp/manager.py` 的 `McpManager`，在 `register_into` 之后加：

```python
    async def refresh_all(self, force: bool = False) -> list[_ToolDiff]:
        """请求边界 TTL 刷新:per-server 过期/force 才重拉 list_tools,diff 后返回。
        不持 tm(方案B 解耦):调用方应用 diff。失败降级(沿用旧清单,返回空)。"""
        if not self._server_resources:
            return []
        ttl = self._config.tool_refresh_ttl
        diffs: list[_ToolDiff] = []
        now = time.time()
        for res in self._server_resources.values():
            async with self._locks.setdefault(res.name, asyncio.Lock()):
                need = force or (ttl is not None and now - res.last_update >= ttl)
                if not need:
                    continue
                try:
                    cards = await asyncio.wait_for(
                        res.client.list_tools(), self._config.call_timeout)
                except Exception as exc:
                    log.warning("mcp refresh %s failed, keep old tools: %s", res.name, exc)
                    continue
                new_names = {c.name for c in cards}
                removed = list(res.tool_names - new_names)
                added: list[Tool] = []
                for card in cards:
                    tool = McpTool(client=res.client, card=card)
                    self._tools[card.name] = tool
                    added.append(tool)
                for name in removed:
                    self._tools.pop(name, None)
                res.tool_names = new_names
                res.last_update = time.time()
                diffs.append(_ToolDiff(added=added, removed=removed))
        return diffs
```

- [ ] **Step 7: 跑测试验证通过**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: PASS（3 条新测试 + 现有 7 条不回归）

- [ ] **Step 8: Commit（先问用户）**

```bash
git add twinkle/agentserver/mcp/manager.py tests/test_mcp_manager.py
```
暂停问用户。同意后：
```bash
git commit -m "feat(mcp): refresh_all with TTL throttle + diff + failure degrade"
```

---

### Task 4: `ProgressiveToolHook.recompute_eager()`

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py`（加方法）
- Test: `tests/test_progressive_tool_hook.py`（若无则新建）

- [ ] **Step 1: 写失败测试**

新建或追加到 `tests/test_progressive_tool_hook.py`：

```python
import asyncio
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.local_function import LocalFunction


def _make_tool(name: str) -> LocalFunction:
    async def _fn(args): return "ok"
    return LocalFunction(ToolCard(name=name, description="d", parameters={}), _fn)


def test_recompute_eager_pulls_non_allow_into_eager() -> None:
    tm = ToolManager()
    tm.register(_make_tool("builtin_a"))
    tm.register(_make_tool("mcp.risky"))       # 模拟 refresh 后新增的非 allow 档 MCP 工具
    hook = ProgressiveToolHook(eager_names=["builtin_a"])   # 初始 eager 只有 builtin_a
    assert "mcp.risky" not in hook.eager_names
    permissions = type("P", (), {"global_default": "allow", "tools": {"mcp.risky": "require-approval"}})()
    hook.recompute_eager(tm, permissions)
    assert "mcp.risky" in hook.eager_names     # 非 allow 档被拉回 eager(#1 守卫 intact)
    assert "builtin_a" in hook.eager_names      # 原有保留


def test_recompute_eager_leaves_allow_deferred() -> None:
    tm = ToolManager()
    tm.register(_make_tool("mcp.safe"))
    hook = ProgressiveToolHook(eager_names=[])
    permissions = type("P", (), {"global_default": "allow", "tools": {}})()
    hook.recompute_eager(tm, permissions)
    assert "mcp.safe" not in hook.eager_names   # allow 档保持 deferred
```

> 若 `LocalFunction` 构造签名不同，查 `twinkle/agentserver/tools/local_function.py` 对齐。如不确定，用最小 `_FakeTool`：`class _T: def __init__(s,c): s._c=c; @property \n def card(s): return s._c; async def invoke(s,a): return ""`，构造 `_T(ToolCard(name=..., description="d", parameters={}))`。

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_progressive_tool_hook.py -v`
Expected: FAIL with `AttributeError: 'ProgressiveToolHook' object has no attribute 'recompute_eager'`

- [ ] **Step 3: 实现 `recompute_eager`**

`twinkle/agentserver/hooks/builtin/progressive_tool_hook.py` 的 `ProgressiveToolHook` 类，在 `before_model_call` 之后加：

```python
    def recompute_eager(self, tm, permissions) -> None:
        """refresh 后重算 eager:对新出现的非 allow 档工具拉回 eager(保 #1 守卫)。
        复用 _force_protected_eager:遍历 tm.list(),非 allow 档且不在 eager 的 add。
        局部 import 避免循环(progressive.py import 本类)。"""
        from twinkle.agentserver.tools.progressive import _force_protected_eager
        _force_protected_eager(tm, self.eager_names, permissions)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_progressive_tool_hook.py -v`
Expected: PASS

- [ ] **Step 5: Commit（先问用户）**

```bash
git add twinkle/agentserver/hooks/builtin/progressive_tool_hook.py tests/test_progressive_tool_hook.py
```
暂停问用户。同意后：
```bash
git commit -m "feat(progressive): recompute_eager after MCP refresh (#1 guard intact)"
```

---

### Task 5: agent `_refresh_mcp_tools()` + `__init__` 存 progressive hook

**Files:**
- Modify: `twinkle/agentserver/agent.py:378-395`（__init__ 存 `_progressive_hook`）
- Modify: `twinkle/agentserver/agent.py`（加 `_refresh_mcp_tools` 方法）
- Test: `tests/test_agent_loop.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent_loop.py` 加（若该文件有现成 agent 构造 fixture，复用它；否则用最小构造）：

```python
def test_refresh_mcp_tools_applies_diff(tmp_path, monkeypatch) -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.mcp.manager import _ToolDiff
    from twinkle.agentserver.mcp.tool import McpToolCard, McpTool
    from twinkle.agentserver.tools.base import ToolCard
    from twinkle.agentserver.tools.local_function import LocalFunction

    async def _fn(args): return "ok"
    old_tool = LocalFunction(ToolCard(name="old.x", description="d", parameters={}), _fn)
    new_tool = LocalFunction(ToolCard(name="new.x", description="d", parameters={}), _fn)
    tm = ToolManager()
    tm.register(old_tool)

    agent = ReActAgent(llm=None, store=None, tools=tm, hooks=())

    class _FakeMgr:
        async def refresh_all(self):
            return [_ToolDiff(added=[new_tool], removed=["old.x"])]
    from twinkle.agentserver.mcp import manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda *a, **k: _FakeMgr())

    asyncio.run(agent._refresh_mcp_tools())
    names = {t.card.name for t in tm.list()}
    assert "new.x" in names
    assert "old.x" not in names


def test_refresh_mcp_tools_calls_recompute_when_progressive(tmp_path, monkeypatch) -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook

    calls = []
    tm = ToolManager()
    hook = ProgressiveToolHook(eager_names=["x"])
    hook.recompute_eager = lambda tm, perm: calls.append((tm, perm))   # spy
    agent = ReActAgent(llm=None, store=None, tools=tm, hooks=(hook,))

    class _FakeMgr:
        async def refresh_all(self):
            return []
    from twinkle.agentserver.mcp import manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda *a, **k: _FakeMgr())

    asyncio.run(agent._refresh_mcp_tools())
    assert len(calls) == 1
    assert calls[0][0] is tm
```

> `asyncio`/`monkeypatch` 由 conftest 提供（pytest 内置 monkeypatch fixture）。`llm=None, store=None` 仅构造不调用——`_refresh_mcp_tools` 不碰 llm/store。

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_agent_loop.py::test_refresh_mcp_tools_applies_diff tests/test_agent_loop.py::test_refresh_mcp_tools_calls_recompute_when_progressive -v`
Expected: FAIL with `AttributeError: 'ReActAgent' object has no attribute '_refresh_mcp_tools'` 或 `'_progressive_hook'`

- [ ] **Step 3: `__init__` 存 `_progressive_hook`**

`twinkle/agentserver/agent.py` 顶部 import 加（在现有 hooks import 附近，line 31-39 区块后或内）：

```python
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
```

> 检查无循环 import：`progressive_tool_hook.py` 只 import `base/prompts/progressive_tools`，不 import agent，安全。

`__init__`（line 388-395）末尾加识别：

```python
        self._inbox = inbox
        self._base_sections = base_sections
        self._progressive_hook = next(
            (h for h in hooks if isinstance(h, ProgressiveToolHook)), None)
```

- [ ] **Step 4: 实现 `_refresh_mcp_tools`**

`twinkle/agentserver/agent.py` 的 `ReActAgent`，在 `run` 方法之前（line 410 `# -- Public entry point --` 之前）加：

```python
    async def _refresh_mcp_tools(self) -> None:
        """请求边界刷新 MCP 工具清单:refresh_all → 应用 diff 到 tm → progressive recompute。
        在 _run_react_loop 取 schemas 快照前调,整轮冻结保 prefix cache。"""
        from twinkle.agentserver.mcp import get_mcp_manager
        from twinkle.config import settings
        mgr = get_mcp_manager()
        for d in await mgr.refresh_all():
            for name in d.removed:
                self._tool_manager.unregister(name)
            for tool in d.added:
                self._tool_manager.register(tool)
        if self._progressive_hook is not None:
            self._progressive_hook.recompute_eager(self._tool_manager, settings.permissions)
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_agent_loop.py::test_refresh_mcp_tools_applies_diff tests/test_agent_loop.py::test_refresh_mcp_tools_calls_recompute_when_progressive -v`
Expected: PASS

- [ ] **Step 6: Commit（先问用户）**

```bash
git add twinkle/agentserver/agent.py tests/test_agent_loop.py
```
暂停问用户。同意后：
```bash
git commit -m "feat(agent): _refresh_mcp_tools applies diff + triggers progressive recompute"
```

---

### Task 6: `_run_react_loop` 集成 — 在 schemas 冻结前调 refresh

`_refresh_mcp_tools` 逻辑已在 Task 5 单测覆盖。本 Task 把它接到 `_run_react_loop` 的 schemas 冻结点之前 + 回归守门。spec §7 未要求端到端 run 测试（其 7 条为 TTL 不刷/过期+diff/失败降级/force/progressive 拉回/默认 ttl=300/现有不回归），故不写脆弱的 run 驱动测试（依赖 LLM mock 类型名）。

**Files:**
- Modify: `twinkle/agentserver/agent.py:495-499`

- [ ] **Step 1: 在 schemas 冻结点之前插入 refresh 调用**

`twinkle/agentserver/agent.py` 的 `_run_react_loop`，把（line 495-499 现状，逐字匹配）：

```python
        seq = 0
        full_text = ""
        # 一次冻结 tool schemas:invoke 内不变;team 过滤只依赖 request.mode(before_invoke 时已知)。
        # 对齐 jiuwenswarm:tools 跨步稳定 → system prefix 字节稳定 → provider 自动 prefix cache 命中。
        tool_schemas = self._tool_manager.schemas()
```

改为：

```python
        seq = 0
        full_text = ""
        # 请求边界刷新 MCP 工具清单(应用 diff + progressive recompute),在 schemas 冻结前。
        # 整轮复用冻结的 schemas → prefix cache 命中(见下注释)。
        await self._refresh_mcp_tools()
        # 一次冻结 tool schemas:invoke 内不变;team 过滤只依赖 request.mode(before_invoke 时已知)。
        # 对齐 jiuwenswarm:tools 跨步稳定 → system prefix 字节稳定 → provider 自动 prefix cache 命中。
        tool_schemas = self._tool_manager.schemas()
```

> old_string 必须逐字匹配两行中文注释（全角标点：分号 `；`、冒号 `:`后无空格、`→` 箭头）。`_refresh_mcp_tools` 已在 Task 5 实现。

- [ ] **Step 2: 跑现有 agent 测试验证不回归**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: PASS（现有测试全过；`_refresh_mcp_tools` 在 run 流程被调一次——mcp 未 enabled 时 `refresh_all()` 返回空、无副作用）

> 若现有测试因 `_refresh_mcp_tools` 调 `get_mcp_manager()` 失败：确认局部 import `from twinkle.agentserver.mcp import get_mcp_manager` 正确（manager.py 已导出此函数）。mcp disabled 时单例 `_server_resources` 为空 → `refresh_all` 早返回 `[]` → `for d in []` 不执行；`_progressive_hook` 为 None（progressive disabled）→ 无副作用。

- [ ] **Step 3: 全套回归**

Run: `python -m pytest tests/ -v`
Expected: 全套 PASS。重点确认：
- `tests/test_mcp_manager.py`（refresh_all 3 条 + 现有 7 条）
- `tests/test_progressive_tool_hook.py`（recompute 2 条）
- `tests/test_agent_loop.py`（Task 5 的 2 条 + 现有不回归）
- `tests/test_mcp_integration.py` / `test_mcp_reconnect.py` 不回归

> 若现有 MCP 测试因 mock 的 `list_tools` 不可重入而 fail：Task 3 Step 5 已让 `_FakeClient.list_tools` 可重入（call_count++），现有测试用同一 `_FakeClient` 应仍过。如 fail，按错误调整 mock。

- [ ] **Step 4: Commit（先问用户）**

```bash
git add twinkle/agentserver/agent.py tests/test_agent_loop.py
```
暂停问用户。同意后：
```bash
git commit -m "feat(agent): invoke MCP refresh at request boundary before schemas freeze"
```

---

## Self-Review

**1. Spec coverage（逐条对照 spec §2-§9）:**
- §2 决策① TTL=300 → Task 1 ✓
- §2 决策② 失败降级 → Task 3 测试 C + 实现 `except ... continue` ✓
- §2 决策③ progressive 联动 → Task 4 + Task 5（_refresh_mcp_tools 调 recompute）✓
- §3 方案 B（不持 tm，返回 diff）→ Task 3 `refresh_all` 返回 `list[_ToolDiff]`，Task 5 agent 应用 ✓
- §4.1 McpManager 数据结构 → Task 2 ✓；refresh_all → Task 3 ✓
- §4.2 ProgressiveToolHook.recompute_eager → Task 4 ✓
- §4.3 agent __init__ 存 hook + _run_react_loop 调用 → Task 5 + Task 6 ✓
- §4.4 配置 → Task 1 ✓
- §5 数据流（请求边界→refresh→diff→recompute→schemas 冻结）→ Task 6 插入点 ✓
- §6 错误处理（超时/失败降级）→ Task 3 `asyncio.wait_for` + except ✓
- §7 测试 7 条 → Task 1(2)+Task2(1)+Task3(3)+Task4(2)+Task5(2) 覆盖；Task 6 无新测试(接线+回归守门,spec §7 第7条=现有不回归) ✓
- §8 不做的事 → 未实现，✓
- §9 文件清单 → 全覆盖 ✓

**2. Placeholder scan:** 无 TBD/TODO。Task 4 测试有"若 LocalFunction 构造签名不同"的 fallback——带具体 _FakeTool 代码的对齐指引,非占位。Task 6 无测试代码(接线+回归),不存在签名猜测。✓

**3. Type consistency:**
- `_McpServerResource`（Task 2）字段 `name/client/tool_names/last_update/expiry` — Task 3 `refresh_all` 用 `res.name/res.client/res.tool_names/res.last_update` 一致 ✓
- `_ToolDiff`（Task 2）`added: list[Tool]/removed: list[str]` — Task 3 返回、Task 5 测试 `d.removed/d.added` 一致 ✓
- `refresh_all(force=False) -> list[_ToolDiff]`（Task 3）— Task 5 调用 `await mgr.refresh_all()` 一致 ✓
- `recompute_eager(tm, permissions)`（Task 4）— Task 5 调用 `recompute_eager(self._tool_manager, settings.permissions)` 一致 ✓
- `self._progressive_hook`（Task 5 __init__）— Task 5 `_refresh_mcp_tools` 用、Task 6 不直接用 ✓

**修正点（相对 spec §4.2）：** spec 说"构造时存 tm+permissions 引用"，但 `progressive_tool_hook.py:7-8` 注释明说 hook 故意 init 收 None、走 ctx.agent。plan 改为 `recompute_eager(tm, permissions)` 参数传入 + 局部 import `_force_protected_eager` 避循环。已在 Task 4 体现。✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-25-mcp-tool-refresh.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派 fresh subagent，任务间 review，快速迭代。

**2. Inline Execution** — 本会话内用 executing-plans 批量执行 + 检查点 review。

哪个？
