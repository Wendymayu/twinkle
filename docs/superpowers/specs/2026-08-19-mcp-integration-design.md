# MCP 工具接入设计(Phase 15)

> 日期:2026-08-19
> 状态:设计已定稿,待写实现计划
> 对齐:`roadmap.md` Phase 15(里程碑 M19);参考实现 `jiuwenswarm`(MCP 在 `openjiuwen` SDK 底层 + `jiuwenclaw` 应用层)

## 1. 目标与范围

### 目标
让 Twinkle 能挂载标准 MCP(Model Context Protocol)server 暴露的 **tools**,作为 builtin 工具生态的扩展。agent 能像调 builtin 工具一样调 MCP server 的工具,且 MCP 工具受现有 Phase 4 权限策略统一管控。

### 范围(做)
- **传输**:`stdio`(本地子进程)+ `streamable-http`(远端/本机 HTTP,MCP 官方当前推荐的 HTTP 传输)。两者是业界最重要、最通用的两种。
- **能力面**:只接 **tools**(MCP 三能力面之一)。`list_tools` 拉取 → `call_tool` 调用 → 结果取 text。
- **生命周期**:进程级 `McpManager` 单例,**eager 启动连**(启动时连全部 config server + 拉工具),进程退出统一 `release`。
- **配置**:走 `config.yaml` 的 `mcp:` 块(对齐 Twinkle 单一配置源约定;**不**做 jiuwenswarm 的 `.mcp.json` 导入 / 落盘 `tools/*.json` / RPC 动态注册——那是给多租户 SaaS 前端的,单用户本地不需要)。
- **权限**:对齐 jiuwenswarm——MCP 工具命名 `{server}.{tool}` 进 `permissions.tools` 按名配 tier,未配走 `global_default`;外加 stdio 危险参数基本拦截。
- **依赖**:官方 `mcp` Python SDK(新增 `[mcp]` extra)。

### 范围(不做 / deferred)
- `sse` 传输(官方已 deprecated,`streamable-http` 是其继任;不做历史兼容)
- `playwright` / `openapi` 两种传输(偏门,生态依赖重)
- **resources** / **prompts**(MCP 另两个能力面):v1 砍。Twinkle 已有 `file_tools`/`memory` 读本地数据,resources 的"被动数据注入"场景重叠度低;接口在 client 层可后续加 `list_resources`/`read_resource` 扩展,不破坏现有设计
- 动态 RPC 注册 + 落盘(无前端 MCP 管理界面,单用户用 config 静态加载足够)
- owner-task 串行化那套复杂并发治理(jiuwenswarm SSE 用的 anyio 跨 task 治理;Twinkle 用简单 `AsyncExitStack` 即可)
- 多租户安全全套(SSRF 拦截、host 白名单、command 仅限 node/python)——单机本地放宽
- 子 agent / team member 持有 MCP 工具(重资源/危险面不暴露给子 agent;主 agent 才注入)

### 验收标准
1. 在 `config.yaml` 的 `mcp.servers` 配一个 MCP server(stdio 或 streamable-http),agent 能像调 builtin 工具一样调其工具,结果正确回灌进 ReAct 循环。
2. MCP server 连不上时不阻断 AgentServer 启动(该 server 工具不注册 + warning 日志)。
3. `permissions.tools` 里给 MCP 工具配 `require-approval` 时触发 ASK;未配时走 `global_default`。
4. 进程退出时所有 MCP client 正常 disconnect(stdio 子进程不泄漏)。
5. streamable-http server 传输层瞬时错误(如 `connection closed`)自动重连,重连耗尽才抛错。
6. 真实验收:接用户本机的 streamable-http MCP server(地址由用户提供,填入 config 验收),agent 调其工具跑通。

## 2. 架构概览

```
config.yaml (mcp.servers)
        │
        ▼  startup()  [server.py main(), create_agent 之前]
McpManager 单例 (进程级,对齐 get_memory_manager 形态)
  ├─ StdioMcpClient ──stdio──> MCP server (子进程)
  └─ StreamableHttpMcpClient ──http──> MCP server
        │ eager: connect + list_tools
        ▼  存单例工具表: {server.tool: McpTool}
        │
        │  register_into(tm)  [server.py create_agent(), tool_manager() 之后]
        ▼
主 agent 的 ToolManager ──register──> McpTool (实现 Tool 协议)
        │
        ▼  LLM 选 {server}.{tool}
ToolManager.execute(name, args) ──> McpTool.invoke(args)
        │
        ▼  client.call_tool(name, args) ──> MCP server
extract_text_content(CallToolResult) ──> str 回灌进 ReAct
```

**零改动面**:`ToolManager`(`register`/`schemas`/`execute`)、`ReActAgent`、`@tool`/`LocalFunction` 全不动。`McpTool` 实现 `Tool` 协议(`card: ToolCard` + `async invoke(args: dict) -> str`),`tm.register(mcp_tool)` 即接入。`tools/local_function.py` 注释早预留:"future MCP tools would be a sibling implementation of the same Tool interface"。

**隔离面**:子 agent(`tools/builtin/subagent/executor.py` 自建 `ToolManager()` + `EXCLUDED_TOOLS` 裁剪)和 team member(`team/manager.py` 自建 `ToolManager()` + 白名单)都**不调** `register_into`,天然无 MCP 工具——重资源/危险面不暴露给子 agent。

## 3. 组件 — 新增 `twinkle/agentserver/mcp/` 包

### 3.1 `client.py` — MCP 传输客户端
```python
class McpClient(ABC):
    """MCP 传输客户端抽象。带 timeout 的 connect/disconnect/list_tools/call_tool。"""
    __client_name__: str  # "stdio" | "streamable-http"
    def __init__(self, config: McpServerConfig, connect_timeout, call_timeout): ...
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def list_tools(self) -> list[McpToolCard]: ...   # 返回带 server_name 的卡片
    @abstractmethod
    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str: ...
```

- **`StdioMcpClient(McpClient)`** —— `__client_name__ = "stdio"`
  - `connect()`:`StdioServerParameters(command=, args=, env=)` → `async with stdio_client(params) as (read, write)` → `async with ClientSession(read, write) as session` → 存 `self._session`。用 `AsyncExitStack` 管两层 context manager(对齐 jiuwenswarm,保证子进程必回收)。
  - `disconnect()`:`await self._stack.aclose()`(回收子进程)。
  - `list_tools()`:`await self._session.list_tools()` → 每个 tool 构 `McpToolCard(name=tool.name, server_name=self._name, description=tool.description, parameters=tool.inputSchema)`。
  - `call_tool()`:`await self._session.call_tool(name, arguments=arguments)` → `extract_text_content(result)` 取 text。
  - 用官方 SDK:`from mcp import ClientSession, StdioServerParameters` + `from mcp.client.stdio import stdio_client`。

- **`StreamableHttpMcpClient(McpClient)`** —— `__client_name__ = "streamable-http"`
  - `connect()`:`async with streamable_http_client(url, headers=auth_headers, timeout=...) as (read, write, session_ctx)` → `ClientSession` → 存 `self._session`。`AsyncExitStack` 管连接。
  - `disconnect()`:`await self._stack.aclose()`。
  - `list_tools()`/`call_tool()`:同 stdio,但挂 `@with_reconnect` 装饰器(见 `reconnect.py`)。
  - 用官方 SDK:`from mcp import ClientSession` + `from mcp.client.streamable_http import streamable_http_client`(具体模块名按所装 mcp SDK 版本核对)。
  - `auth_headers` / `auth_query_params` 透传给 SDK(本机 server 通常无需认证,字段保留)。

- 两个 client 的 `connect`/`call_tool` 都包 `asyncio.wait_for(..., timeout)`,`connect` 失败先 `await disconnect()` 清理半连接(对齐 jiuwenswarm)。

### 3.2 `reconnect.py` — 传输层重连
```python
RETRYABLE_TRANSPORT_ERRORS = (
    "session terminated", "ClosedResourceError", "broken pipe",
    "connection closed", "connection reset",
)  # 可重试传输层错误白名单(对齐 jiuwenswarm is_retryable_transport_error)

def with_reconnect(fn):
    """装饰 list_tools/call_tool:撞可重试传输错误 → disconnect+connect 重试,默认 3 次(由 config.reconnect_attempts 覆盖)。耗尽抛原异常。只挂 StreamableHttpMcpClient。"""
```
stdio **不挂**重连(子进程崩了通常是不可恢复的,直接抛 `ToolError`,对齐 jiuwenswarm stdio 行为)。

### 3.3 `tool.py` — MCP 工具适配 Tool 协议
```python
@dataclass
class McpToolCard(ToolCard):
    """带 server_name 的 ToolCard。继承 ToolCard(name/description/parameters)。"""
    server_name: str

class McpTool:
    """MCP server 工具,实现 Tool 协议(card + invoke)。共享底层 McpClient(不重复连)。"""
    def __init__(self, client: McpClient, card: McpToolCard): ...
    @property
    def card(self) -> ToolCard: return self._card
    async def invoke(self, args: dict) -> str:
        # self._tool_name = card.name 去掉 "{server_name}." 前缀(裸名;MCP server 只认自己的工具名,不认 server 前缀)
        # 调 client.call_tool(self._tool_name, args) → str
        # 失败抛 ToolError(对齐 tools/errors.py),不把错误编码进返回 content
        try:
            return await self._client.call_tool(self._tool_name, args)
        except ToolError: raise
        except Exception as e: raise ToolError(f"{self._card.name}: {e}") from e

def extract_text_content(result) -> str:
    """从 MCP CallToolResult.content list 取 text。content[-1] 的 text 字段;无 text 返回空串。对齐 jiuwenswarm extract_mcp_tool_result_content。"""
```
- `card.name` = `{server_name}.{tool_name}`(完整名,进 `permissions.tools` 按名配 tier + 进 LLM tools schema)。
- `invoke` 调 `client.call_tool` 时传**裸 tool_name**(去掉 server 前缀,MCP server 只认自己的工具名)。
- 失败统一抛 `ToolError`(对齐 `tools/errors.py` 的"throw on failure,不编码进 content"),agent loop catch 后转 `[tool error] ...`;transient 传输错误交 `RetryHook`(不砍,对齐现有约定)。

### 3.4 `manager.py` — 进程级单例
```python
class McpManager:
    def __init__(self, config: McpConfig): ...
    async def startup(self) -> None:
        """eager: 遍历 config.servers → check_dangerous_args → _create_client → connect(失败 skip+warn)→ list_tools → 包 McpTool 存 _tools。per-server asyncio.Lock 防并发重复连。"""
    def register_into(self, tm: ToolManager) -> None:
        """把 _tools 里的 McpTool 注册进主 agent 的 ToolManager。disabled/no-started 时 no-op。"""
    async def release(self) -> None:
        """遍历所有 client → disconnect。stdio 的 AsyncExitStack.aclose() 回收子进程。"""
    @property
    def tool_names(self) -> list[str]: ...  # 供测试/日志

def get_mcp_manager(config: McpConfig | None = None) -> McpManager:
    """进程级单例(对齐 get_memory_manager 形态)。首次调用以 config 构造;后续调用返回已存实例。"""
def _set_mcp_manager(mgr: McpManager | None) -> None:
    """测试钩子(对齐 _set_memory_manager)。"""
```
- `startup()` 在 `server.py main()` 里 `create_agent` 之前 `await`(eager 连完才能让 `create_agent` 注册到工具)。
- `register_into()` 在 `server.py create_agent()` 里 `tool_manager()` 之后调(同步,从单例工具表拉快照注册,不影响 `tool_manager()` 同步性)。
- 连接失败的 server:`log.warning("mcp server %s connect failed: %s, skipping", ...)` + 不注册其工具,不阻断启动。

### 3.5 `safety.py` — stdio 危险参数拦截
```python
_DANGEROUS_ARGS = ("-e", "--eval", "-c", "--command", "-i", "-m", "--interactive")  # 拦截代码执行类参数

def check_dangerous_args(args: list[str]) -> None:
    """stdio command 的 args 命中危险参数 → raise ValueError。connect 前调。本机单用户基本兜底,不做多租户全套。"""
```

### 3.6 `__init__.py` — re-export
```python
from twinkle.agentserver.mcp.manager import McpManager, get_mcp_manager, _set_mcp_manager
__all__ = ["McpManager", "get_mcp_manager", "_set_mcp_manager"]
```

## 4. 配置

### 4.1 `config/schema.py` — 新增模型
```python
McpTransport = Literal["stdio", "streamable-http"]

class McpServerConfig(_StrictModel):
    name: str                              # server 名,工具命名前缀 {name}.{tool}
    transport: McpTransport
    # stdio 专用
    command: str = ""                      # stdio 必填
    args: list[str] = []
    env: dict[str, str] = {}
    # streamable-http 专用
    url: str = ""                          # streamable-http 必填
    auth_headers: dict[str, str] = {}
    auth_query_params: dict[str, str] = {}
    timeout: float = 60.0                  # 该 server 单次 call_tool 超时(覆盖 call_timeout)

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "McpServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio server requires 'command'")
        if self.transport == "streamable-http" and not self.url:
            raise ValueError("streamable-http server requires 'url'")
        return self

class McpConfig(_StrictModel):
    enabled: bool = False                  # opt-in,默认关(对齐 OTEL/Evolution/Team)
    servers: list[McpServerConfig] = []
    connect_timeout: float = 30.0         # 连接超时
    call_timeout: float = 60.0            # 调用超时(call_tool 兜底)
    reconnect_attempts: int = 3           # streamable-http 重连次数

# TwinkleConfig 加字段:
class TwinkleConfig(_StrictModel):
    ...
    mcp: McpConfig = McpConfig()
```
`_derive_paths` 无需改(MCP 无独立数据目录,clients 在内存 + stdio 子进程)。

### 4.2 `resources/config.yaml` — 新增 `mcp:` 块
```yaml
mcp:
  enabled: false                            # false = 不加载任何 MCP server(零成本);true 才连 config 里的 servers
  connect_timeout: 30.0                    # 连接超时秒
  call_timeout: 60.0                       # 调用超时秒(call_tool 兜底)
  reconnect_attempts: 3                    # streamable-http 传输层错误重连次数
  servers: []                              # MCP server 列表;示例见下
  # servers 示例:
  # - name: myserver
  #   transport: streamable-http
  #   url: http://127.0.0.1:8080/mcp      # 本机 MCP server
  # - name: fs
  #   transport: stdio
  #   command: npx
  #   args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

### 4.3 `pyproject.toml` — 新增 `[mcp]` extra
```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
obs = [...]
memory = ["sqlite-vec", "jieba"]
mcp = ["mcp>=1.26.0"]                     # 官方 MCP Python SDK(jiuwenswarm 装的是 1.29.0)
```
不加 `fastmcp`(那是 server 端框架,Twinkle 只做 client)。

## 5. 数据流

### 5.1 启动(server.py `main()`)
```
ensure_workspace_dir()
store = session_store()
engine = permission_engine()
llm = LLMClient(...)
if settings.mcp.enabled:                                        # ← 新增
    await get_mcp_manager(settings.mcp).startup()               # ← eager 连 + 拉工具,失败 skip+warn
agent = create_agent(store, hooks=[...PermissionHook...], llm=llm)   # ← create_agent 内 register_into
handler = ws_handler(agent)
dreaming_task = start_dreaming(...)
try:                                                            # ← 改:serve 包 try/finally
    async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()
finally:
    if settings.mcp.enabled:
        await get_mcp_manager().release()                       # ← 退出统一 disconnect
```

### 5.2 agent 构建(server.py `create_agent()`)
```python
tools = tool_manager()                                           # 现有:builtin 工具
from twinkle.agentserver.mcp import get_mcp_manager              # ← 新增
get_mcp_manager().register_into(tools)                           # ← 注入 MCP 工具(disabled/no-started no-op)
# ... 后续 SubagentExecutor / WorkflowExecutor / hooks 不变
return ReActAgent(llm, store, tools, hooks=tuple(all_hooks))
```

### 5.3 运行(ReAct 调用)
```
LLM 选 tool_call: name="myserver.search", args={...}
  → ReActAgent → ToolManager.execute("myserver.search", args)
  → McpTool.invoke(args)
  → client.call_tool("search", args)          # 裸 tool 名(去 server 前缀)
  → MCP server(本机 streamable-http)
  → CallToolResult.content
  → extract_text_content → str
  → 回灌进 ReAct 作 tool_result
```

### 5.4 退出
`main()` 的 `finally` → `get_mcp_manager().release()` → 遍历 client `disconnect()` → stdio `AsyncExitStack.aclose()` 回收子进程。

## 6. 生命周期与错误处理

| 场景 | 处理 |
|---|---|
| 单 server 连不上 | `log.warning` + skip,不阻断 AgentServer 启动,该 server 工具不注册 |
| streamable-http 传输层瞬时错误 | `@with_reconnect` 自动重连 `reconnect_attempts` 次,耗尽抛原异常 |
| `connect`/`call_tool` 超时 | `asyncio.wait_for(..., connect_timeout/call_timeout/timeout)` 兜底;`connect` 失败先 `await disconnect()` 清理半连接 |
| stdio 子进程回收 | `AsyncExitStack` + `disconnect()` 的 `aclose()`,保证必回收 |
| stdio server 进程崩 | 不重连,直接抛 `ToolError`(对齐 jiuwenswarm stdio) |
| MCP 调用失败(任何) | `McpTool.invoke` 抛 `ToolError`(对齐 `tools/errors.py`:throw on failure,不编码进 content),agent loop catch 转 `[tool error] ...` |
| transient 传输错误 | 交 `RetryHook` 自动重试(不砍 RetryHook,对齐现有约定) |
| stdio 危险参数 | `check_dangerous_args` 在 connect 前拦 `-e`/`-c`/`--eval` 等,`raise ValueError` 阻止连接 |

## 7. 权限(对齐 jiuwenswarm)

- MCP 工具命名 `{server_name}.{tool_name}`,进 `permissions.tools: dict[str, PermissionTier]` 按名配 tier。
- 用户在 `config.yaml` 按名写(带 `.` 的 key 加引号):
  ```yaml
  permissions:
    tools:
      "myserver.search": require-approval    # 该 MCP 工具触发 ASK
      "myserver.read": allow
  ```
- 未配置的 MCP 工具走 `global_default`(默认 `allow`)——和 jiuwenswarm 一致。
- `PermissionHook` 已在 `main()` 的 `create_agent` hooks 里(对主 agent 生效),MCP 工具调用前自动过策略(allow → 执行 / require-approval → ASK 挂起 / deny → 注入 `[PERMISSION_DENIED]`)。**零改动**复用现有权限链。
- stdio `check_dangerous_args` 在 connect 前拦危险参数(配置安全兜底;不做多租户 SSRF/host 白名单——单机本地不需要)。

## 8. 测试策略

> 遵循 CLAUDE.md:`asyncio.run()` + `free_port`/`port_factory`,不用 `pytest-asyncio`。

### 8.1 单元
- **配置**(`test_mcp_config.py`):`McpServerConfig` 校验(stdio 缺 command 报错 / streamable-http 缺 url 报错 / transport 取值域);`McpConfig` 默认值。
- **危险参数拦截**(`test_mcp_safety.py`):`check_dangerous_args` 命中 `-e`/`-c`/`--eval` 等抛 `ValueError`,正常 args 放行。纯函数,易测。
- **`extract_text_content`**(`test_mcp_tool.py`):多 content / 有 text / 无 text / 空 content 的取值。
- **`McpTool.invoke`**(`test_mcp_tool.py`):mock `McpClient.call_tool` 返回固定 text → `invoke` 返回 str;mock 抛异常 → `invoke` 抛 `ToolError`(不编码进 content)。

### 8.2 client 生命周期(mock mcp SDK)
- **`StdioMcpClient`**(`test_mcp_client.py`):mock `mcp.client.stdio.stdio_client` + `mcp.ClientSession`,验证 `connect` → `list_tools`(构 `McpToolCard` 带 server_name)→ `call_tool` → `disconnect`(`AsyncExitStack.aclose` 被调);`connect` 失败时清理半连接。
- **`StreamableHttpMcpClient` 重连**:mock `call_tool` 首次抛 `ClosedResourceError` → `@with_reconnect` 触发 disconnect+connect 重试 → 第 2 次成功;重连 `reconnect_attempts` 次耗尽抛原异常。

### 8.3 manager
- **`McpManager`**(`test_mcp_manager.py`):`startup` eager 连(mock client:成功连 + 拉工具 + 存 `_tools`);连失败的 server skip+warn 不阻断、其工具不注册;`register_into(tm)` 把 `_tools` 注册进 `ToolManager`(验证 `tm.list()` 含 MCP 工具、`tm.schemas()` 含 MCP 工具);`release` 调所有 client `disconnect`;per-server lock 防并发重复连。
- **单例**(`test_mcp_manager.py`):`get_mcp_manager` 返回同一实例;`_set_mcp_manager(fake)` 测试钩子换桩(对齐 `_set_memory_manager`)。

### 8.4 集成(agent loop + MCP 工具)
- **e2e mock**(`test_mcp_integration.py`):`_set_mcp_manager` 注入一个返回固定 `McpTool` 的 fake manager → `create_agent` → ScriptedLLM 让模型调 `{server}.{tool}` → 验证 `ToolManager.execute` 路径通 + 结果回灌进 session。
- **权限**(`test_mcp_permissions.py`):`permissions.tools` 配 `"{server}.{tool}": require-approval` → agent 调该 MCP 工具触发 ASK(复用现有 `test_approval_flow` 模式);未配走 `global_default` allow。

### 8.5 真实验收(手动)
接用户本机 streamable-http MCP server:用户把 server 地址填入 `config.yaml` 的 `mcp.servers`,`enabled: true`,起 AgentServer,agent 调该 server 工具跑通。地址由用户提供(运行时填入,非设计占位符)。

## 9. 文件清单

### 新增
- `twinkle/agentserver/mcp/__init__.py` — re-export + 单例 + 测试钩子
- `twinkle/agentserver/mcp/client.py` — `McpClient` + `StdioMcpClient` + `StreamableHttpMcpClient`
- `twinkle/agentserver/mcp/reconnect.py` — `with_reconnect` + 可重试错误白名单
- `twinkle/agentserver/mcp/tool.py` — `McpToolCard` + `McpTool` + `extract_text_content`
- `twinkle/agentserver/mcp/manager.py` — `McpManager` + `get_mcp_manager` + `_set_mcp_manager`
- `twinkle/agentserver/mcp/safety.py` — `check_dangerous_args`
- 测试:`tests/test_mcp_config.py` / `test_mcp_safety.py` / `test_mcp_tool.py` / `test_mcp_client.py` / `test_mcp_manager.py` / `test_mcp_integration.py` / `test_mcp_permissions.py`

### 修改
- `twinkle/config/schema.py` — 加 `McpServerConfig` + `McpConfig` + `TwinkleConfig.mcp`
- `twinkle/resources/config.yaml` — 加 `mcp:` 块
- `pyproject.toml` — 加 `[mcp]` extra(`mcp>=1.26.0`)
- `twinkle/agentserver/server.py` — `create_agent` 加 `register_into`;`main` 加 `startup` + `try/finally release`
- `docs/architecture.md` / `roadmap.md` — Phase 15 标落地 + MCP 包说明

## 10. 与 jiuwenswarm 的对齐与差异

### 对齐
- 官方 `mcp` Python SDK(非自研协议层)
- eager 连接 + 进程退出 release
- `MCPTool` 实现 Tool 接口(card + invoke),`call_tool` 结果取 text
- 工具命名 `{server}.{tool}`,`permissions.tools` 按名配 tier
- streamable-http 可重试传输错误自动重连(`@with_reconnect` + 错误白名单)
- `connect`/`call_tool` `asyncio.wait_for` 超时兜底
- stdio `AsyncExitStack` 子进程回收

### 差异(学习型单机定位,有意砍/改)
- **砍传输**:`sse`(deprecated)/`playwright`/`openapi`——只 stdio + streamable-http
- **砍能力面**:resources / prompts——只 tools
- **砍配置来源**:`.mcp.json` 导入 / 落盘 `tools/*.json` / RPC 动态注册——只 `config.yaml` 静态加载
- **砍并发治理**:owner-task 串行化(`_owner_loop`/`_submit`/`_reconnect_future`)——用简单 `AsyncExitStack`
- **砍安全全套**:SSRF 拦截 / host 白名单 / command 仅限 node-python——只 stdio 危险参数基本拦截
- **砍 lazy load / progressive exposure**(`tools_search`/`invoke_tool` meta 工具)——eager 全量注册进 ToolManager(单用户工具量小,不需要渐进可见)
- **砍 StatusCode 枚举**(jiuwenswarm ~250 项)——用 Twinkle 现有 `ToolError`(对齐 `tools/errors.py` 的设计决策)
- **错误归一**:不另造 MCP 专属错误模型,失败统一抛 `ToolError`,transient 交现有 `RetryHook`

## 11. 实现顺序提示(供 writing-plans 展开)

1. 配置模型 + config.yaml + pyproject extra(可独立测)
2. `safety.py` + `extract_text_content`(纯函数,可独立测)
3. `client.py`(StdioMcpClient → StreamableHttpMcpClient,mock SDK 测)
4. `reconnect.py`(`with_reconnect`,挂 StreamableHttpMcpClient 测)
5. `tool.py`(`McpTool` + `McpToolCard`,测 invoke + ToolError)
6. `manager.py`(`McpManager` + 单例 + 测试钩子,测 startup/register_into/release)
7. `server.py` 接入(`create_agent` + `main` startup/release)
8. 集成 + 权限测试
9. 真实验收(用户本机 server)
10. docs/architecture.md + roadmap.md 标落地
