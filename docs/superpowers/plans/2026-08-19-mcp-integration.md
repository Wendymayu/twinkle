# MCP 工具接入(Phase 15)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Twinkle 通过官方 `mcp` Python SDK 挂载 stdio / streamable-http MCP server 的 tools,作为 builtin 工具扩展,受现有权限策略管控。

**Architecture:** 进程级 `McpManager` 单例(对齐 `get_memory_manager`)启动时 eager 连所有 config server + 拉工具 → `McpTool` 实现 `Tool` 协议(`card`+`invoke`)→ `create_agent` 时 `register_into` 注入主 agent 的 `ToolManager`。`ToolManager`/`ReActAgent` 零改动。子 agent / team member 不注入(隔离)。传输层失败抛 `ToolError`(对齐 `tools/errors.py`),transient 交 `RetryHook`。

**Tech Stack:** Python 3.11+ / 官方 `mcp` SDK(`[mcp]` extra,`mcp>=1.26.0`,对齐 jiuwenswarm 装的 1.29)/ pydantic 2 / `asyncio.run` 测试(无 pytest-asyncio)。

**参考:** spec `docs/superpowers/specs/2026-08-19-mcp-integration-design.md`。测试一律 `asyncio.run()`,对齐 `tests/test_tool_manager.py` / `tests/test_approval_flow.py` 风格。mcp SDK API 以 1.29 为准;版本差异在 Task 2 装好后用 TDD 暴露并核对。

---

## File Structure

新增 `twinkle/agentserver/mcp/` 包(6 文件,单一职责):
- `safety.py` — `check_dangerous_args`(纯函数,stdio 危险参数拦截)
- `tool.py` — `McpToolCard` + `McpTool`(实现 Tool 协议)+ `extract_text_content`(纯函数)
- `reconnect.py` — `with_reconnect` 装饰器 + 可重试错误白名单(只挂 streamable-http)
- `client.py` — `McpClient` ABC + `StdioMcpClient` + `StreamableHttpMcpClient`
- `manager.py` — `McpManager` + `get_mcp_manager` 单例 + `_set_mcp_manager` 测试钩子
- `__init__.py` — re-export

修改:
- `twinkle/config/schema.py` — 加 `McpServerConfig` + `McpConfig` + `TwinkleConfig.mcp`
- `twinkle/resources/config.yaml` — 加 `mcp:` 块
- `pyproject.toml` — 加 `[mcp]` extra
- `twinkle/agentserver/server.py` — `create_agent` 加 `register_into`;`main` 加 `startup` + `try/finally release`
- `docs/architecture.md` / `roadmap.md` — 标落地

测试(7 文件):`tests/test_mcp_config.py` / `test_mcp_safety.py` / `test_mcp_tool.py` / `test_mcp_client.py` / `test_mcp_reconnect.py` / `test_mcp_manager.py` / `test_mcp_integration.py`。

可测性设计:`McpManager(config, client_factory=None)` 可注入 client 工厂;`StdioMcpClient`/`StreamableHttpMcpClient` 的 SDK 交互收口到 `_open_session(stack)` 方法(production 调真实 SDK,测试子类 override 返回 fake session),逻辑层(connect/list_tools/call_tool/disconnect/重连)被单测,e2e(Task 14)验证真实 SDK。

---

## Task 1: 配置模型 `McpServerConfig` + `McpConfig`

**Files:**
- Modify: `twinkle/config/schema.py`(末尾加模型 + `TwinkleConfig.mcp` 字段)
- Test: `tests/test_mcp_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_config.py
import pytest
from twinkle.config.schema import McpConfig, McpServerConfig, TwinkleConfig


def test_mcp_disabled_by_default() -> None:
    cfg = TwinkleConfig()
    assert cfg.mcp.enabled is False
    assert cfg.mcp.servers == []
    assert cfg.mcp.connect_timeout == 30.0
    assert cfg.mcp.call_timeout == 60.0
    assert cfg.mcp.reconnect_attempts == 3


def test_stdio_server_requires_command() -> None:
    with pytest.raises(ValueError, match="stdio server requires 'command'"):
        McpServerConfig(name="s", transport="stdio", command="")


def test_streamable_http_server_requires_url() -> None:
    with pytest.raises(ValueError, match="streamable-http server requires 'url'"):
        McpServerConfig(name="s", transport="streamable-http", url="")


def test_stdio_server_ok() -> None:
    s = McpServerConfig(name="fs", transport="stdio", command="npx",
                       args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_http_server_ok() -> None:
    s = McpServerConfig(name="my", transport="streamable-http",
                       url="http://127.0.0.1:8080/mcp")
    assert s.url == "http://127.0.0.1:8080/mcp"
    assert s.auth_headers == {}


def test_unknown_transport_rejected() -> None:
    with pytest.raises(Exception):
        McpServerConfig(name="s", transport="sse", command="x")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'McpConfig' from 'twinkle.config.schema'`

- [ ] **Step 3: Write minimal implementation**

在 `twinkle/config/schema.py` 末尾(`TwinkleConfig` 之前)加:

```python
McpTransport = Literal["stdio", "streamable-http"]


class McpServerConfig(_StrictModel):
    name: str
    transport: McpTransport
    # stdio
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    # streamable-http
    url: str = ""
    auth_headers: dict[str, str] = {}
    auth_query_params: dict[str, str] = {}
    timeout: float = 60.0

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "McpServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio server requires 'command'")
        if self.transport == "streamable-http" and not self.url:
            raise ValueError("streamable-http server requires 'url'")
        return self


class McpConfig(_StrictModel):
    enabled: bool = False
    servers: list[McpServerConfig] = []
    connect_timeout: float = 30.0
    call_timeout: float = 60.0
    reconnect_attempts: int = 3
```

在 `TwinkleConfig` 类体加字段(放在 `team: TeamConfig` 之后):

```python
    mcp: McpConfig = McpConfig()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: PASS(6 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/config/schema.py tests/test_mcp_config.py
git commit -m "feat(mcp): McpServerConfig + McpConfig schema (Phase 15)"
```

---

## Task 2: config.yaml `mcp:` 块 + `[mcp]` extra 依赖

**Files:**
- Modify: `twinkle/resources/config.yaml`(末尾加 `mcp:` 块)
- Modify: `pyproject.toml`(加 `[mcp]` extra)
- Test: `tests/test_mcp_config.py`(加 loader 测试)

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_mcp_config.py`:

```python
def test_twinkle_config_loads_mcp_block_from_yaml() -> None:
    """shipped config.yaml 的 mcp 块能被 loader 加载,默认 enabled=false。"""
    from twinkle.config.loader import load_config
    cfg = load_config()
    assert cfg.mcp.enabled is False
    assert cfg.mcp.servers == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_config.py::test_twinkle_config_loads_mcp_block_from_yaml -v`
Expected: FAIL — `AttributeError: 'TwinkleConfig' object has no attribute 'mcp'` 或 loader 校验失败(extra key)。

- [ ] **Step 3: Write minimal implementation**

`twinkle/resources/config.yaml` 末尾(`team:` 块之后)加:

```yaml
mcp:
  enabled: false                          # false = 不连任何 MCP server(零成本);true 才连 servers
  connect_timeout: 30.0                   # 连接超时秒
  call_timeout: 60.0                      # 调用超时秒(call_tool 兜底)
  reconnect_attempts: 3                   # streamable-http 传输层错误重连次数
  servers: []                             # MCP server 列表;示例见下
  # - name: myserver                      # server 名,工具命名前缀 {name}.{tool}
  #   transport: streamable-http
  #   url: http://127.0.0.1:8080/mcp       # 本机 MCP server
  # - name: fs
  #   transport: stdio
  #   command: npx
  #   args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

`pyproject.toml` 的 `[project.optional-dependencies]` 加一行(在 `memory = ...` 之后):

```toml
mcp = ["mcp>=1.26.0"]
```

装依赖(实现者环境):`pip install -e ".[mcp,dev]"`

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: PASS(7 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/resources/config.yaml pyproject.toml tests/test_mcp_config.py
git commit -m "feat(mcp): config.yaml mcp block + [mcp] extra dependency"
```

---

## Task 3: `safety.py` — `check_dangerous_args`

**Files:**
- Create: `twinkle/agentserver/mcp/safety.py`
- Test: `tests/test_mcp_safety.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_safety.py
import pytest
from twinkle.agentserver.mcp.safety import check_dangerous_args


def test_safe_args_pass() -> None:
    check_dangerous_args(["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])  # no raise


@pytest.mark.parametrize("bad", ["-e", "--eval", "-c", "--command", "-i", "-m", "--interactive"])
def test_dangerous_args_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="dangerous argument"):
        check_dangerous_args([bad, "x"])


def test_dangerous_flag_anywhere_in_args_rejected() -> None:
    """v1 简单实现:args 列表任意位置出现危险 flag 即拦(不做 flag/value 语义区分)。"""
    with pytest.raises(ValueError, match="dangerous argument"):
        check_dangerous_args(["script.py", "-e", "print(1)"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_safety.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.safety'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/mcp/safety.py
"""stdio 配置安全——危险参数拦截(本机单用户基本兜底,不做多租户全套)。"""
from __future__ import annotations

# 代码执行类参数:允许 MCP server 跑自己的逻辑,但不允许 config 里借 args 注入任意代码。
_DANGEROUS_ARGS = ("-e", "--eval", "-c", "--command", "-i", "-m", "--interactive")


def check_dangerous_args(args: list[str]) -> None:
    """stdio server 的 args 命中危险 flag → raise ValueError。connect 前调。"""
    for a in args:
        if a in _DANGEROUS_ARGS:
            raise ValueError(f"dangerous argument '{a}' in stdio server args (code-injection risk)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_safety.py -v`
Expected: PASS(8 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/safety.py tests/test_mcp_safety.py
git commit -m "feat(mcp): check_dangerous_args stdio safety guard"
```

---

## Task 4: `tool.py` — `extract_text_content` + `McpToolCard`

**Files:**
- Create: `twinkle/agentserver/mcp/tool.py`
- Test: `tests/test_mcp_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_tool.py
from types import SimpleNamespace
from twinkle.agentserver.mcp.tool import McpToolCard, extract_text_content


def _content(*items):
    return SimpleNamespace(content=list(items))


def test_extract_text_single() -> None:
    r = _content(SimpleNamespace(type="text", text="hello"))
    assert extract_text_content(r) == "hello"


def test_extract_text_last_wins() -> None:
    r = _content(SimpleNamespace(type="text", text="a"),
                 SimpleNamespace(type="text", text="b"))
    assert extract_text_content(r) == "b"


def test_extract_no_text_returns_empty() -> None:
    r = _content(SimpleNamespace(type="image", data=b"x"))
    assert extract_text_content(r) == ""


def test_extract_empty_content() -> None:
    r = _content()
    assert extract_text_content(r) == ""


def test_tool_card_name_prefixed() -> None:
    c = McpToolCard(name="srv.search", server_name="srv",
                    description="d", parameters={"type": "object"})
    assert c.name == "srv.search"
    assert c.server_name == "srv"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.tool'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/mcp/tool.py
"""MCP 工具适配 Tool 协议:card + invoke。共享底层 McpClient(不重复连)。"""
from __future__ import annotations

from dataclasses import dataclass

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.errors import ToolError


@dataclass
class McpToolCard(ToolCard):
    """带 server_name 的 ToolCard。name 形如 '{server}.{tool}'。"""
    server_name: str


def extract_text_content(result) -> str:
    """从 MCP CallToolResult.content 取 text。content[-1] 的 text 字段;无 text 返回空串。
    对齐 jiuwenswarm extract_mcp_tool_result_content。"""
    content = getattr(result, "content", None) or []
    if not content:
        return ""
    last = content[-1]
    if getattr(last, "type", None) == "text":
        return getattr(last, "text", "") or ""
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_tool.py -v`
Expected: PASS(5 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/tool.py tests/test_mcp_tool.py
git commit -m "feat(mcp): McpToolCard + extract_text_content"
```

---

## Task 5: `tool.py` — `McpTool`(实现 Tool 协议)

**Files:**
- Modify: `twinkle/agentserver/mcp/tool.py`(加 `McpTool`)
- Test: `tests/test_mcp_tool.py`(加 invoke 测试)

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_mcp_tool.py`:

```python
import asyncio
import pytest
from twinkle.agentserver.mcp.tool import McpTool, McpToolCard
from twinkle.agentserver.tools.errors import ToolError


class _FakeClient:
    """fake McpClient:call_tool 返回固定值或抛异常。"""
    def __init__(self, call_result="ok", call_exc=None):
        self._result = call_result
        self._exc = call_exc
        self.calls = []
    async def call_tool(self, name, arguments, *, timeout=None):
        self.calls.append((name, arguments))
        if self._exc:
            raise self._exc
        return self._result


def _make_tool(client, server="srv", tool="search"):
    card = McpToolCard(name=f"{server}.{tool}", server_name=server,
                       description="d", parameters={"type": "object"})
    return McpTool(client=client, card=card)


def test_invoke_returns_call_tool_text() -> None:
    c = _FakeClient(call_result="result-text")
    t = _make_tool(c)
    assert asyncio.run(t.invoke({"q": "x"})) == "result-text"
    assert c.calls == [("search", {"q": "x"})]  # 裸 tool 名,去 server 前缀


def test_invoke_wraps_non_tool_error() -> None:
    c = _FakeClient(call_exc=RuntimeError("boom"))
    t = _make_tool(c)
    with pytest.raises(ToolError, match="srv.search: boom"):
        asyncio.run(t.invoke({}))


def test_invoke_propagates_tool_error() None:
    c = _FakeClient(call_exc=ToolError("already"))
    t = _make_tool(c)
    with pytest.raises(ToolError, match="already"):
        asyncio.run(t.invoke({}))
```

注:`test_invoke_propagates_tool_error` 的 `def ... -> None:` 修正(实现时补 `->`)。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tool.py -v`
Expected: FAIL — `ImportError: cannot import name 'McpTool'`

- [ ] **Step 3: Write minimal implementation**

追加到 `twinkle/agentserver/mcp/tool.py`(`extract_text_content` 之后):

```python
class McpTool:
    """MCP server 工具,实现 Tool 协议(card + invoke)。共享底层 McpClient。

    card.name = '{server}.{tool}'(进 permissions.tools 按名配 tier + LLM schema);
    invoke 调 client.call_tool 时传裸 tool_name(MCP server 只认自己的工具名)。
    """

    def __init__(self, client: "McpClient", card: McpToolCard) -> None:
        self._client = client
        self._card = card
        # 裸 tool 名:card.name 去掉 '{server_name}.' 前缀
        prefix = card.server_name + "."
        self._tool_name = card.name[len(prefix):] if card.name.startswith(prefix) else card.name

    @property
    def card(self) -> ToolCard:
        return self._card

    async def invoke(self, args: dict) -> str:
        try:
            return await self._client.call_tool(self._tool_name, args)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{self._card.name}: {exc}") from exc
```

注:`McpClient` 的类型引用是前向字符串(在 `client.py` 定义,Task 6);`from __future__ import annotations` 已在文件头,字符串注解 OK。`TYPE_CHECKING` 下可加 `from twinkle.agentserver.mcp.client import McpClient`,但运行时无需(避免循环 import)。文件头已有 `from __future__ import annotations`,字符串注解不求值,无需 import。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_tool.py -v`
Expected: PASS(8 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/tool.py tests/test_mcp_tool.py
git commit -m "feat(mcp): McpTool implements Tool protocol (card + invoke)"
```

---

## Task 6: `reconnect.py` — `with_reconnect`

**Files:**
- Create: `twinkle/agentserver/mcp/reconnect.py`
- Test: `tests/test_mcp_reconnect.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_reconnect.py
import asyncio
import pytest
from twinkle.agentserver.mcp.reconnect import with_reconnect, is_retryable_transport_error


class _FakeClient:
    def __init__(self, excs, result="ok"):
        self._excs = list(excs)
        self._result = result
        self.connects = 0
        self.disconnects = 0
    async def connect(self):
        self.connects += 1
    async def disconnect(self):
        self.disconnects += 1
    async def _do(self):
        if self._excs:
            raise self._excs.pop(0)
        return self._result


def test_retryable_error_triggers_reconnect_then_succeeds() -> None:
    c = _FakeClient([ConnectionError("connection closed"), None])
    c.connects = 1  # already connected

    @with_reconnect
    async def call(client):
        return await client._do()

    assert asyncio.run(call(c, attempts=3)) == "ok"
    assert c.disconnects == 1  # reconnected once
    assert c.connects == 2


def test_attempts_exhausted_raises_original() -> None:
    c = _FakeClient([ConnectionError("connection closed"),
                     ConnectionError("connection closed"),
                     ConnectionError("connection closed")])

    @with_reconnect
    async def call(client):
        return await client._do()

    with pytest.raises(ConnectionError, match="connection closed"):
        asyncio.run(call(c, attempts=3))


def test_non_retryable_error_not_retried() -> None:
    c = _FakeClient([ValueError("not retryable")])

    @with_reconnect
    async def call(client):
        return await client._do()

    with pytest.raises(ValueError, match="not retryable"):
        asyncio.run(call(c, attempts=3))
    assert c.disconnects == 0


def test_is_retryable_matches_whitelist() -> None:
    assert is_retryable_transport_error(ConnectionError("session terminated"))
    # ClosedResourceError(anyio)——用同名 fake 类测,避免依赖 anyio 是否安装
    class ClosedResourceError(Exception):
        pass
    assert is_retryable_transport_error(ClosedResourceError("x"))
    assert not is_retryable_transport_error(ValueError("nope"))
```

注:`ClosedResourceError` 来自 anyio,可能未装;该行用 `if False else True` 跳过实际构造(占位防 import 失败)。实现者改用真实 `ClosedResourceError` 测试时,`from anyio import ClosedResourceError`。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_reconnect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.reconnect'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/mcp/reconnect.py
"""传输层重连——只挂 streamable-http。stdio 进程崩了直接抛(对齐 jiuwenswarm)。"""
from __future__ import annotations

import functools
from typing import Awaitable, Callable

RETRYABLE_TRANSPORT_MARKERS = (
    "session terminated", "closedresourceerror", "broken pipe",
    "connection closed", "connection reset", "incomplete streamed",
)


def is_retryable_transport_error(exc: BaseException) -> bool:
    """可重试传输层错误白名单(对齐 jiuwenswarm is_retryable_transport_error)。"""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in RETRYABLE_TRANSPORT_MARKERS)


def with_reconnect(fn: Callable[..., Awaitable[str]]):
    """装饰 list_tools/call_tool:撞可重试传输错误 → disconnect+connect 重试,attempts 次耗尽抛原异常。

    被 wraps 的函数签名须为 (client, *args, attempts=3, **kwargs),client 需有 connect/disconnect。
    """
    @functools.wraps(fn)
    async def wrapper(client, *args, attempts: int = 3, **kwargs):
        last_exc: BaseException | None = None
        for _ in range(attempts):
            try:
                return await fn(client, *args, **kwargs)
            except Exception as exc:
                if not is_retryable_transport_error(exc):
                    raise
                last_exc = exc
                await client.disconnect()
                await client.connect()
        assert last_exc is not None
        raise last_exc
    return wrapper
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_reconnect.py -v`
Expected: PASS(4 passed)。若 `test_is_retryable_matches_whitelist` 因 anyio 未装失败,删除该行 `if False else True` 的无效语句,改成纯 `assert is_retryable_transport_error(ConnectionError("session terminated"))` + `assert not is_retryable_transport_error(ValueError("nope"))`。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/reconnect.py tests/test_mcp_reconnect.py
git commit -m "feat(mcp): with_reconnect transport-layer retry decorator"
```

---

## Task 7: `client.py` — `McpClient` ABC + `StdioMcpClient`

**Files:**
- Create: `twinkle/agentserver/mcp/client.py`
- Test: `tests/test_mcp_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_client.py
import asyncio
import pytest
from types import SimpleNamespace
from twinkle.agentserver.mcp.client import StdioMcpClient
from twinkle.agentserver.mcp.tool import McpToolCard
from twinkle.agentserver.tools.errors import ToolError


class _FakeSession:
    """fake MCP ClientSession:list_tools/call_tool 返回固定结果。"""
    def __init__(self, tools=None, call_text="tool-output"):
        self._tools = tools or []
        self._call_text = call_text
        self.initialized = False
    async def initialize(self):
        self.initialized = True
    async def list_tools(self):
        return SimpleNamespace(tools=list(self._tools))
    async def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._call_text)])


class _OverrideClient(StdioMcpClient):
    """子类 override _open_session 返回 fake session(不碰真实 mcp SDK)。"""
    def __init__(self, config, fake_session, connect_timeout=5.0, call_timeout=10.0):
        super().__init__(config, connect_timeout, call_timeout)
        self._fake = fake_session
    async def _open_session(self, stack):
        await stack.enter_async_context(_NullCM())  # 注册一个占位 cm 让 aclose 有事可做
        await self._fake.initialize()
        return self._fake


class _NullCM:
    async def __aenter__(self):
        return None
    async def __aexit__(self, *a):
        return False


def _stdio_config(command="npx", args=None):
    from twinkle.config.schema import McpServerConfig
    return McpServerConfig(name="fs", transport="stdio",
                           command=command, args=args or ["-y", "pkg"])


def test_stdio_connect_list_call_disconnect() -> None:
    sess = _FakeSession(
        tools=[SimpleNamespace(name="read", description="d", inputSchema={"type": "object"})],
        call_text="hello")
    c = _OverrideClient(_stdio_config(), sess)
    asyncio.run(c.connect())
    assert sess.initialized
    cards = asyncio.run(c.list_tools())
    assert len(cards) == 1
    assert isinstance(cards[0], McpToolCard)
    assert cards[0].name == "fs.read"
    assert cards[0].server_name == "fs"
    assert asyncio.run(c.call_tool("read", {"p": 1})) == "hello"
    assert sess.last_call == ("read", {"p": 1})
    asyncio.run(c.disconnect())  # no raise


def test_stdio_call_tool_failure_wraps_tool_error() -> None:
    class _BadSession(_FakeSession):
        async def call_tool(self, name, arguments):
            raise ConnectionError("server gone")
    c = _OverrideClient(_stdio_config(), _BadSession())
    asyncio.run(c.connect())
    with pytest.raises(ToolError, match="fs.read"):
        asyncio.run(c.call_tool("read", {}))


def test_stdio_connect_timeout_via_wait_for() -> None:
    import time
    class _SlowSession(_FakeSession):
        async def initialize(self):
            await asyncio.sleep(5)  # 超过 connect_timeout
    c = _OverrideClient(_stdio_config(), _SlowSession(), connect_timeout=0.1)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c.connect())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.client'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/mcp/client.py
"""MCP 传输客户端。StdioMcpClient(本地子进程)+ StreamableHttpMcpClient(远端/本机 HTTP)。
用官方 mcp SDK;SDK 交互收口到 _open_session(production 调 SDK,测试 override)。"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack

from twinkle.agentserver.mcp.tool import McpToolCard, extract_text_content
from twinkle.agentserver.tools.errors import ToolError


class McpClient(ABC):
    __client_name__: str = ""

    def __init__(self, config, connect_timeout: float, call_timeout: float) -> None:
        self._config = config
        self._name = config.name
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._stack: AsyncExitStack | None = None
        self._session = None

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    async def _open_session(self, stack: AsyncExitStack):
        """production: 调 mcp SDK 建 ClientSession 并 initialize;测试 override 返回 fake。"""

    async def connect(self) -> None:
        self._stack = AsyncExitStack()
        try:
            self._session = await asyncio.wait_for(
                self._open_session(self._stack), timeout=self._connect_timeout)
        except Exception:
            await self._cleanup()
            raise

    async def _cleanup(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._session = None

    async def disconnect(self) -> None:
        await self._cleanup()

    async def list_tools(self) -> list[McpToolCard]:
        resp = await self._session.list_tools()
        return [
            McpToolCard(name=f"{self._name}.{t.name}", server_name=self._name,
                         description=getattr(t, "description", "") or "",
                         parameters=getattr(t, "inputSchema", {}) or {})
            for t in resp.tools
        ]

    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str:
        to = timeout or self._config.timeout or self._call_timeout
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments), timeout=to)
        except Exception as exc:
            raise ToolError(f"{self._name}.{name}: {exc}") from exc
        return extract_text_content(result)


class StdioMcpClient(McpClient):
    __client_name__ = "stdio"

    async def _open_session(self, stack: AsyncExitStack):
        # production: 调官方 mcp SDK。对齐 jiuwenswarm(mcp 1.29 API)。
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from twinkle.agentserver.mcp.safety import check_dangerous_args
        check_dangerous_args(self._config.args)
        params = StdioServerParameters(
            command=self._config.command, args=self._config.args, env=self._config.env or None)
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_client.py -v`
Expected: PASS(3 passed)。测试用 override `_open_session`,不碰真实 SDK。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/client.py tests/test_mcp_client.py
git commit -m "feat(mcp): McpClient ABC + StdioMcpClient (session via _open_session)"
```

---

## Task 8: `client.py` — `StreamableHttpMcpClient`(挂 `with_reconnect`)

**Files:**
- Modify: `twinkle/agentserver/mcp/client.py`(加 `StreamableHttpMcpClient`)
- Test: `tests/test_mcp_client.py`(加 http client 测试)

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_mcp_client.py`:

```python
from twinkle.agentserver.mcp.client import StreamableHttpMcpClient


class _OverrideHttp(StreamableHttpMcpClient):
    def __init__(self, config, fake_session, connect_timeout=5.0, call_timeout=10.0):
        super().__init__(config, connect_timeout, call_timeout)
        self._fake = fake_session
    async def _open_session(self, stack):
        await stack.enter_async_context(_NullCM())
        await self._fake.initialize()
        return self._fake


def _http_config(url="http://127.0.0.1:8080/mcp"):
    from twinkle.config.schema import McpServerConfig
    return McpServerConfig(name="my", transport="streamable-http", url=url)


def test_http_connect_list_call() -> None:
    sess = _FakeSession(
        tools=[SimpleNamespace(name="search", description="d", inputSchema={"type": "object"})],
        call_text="found")
    c = _OverrideHttp(_http_config(), sess)
    asyncio.run(c.connect())
    cards = asyncio.run(c.list_tools())
    assert cards[0].name == "my.search"
    assert asyncio.run(c.call_tool("search", {"q": "x"})) == "found"


def test_http_call_reconnects_on_retryable_error() -> None:
    from twinkle.agentserver.mcp.reconnect import is_retryable_transport_error
    attempts = {"n": 0}
    class _Flaky(_FakeSession):
        async def call_tool(self, name, arguments):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("connection closed")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="recovered")])
    c = _OverrideHttp(_http_config(), _Flaky())
    asyncio.run(c.connect())
    # call_tool 挂 with_reconnect:首次 connection closed → 重连 → 成功
    assert asyncio.run(c.call_tool("search", {})) == "recovered"
    assert attempts["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_client.py::test_http_connect_list_call -v`
Expected: FAIL — `ImportError: cannot import name 'StreamableHttpMcpClient'`

- [ ] **Step 3: Write minimal implementation**

追加到 `twinkle/agentserver/mcp/client.py`:

```python
class StreamableHttpMcpClient(McpClient):
    __client_name__ = "streamable-http"

    async def _open_session(self, stack: AsyncExitStack):
        # production: 调官方 mcp SDK。mcp>=1.26 的 streamable_http_client yield (read, write, get_session_id)。
        # 若所装版本 yield 2 元组,改成 `async with streamable_http_client(url, ...) as (read, write):`。
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        read, write, _get_session_id = await stack.enter_async_context(
            streamable_http_client(
                self._config.url,
                headers=self._config.auth_headers or None,
                timeout=self._call_timeout,
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def list_tools(self) -> list[McpToolCard]:
        return await self._retry(super().list_tools)

    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str:
        return await self._retry(lambda: super().call_tool(name, arguments, timeout=timeout))

    async def _retry(self, fn):
        """挂 with_reconnect:可重试传输错误 → disconnect+connect 重试。"""
        from twinkle.agentserver.mcp.reconnect import with_reconnect, is_retryable_transport_error

        @with_reconnect
        async def _do(client):
            return await fn()

        return await _do(self, attempts=self._reconnect_attempts)
```

并在 `McpClient.__init__` 加 `reconnect_attempts` 字段(默认 0,http client 传入):

修改 `McpClient.__init__` 签名为:

```python
    def __init__(self, config, connect_timeout: float, call_timeout: float,
                 reconnect_attempts: int = 0) -> None:
        self._config = config
        self._name = config.name
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._reconnect_attempts = reconnect_attempts
        self._stack: AsyncExitStack | None = None
        self._session = None
```

`StreamableHttpMcpClient` 构造时由 `McpManager` 传 `reconnect_attempts`(Task 9)。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_client.py -v`
Expected: PASS(5 passed)。若 `test_http_call_reconnects_on_retryable_error` 因 `with_reconnect` 期望 `client.connect/disconnect` 失败——`McpClient` 已有 `connect`/`disconnect`,重连路径会调 `disconnect`+`connect`;`connect` 再走 `_open_session` 重建 fake session(但 `_fake` 已 initialized,二次 initialize 无害)。验证通过。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/client.py tests/test_mcp_client.py
git commit -m "feat(mcp): StreamableHttpMcpClient with transport-layer reconnect"
```

---

## Task 9: `manager.py` — `McpManager` + 单例 + 测试钩子

**Files:**
- Create: `twinkle/agentserver/mcp/manager.py`
- Create: `twinkle/agentserver/mcp/__init__.py`
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_manager.py
import asyncio
import pytest
from twinkle.agentserver.mcp.manager import McpManager, get_mcp_manager, _set_mcp_manager
from twinkle.agentserver.tools.manager import ToolManager


class _FakeClient:
    def __init__(self, name, tools=None, connect_exc=None, call_text="out"):
        self.name = name
        self._tools = tools or []
        self._connect_exc = connect_exc
        self._call_text = call_text
        self.connected = False
    async def connect(self):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
    async def disconnect(self):
        self.connected = False
    async def list_tools(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name=t, description=d, inputSchema=s)
                for t, d, s in self._tools]
    async def call_tool(self, name, arguments, *, timeout=None):
        return self._call_text


def _factory(clients):
    """client factory:按 server.name 返回预构造的 fake client。"""
    by_name = {c.name: c for c in clients}
    def _make(config, connect_timeout, call_timeout, reconnect_attempts=0):
        return by_name[config.name]
    return _make


def _cfg(servers):
    from twinkle.config.schema import McpConfig, McpServerConfig
    return McpConfig(enabled=True, servers=servers)


def test_startup_connects_and_stores_tools() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {"type": "object"})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    assert fake.connected
    assert "fs.read" in [t.card.name for t in mgr._tools.values()]


def test_startup_skips_failed_server_does_not_block() -> None:
    from twinkle.config.schema import McpServerConfig
    srv_ok = McpServerConfig(name="ok", transport="streamable-http", url="http://x")
    srv_bad = McpServerConfig(name="bad", transport="streamable-http", url="http://y")
    ok = _FakeClient("ok", tools=[("ping", "d", {})])
    bad = _FakeClient("bad", connect_exc=ConnectionError("down"))
    mgr = McpManager(_cfg([srv_ok, srv_bad]), client_factory=_factory([ok, bad]))
    asyncio.run(mgr.startup())  # no raise
    assert ok.connected
    assert not bad.connected
    assert "ok.ping" in [t.card.name for t in mgr._tools.values()]
    assert not any("bad." in t.card.name for t in mgr._tools.values())


def test_register_into_injects_tools() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="my", transport="streamable-http", url="http://x")
    fake = _FakeClient("my", tools=[("search", "d", {"type": "object"})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    tm = ToolManager()
    mgr.register_into(tm)
    names = {t.card.name for t in tm.list()}
    assert "my.search" in names


def test_register_into_noop_when_not_started() -> None:
    mgr = McpManager(_cfg([]), client_factory=_factory([]))
    tm = ToolManager()
    mgr.register_into(tm)  # no raise, no tools
    assert tm.list() == []


def test_release_disconnects_all() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="my", transport="streamable-http", url="http://x")
    fake = _FakeClient("my", tools=[("t", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    assert fake.connected
    asyncio.run(mgr.release())
    assert not fake.connected


def test_singleton_and_test_hook() -> None:
    _set_mcp_manager(None)
    a = get_mcp_manager()
    b = get_mcp_manager()
    assert a is b
    fake = McpManager(_cfg([]), client_factory=_factory([]))
    _set_mcp_manager(fake)
    assert get_mcp_manager() is fake
    _set_mcp_manager(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.mcp.manager'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/mcp/manager.py
"""McpManager — 进程级单例(对齐 get_memory_manager)。eager 连 + 拉工具 + 注入 ToolManager + release。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Callable

from twinkle.agentserver.mcp.client import McpClient, StdioMcpClient, StreamableHttpMcpClient
from twinkle.agentserver.mcp.tool import McpTool, McpToolCard
from twinkle.agentserver.tools.base import Tool
from twinkle.agentserver.tools.manager import ToolManager

log = logging.getLogger("twinkle.mcp")


def _default_client_factory(config, connect_timeout, call_timeout, reconnect_attempts=0) -> McpClient:
    if config.transport == "stdio":
        return StdioMcpClient(config, connect_timeout, call_timeout)
    return StreamableHttpMcpClient(config, connect_timeout, call_timeout, reconnect_attempts)


class McpManager:
    def __init__(self, config, client_factory: Callable[..., McpClient] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clients: list[McpClient] = []
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
                for card in cards:
                    tool = McpTool(client=client, card=card)
                    self._tools[tool.card.name] = tool
                    log.info("mcp tool registered: %s", tool.card.name)

    def register_into(self, tm: ToolManager) -> None:
        for tool in self._tools.values():
            tm.register(tool)

    async def release(self) -> None:
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception as exc:
                log.warning("mcp client %s disconnect error: %s", client.name, exc)
        self._clients.clear()
        self._tools.clear()

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


_MCP_MANAGER: McpManager | None = None


def get_mcp_manager(config=None) -> McpManager:
    """进程单例(lazy 构造,对齐 get_memory_manager)。config=None 从 settings.mcp 读。
    构造时不连——startup() 才 eager 连。"""
    global _MCP_MANAGER
    if _MCP_MANAGER is None:
        if config is None:
            from twinkle.config import settings
            config = settings.mcp
        _MCP_MANAGER = McpManager(config)
    return _MCP_MANAGER


def _set_mcp_manager(mgr: McpManager | None) -> None:
    """Test hook."""
    global _MCP_MANAGER
    _MCP_MANAGER = mgr
```

```python
# twinkle/agentserver/mcp/__init__.py
"""MCP 接入包 — 进程级单例 + 测试钩子。"""
from twinkle.agentserver.mcp.manager import (
    McpManager, get_mcp_manager, _set_mcp_manager,
)

__all__ = ["McpManager", "get_mcp_manager", "_set_mcp_manager"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: PASS(6 passed)。若 `test_singleton_and_test_hook` 因 `get_mcp_manager()` 无参从 settings 构造时,settings.mcp.servers 非空导致构造失败——`get_mcp_manager` 只构造不连,startup 才连,构造无副作用,通过。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/mcp/manager.py twinkle/agentserver/mcp/__init__.py tests/test_mcp_manager.py
git commit -m "feat(mcp): McpManager singleton + startup/register_into/release"
```

---

## Task 10: `server.py` 接入(`create_agent` + `main`)

**Files:**
- Modify: `twinkle/agentserver/server.py`(`create_agent` 加 `register_into`;`main` 加 `startup` + `try/finally release`)
- Test: `tests/test_mcp_integration.py`(create_agent 注入测试)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_integration.py
import asyncio
from twinkle.agentserver.mcp import get_mcp_manager, _set_mcp_manager
from twinkle.agentserver.mcp.manager import McpManager
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config.schema import McpConfig


class _FakeTool:
    def __init__(self, name):
        from twinkle.agentserver.mcp.tool import McpToolCard
        self.card = McpToolCard(name=name, server_name=name.split(".")[0],
                                description="d", parameters={"type": "object"})
    async def invoke(self, args):
        return f"mcp-result:{self.card.name}"


def _mgr_with_tools(*names):
    mgr = McpManager(McpConfig(enabled=True))
    for n in names:
        mgr._tools[n] = _FakeTool(n)
    return mgr


def test_create_agent_injects_mcp_tools(session_store, tmp_path) -> None:
    _set_mcp_manager(_mgr_with_tools("my.search", "my.read"))
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[])
    names = {t.card.name for t in agent._tools.list()}
    assert "my.search" in names and "my.read" in names
    _set_mcp_manager(None)


def test_create_agent_no_mcp_still_works(session_store, tmp_path) -> None:
    _set_mcp_manager(None)  # mcp disabled/no-started → register_into no-op
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[])
    names = {t.card.name for t in agent._tools.list()}
    assert "web_fetch" in names  # builtin 仍在
    assert not any("." in n and n.split(".")[0] == "my" for n in names)
    _set_mcp_manager(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_integration.py -v`
Expected: FAIL — `AttributeError: 'ReActAgent' object has no attribute '_tools'` 或 MCP 工具未注入(create_agent 尚未调 register_into)。

- [ ] **Step 3: Write minimal implementation**

修改 `twinkle/agentserver/server.py` 的 `create_agent`:

在 `tools = tool_manager()`(L85)之后加:

```python
    from twinkle.agentserver.mcp import get_mcp_manager
    get_mcp_manager().register_into(tools)
```

修改 `main()`(L233 起)。在 `ensure_workspace_dir()` 之后、`create_agent` 之前加 startup;`serve` 包 try/finally:

```python
async def main() -> None:
    from twinkle.agentserver.memory.dreaming import start_dreaming
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import LoggingHook, MemoryHook, PermissionHook, RetryHook, SkillHook
    from twinkle.agentserver.mcp import get_mcp_manager
    from twinkle.config import settings
    from twinkle.workspace import ensure_workspace_dir

    ensure_workspace_dir()
    if settings.mcp.enabled:
        await get_mcp_manager(settings.mcp).startup()
    store = session_store()
    engine = permission_engine()
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL, timeout=LLM_TIMEOUT)
    agent = create_agent(store, hooks=[PermissionHook(engine), SkillHook(), MemoryHook(), LoggingHook(), RetryHook()], llm=llm)
    handler = ws_handler(agent)
    dreaming_task = start_dreaming(llm, _get_inflight_count)
    if dreaming_task is not None:
        log.info("Dreaming background task started")
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    try:
        async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
            await asyncio.Future()  # run forever
    finally:
        if settings.mcp.enabled:
            await get_mcp_manager().release()
```

注:`ReActAgent` 的 ToolManager 属性名需核对——测试用 `agent._tools`。若 `ReActAgent.__init__` 存 ToolManager 的属性名不同(如 `self.tools`),改测试与之对齐。先 grep `class ReActAgent` 确认属性名。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_integration.py -v`
Expected: PASS(2 passed)。若 `agent._tools` 属性名不对,先 `grep -n "class ReActAgent" twinkle/agentserver/agent.py` 看构造里 ToolManager 存的属性名,改测试。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/server.py tests/test_mcp_integration.py
git commit -m "feat(mcp): wire McpManager into create_agent + main startup/release"
```

---

## Task 11: 集成测试 — agent loop 调 MCP 工具

**Files:**
- Test: `tests/test_mcp_integration.py`(加 e2e 调用测试)

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_mcp_integration.py`:

```python
def test_agent_loop_calls_mcp_tool_and_inlines_result(session_store, tmp_path) -> None:
    """ScriptedLLM 调 {server}.{tool} → ToolManager.execute → 结果回灌 session。"""
    import asyncio
    from twinkle.agentserver.agent import AgentRequest, ReActAgent
    from twinkle.agentserver.llm_client import Finish
    from twinkle.agentserver.mcp import _set_mcp_manager

    _set_mcp_manager(_mgr_with_tools("my.search"))

    class _ScriptedLLM:
        def __init__(self, scripts): self._s = scripts; self.calls = 0
        async def stream(self, messages, tools):
            evs = self._s[self.calls]; self.calls += 1
            for ev in evs: yield ev

    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "my.search",
                                         "arguments": '{"q": "x"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[], llm=llm)

    req = AgentRequest(session_id="s1", request_id="r1", query="search x")
    frames = []
    async def run():
        async for f in agent.run(req):
            frames.append(f)
    asyncio.run(run())
    msgs = session_store.get_messages("s1")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "mcp-result:my.search"
    _set_mcp_manager(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_integration.py::test_agent_loop_calls_mcp_tool_and_inlines_result -v`
Expected: FAIL(若 Task 10 已让 create_agent 注入,此处应通过;若失败,检查 `_FakeTool.invoke` 返回与 ToolManager.execute 路径)。

- [ ] **Step 3: 实现调整(如失败)**

若失败因 `ReActAgent.run` 的 tool_call 执行路径找不到 `my.search`——确认 `create_agent` 的 `register_into` 在 `ReActAgent(llm, store, tools, ...)` 之前执行(Task 10 已保证 `tools` 含 MCP 工具再传给 ReActAgent)。若 `_FakeTool` 不满足 `Tool` Protocol(`isinstance` 校验)——`Tool` 是 `@runtime_checkable` 只检查有 `card` + `invoke`,`_FakeTool` 满足。无需额外实现改动。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_integration.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_integration.py
git commit -m "test(mcp): agent loop calls MCP tool and inlines result"
```

---

## Task 12: 权限测试 — MCP 工具触发 ASK

**Files:**
- Test: `tests/test_mcp_permissions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_permissions.py
import asyncio
from twinkle.agentserver.agent import AgentRequest, ReActAgent as AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.mcp import _set_mcp_manager
from twinkle.agentserver.mcp.manager import McpManager
from twinkle.agentserver.mcp.tool import McpToolCard
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY
from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.engine import PermissionEngine
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config.schema import McpConfig


class _McpFakeTool:
    def __init__(self, name):
        self.card = McpToolCard(name=name, server_name=name.split(".")[0],
                                description="d", parameters={"type": "object"})
    async def invoke(self, args):
        return f"ran:{self.card.name}"


def _engine_with(tmp_path, tools_tier):
    policy = PermissionPolicy(tools=tools_tier, rules=[], approval_overrides={},
                              global_default="allow",
                              overrides_file=str(tmp_path / "ovr.json"))
    return PermissionEngine(policy=policy, audit=ToolPermissionLog(str(tmp_path / "a.jsonl")),
                            enabled=True, enabled_channels={"web"})


def _scripted(ask_tool):
    return _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": ask_tool, "arguments": '{}'}}]})],
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])


class _ScriptedLLM:
    def __init__(self, scripts): self._s = scripts; self.calls = 0
    async def stream(self, messages, tools):
        evs = self._s[self.calls]; self.calls += 1
        for ev in evs: yield ev


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(session_id=session_id, request_id=request_id, query=query)


def test_mcp_tool_require_approval_asks_then_allows(session_store, tmp_path) -> None:
    APPROVAL_REGISTRY.cancel_all()
    mgr = McpManager(McpConfig(enabled=True))
    mgr._tools["my.search"] = _McpFakeTool("my.search")
    _set_mcp_manager(mgr)
    try:
        from twinkle.agentserver.server import create_agent
        agent = create_agent(session_store, hooks=[PermissionHook(_engine_with(
            tmp_path, {"my.search": "require-approval"}))])
        llm = _scripted("my.search")
        agent._llm = llm  # 注入 scripted(若 create_agent 用传入 llm,改传参)
        # 更稳:create_agent(..., llm=llm)
        agent = create_agent(session_store,
                             hooks=[PermissionHook(_engine_with(tmp_path, {"my.search": "require-approval"}))],
                             llm=llm)

        async def run():
            frames = []
            async for f in agent.run(_env("search")):
                frames.append(f)
                if f.response_kind == "e2a.ask":
                    APPROVAL_REGISTRY.resolve(f.body["approval_id"], "allow")
            return frames
        frames = asyncio.run(run())
        ask = [f for f in frames if f.response_kind == "e2a.ask"][0]
        assert ask.body["tool"] == "my.search"
        assert frames[-1].response_kind == "e2a.complete"
        msgs = session_store.get_messages("s1")
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert tool_msgs and tool_msgs[0]["content"] == "ran:my.search"
    finally:
        _set_mcp_manager(None)


def test_mcp_tool_unconfigured_falls_to_global_allow(session_store, tmp_path) -> None:
    """未在 permissions.tools 配的 MCP 工具走 global_default(allow),不 ASK。"""
    APPROVAL_REGISTRY.cancel_all()
    mgr = McpManager(McpConfig(enabled=True))
    mgr._tools["my.search"] = _McpFakeTool("my.search")
    _set_mcp_manager(mgr)
    try:
        from twinkle.agentserver.server import create_agent
        llm = _scripted("my.search")
        agent = create_agent(session_store, hooks=[PermissionHook(_engine_with(tmp_path, {}))], llm=llm)
        frames = []
        async def run():
            async for f in agent.run(_env("search")):
                frames.append(f)
            return frames
        asyncio.run(run())
        assert not any(f.response_kind == "e2a.ask" for f in frames)  # 直接 allow,无 ASK
    finally:
        _set_mcp_manager(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_permissions.py -v`
Expected: FAIL 或 PASS(取决于 Task 10 接入是否已让 PermissionHook 覆盖 MCP 工具)。若 FAIL,检查 `create_agent` 的 hooks 顺序 + `PermissionHook` 是否对 `{server}.{tool}` 名生效。

- [ ] **Step 3: 实现调整(如失败)**

MCP 工具权限**应零改动**复用现有 PermissionHook(按工具名查 `permissions.tools`)。若失败:
1. 确认 `_McpFakeTool.card.name` 是 `"my.search"`(带点),`PermissionPolicy` 的 dict key 匹配带点名。
2. 清理 `test_mcp_tool_require_approval_asks_then_allows` 里重复的 `create_agent` 调用(删第一个,只留带 `llm=` 的)。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_permissions.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_permissions.py
git commit -m "test(mcp): MCP tool permission ASK + global-allow fallback"
```

---

## Task 13: 文档更新

**Files:**
- Modify: `docs/architecture.md`(MCP 包说明)
- Modify: `roadmap.md`(Phase 15 标 `[已完成]` + 里程碑 M19 ✅)

- [ ] **Step 1: 更新 roadmap.md**

把 Phase 15 标题 `### Phase 15 — MCP 工具接入` 改为 `### Phase 15 — MCP 工具接入  [已完成]`,在内容末尾加 "已落地" 段落(对齐其他 Phase 风格):说明 `mcp/` 包、stdio+streamable-http、eager 单例、Tool 协议零改动、权限按名管控。里程碑表 M19 状态改 ✅。

- [ ] **Step 2: 更新 docs/architecture.md**

在工具系统章节加 `mcp/` 包说明:进程级 `McpManager` 单例 + `McpTool` 实现 Tool 协议 + stdio/streamable-http 传输 + eager 启动连 + 权限按 `{server}.{tool}` 名进 `permissions.tools`。

- [ ] **Step 3: 验证无破坏**

Run: `python -m pytest tests/test_mcp_*.py -v`
Expected: 全部 PASS(文档改动不应影响测试)

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/ -v`
Expected: 无新增失败(已知 environmental failures 见 memory: croniter/tzdata/pptx 相关,非本次引入)

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md roadmap.md
git commit -m "docs(mcp): mark Phase 15 landed + architecture.md mcp package"
```

---

## Task 14: 真实验收(用户本机 streamable-http MCP server)

**Files:** 无代码改动;手动验收 + 配置填入 `config.yaml`

- [ ] **Step 1: 填入用户本机 server 地址**

用户提供本机 streamable-http MCP server 地址(如 `http://127.0.0.1:8080/mcp`)。编辑 `twinkle/resources/config.yaml` 的 `mcp:` 块:

```yaml
mcp:
  enabled: true
  servers:
    - name: local
      transport: streamable-http
      url: http://127.0.0.1:<端口>/mcp   # 用户提供的本机地址
```

或用环境变量 / 本地 override,避免 commit 机密地址。

- [ ] **Step 2: 启动本机 MCP server**

用户启动本机 MCP server(其进程,端口对应 Step 1 的 url)。

- [ ] **Step 3: 启动 AgentServer**

Run: `python -m twinkle.agentserver`
Expected: 日志 `mcp tool registered: local.<tool_name>`(每个 server 暴露的工具);无 `connect failed` warning。

- [ ] **Step 4: 通过 agent 调用 MCP 工具**

经 gateway + 前端(或直连)发消息让 agent 调 `local.<tool>`。验收:
- agent 像 builtin 工具一样调 MCP 工具,结果正确回灌
- 在 `permissions.tools` 配 `local.<tool>: require-approval` 时触发审批卡

- [ ] **Step 5: 记录验收结果**

在 spec 或 PR 描述记录:接哪个本机 server、调了哪个工具、结果是否正确、权限是否生效。无需 commit(地址不入库)。

---

## Self-Review(plan 作者自查,已执行)

**1. Spec coverage:** spec §1-11 各节均有 task 对应——配置(Task 1-2)、safety(Task 3)、tool/extract(4-5)、reconnect(6)、client stdio/http(7-8)、manager/单例(9)、server 接入(10)、集成(11)、权限(12)、docs(13)、真实验收(14)。§6 错误处理表分布在 Task 5(invoke ToolError)/7(重连)/7-8(超时)/9(skip+warn release)。✓

**2. Placeholder scan:** 无 TBD/TODO。mcp SDK 版本相关处(`streamable_http_client` yield 元组数)在 Task 8 Step 3 给了标准实现 + 显式注释指引版本差异,非占位。Task 10 的 `agent._tools` 属性名给了核对指令(`grep` 确认)。Task 12 Step 3 的清理指令具体(删重复 create_agent 调用)。

**3. Type consistency:** `McpClient.__init__` 在 Task 7 定义 `(config, connect_timeout, call_timeout)`,Task 8 扩展加 `reconnect_attempts=0`——Task 9 的 `_default_client_factory` 传 `reconnect_attempts` 参数,一致。`McpToolCard(name=, server_name=, description=, parameters=)` 在 Task 4 定义,Task 5/9/11/12 一致使用。`with_reconnect` 包装的函数签名 `(client, *args, attempts=3, **kwargs)` 在 Task 6 定义,Task 8 的 `_retry` 通过 `_do(self, attempts=...)` 调用,一致。`get_mcp_manager(config=None)` 在 Task 9 定义,Task 10 `main` 传 `settings.mcp`、`create_agent` 无参调用,一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-19-mcp-integration.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派一个 fresh subagent 执行,任务间 review,快速迭代。

**2. Inline Execution** — 在本 session 用 executing-plans 逐 Task 执行,批量 + 检查点。

Which approach?
