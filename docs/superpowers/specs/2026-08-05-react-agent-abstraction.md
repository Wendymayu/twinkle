# AgentLoop → ReActAgent 抽象重构

> 日期：2026-08-05 · 状态：草案
> 动机：`AgentLoop` 命名暗示它只是循环，实际上是一个完整的 agent；且 `run_stream(E2AEnvelope)` 让 agent 耦合了传输协议。

---

## 0. 现状问题

```python
# 当前：agent 知道 E2AEnvelope（传输层概念）
class AgentLoop:
    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        session_id = envelope.session_id
        request_id = envelope.request_id
        query = (envelope.params or {}).get("query", "")
        ...
```

三个问题：
1. **命名误导**：`AgentLoop` 听起来像是一个循环控制结构，实际它是一个完整的 agent（持有 LLM、工具、hook、session）
2. **传输耦合**：agent 接收 `E2AEnvelope`，需要自己从 `params` 里掏 `query`。如果将来对接非 E2A 通道（如 CLI、HTTP），agent 也得改
3. **构造分散**：`build_agent_loop()` 构造 agent 后还要 `register_hook()`，分两步

---

## 1. 目标

```python
# 目标：agent 只知道业务输入
class ReActAgent:
    def __init__(self, llm, store, tools, *, hooks, config):
        ...

    async def run(self, request: AgentRequest) -> AsyncIterator[E2AResponse]:
        ...
```

- 命名表达意图：这是一个 ReAct 模式的 agent
- 输入是纯业务对象（`AgentRequest`），不依赖传输协议
- hook 在构造时注入，不暴露 `register_hook`

---

## 2. 新增：`AgentRequest`

```python
# twinkle/agentserver/agent.py（新文件，ReActAgent + AgentRequest）

from dataclasses import dataclass

@dataclass
class AgentRequest:
    """一次 agent 运行的业务输入。无传输层概念。"""
    session_id: str
    request_id: str
    query: str
    channel: str = "web"
```

server.py 负责从 E2AEnvelope 构造 AgentRequest：

```python
# server.py — 传输层职责留在传输层
request = AgentRequest(
    session_id=envelope.session_id or envelope.request_id,
    request_id=envelope.request_id,
    query=(envelope.params or {}).get("query", ""),
    channel=envelope.channel or "web",
)
async for frame in agent.run(request):
    await send(frame)
```

---

## 3. 改动：`AgentLoop` → `ReActAgent`

实际改动很小，因为内部逻辑不变：

```python
# 原来
class AgentLoop:
    def __init__(self, llm, store, tools, max_steps=None):
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        self._max_steps = max_steps or MAX_STEPS

    def register_hook(self, hook): ...
    def unregister_hook(self, hook): ...

    async def run_stream(self, envelope: E2AEnvelope):
        session_id = envelope.session_id
        ...

# 改为
class ReActAgent:
    def __init__(self, llm, store, tools, *, hooks=(), max_steps=None):
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        for h in hooks:
            self._hook_manager.register_hook(h)
        self._max_steps = max_steps or MAX_STEPS

    async def run(self, request: AgentRequest):
        session_id = request.session_id
        ...
```

`_inner_run_stream` 内部逻辑完全不变，只把 `envelope.xxx` 替换为 `request.xxx`。

`register_hook`/`unregister_hook` 保留（测试需要动态注入 hook），但不作为主要构造方式。

---

## 4. `build_agent_loop` → `create_agent`

```python
# 原来：两步构造
loop = build_agent_loop(store, hooks=[...])
loop.register_hook(extra_hook)  # 可能还有后续注入

# 改为：一步构造
agent = create_agent(store, hooks=[...])
```

`create_agent()` 就是原来的 `build_agent_loop()`，只是返回 `ReActAgent` 而非 `AgentLoop`，hook 在内部注入好。

---

## 5. server.py 的职责更清晰

```python
# ws_handler：传输层只管三件事
# ① 解析 E2AEnvelope → AgentRequest
# ② 调 agent.run(request)
# ③ 把 E2AResponse 发回 ws

async def run_task(envelope):
    request = AgentRequest(
        session_id=envelope.session_id or envelope.request_id,
        request_id=envelope.request_id,
        query=(envelope.params or {}).get("query", ""),
        channel=envelope.channel or "web",
    )
    async for frame in agent.run(request):
        await send(frame)
```

---

## 6. 文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `twinkle/agentserver/agent.py` | **新增** | `ReActAgent` + `AgentRequest`。核心逻辑从 agent_loop.py 搬过来 |
| `twinkle/agentserver/agent_loop.py` | **删除**（或保留为兼容别名） | 内容移到 agent.py |
| `twinkle/agentserver/server.py` | 改 ~20 行 | E2AEnvelope→AgentRequest 转换；`loop`→`agent` |
| `twinkle/agentserver/__init__.py` | 改 1 行 | 导出 `ReActAgent` |
| `twinkle/agentserver/tools/builtin/subagent/executor.py` | 改 import | `AgentLoop` → `ReActAgent` |
| `tests/` | 改 import + 构造方式 | 所有 `AgentLoop(envelope)` → `ReActAgent(AgentRequest(...))` |

**不改**：`_inner_run_stream` 内部逻辑、`llm_client.py`、`tools/`、`hooks/`、`e2a/`、`gateway/`。

---

## 7. 验收

1. 现有所有测试通过（只改 import 和构造方式）
2. `ReActAgent.run(AgentRequest(...))` 行为与旧 `AgentLoop.run_stream(envelope)` 完全一致
3. `server.py` 不再把 E2AEnvelope 传给 agent
4. 测试可以直接构造 `AgentRequest(session_id="s1", request_id="r1", query="hello")` 而不需要伪造 E2AEnvelope

---

## 8. 与后续 Team 的关系

`ReActAgent` 是自然的基类：

```python
class TeamAgent(ReActAgent):
    """多 member 协作 agent。"""

    def __init__(self, ..., members: dict[str, MemberConfig]):
        super().__init__(...)
        self._members: dict[str, ReActAgent] = {}

    async def run(self, request: AgentRequest):
        # 覆盖 run()，加入 team 编排逻辑
        ...

    async def spawn_member(self, role: str) -> ReActAgent:
        ...
```

Team leader 和 member 都是 `ReActAgent`，抽象统一。每个 TeamAgent 实例管理自己的 member 集合，普通 `ReActAgent` 仍然是全局共享一个实例。
