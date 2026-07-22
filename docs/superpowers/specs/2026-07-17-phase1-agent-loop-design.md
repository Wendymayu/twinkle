# Phase 1 设计文档 — agent loop 最小闭环 + 短期会话记忆

> 状态：已实现（Phase 1 落地）。对应 [roadmap](../../roadmap.md) Phase 1 / 里程碑 M2。
>
> **实现期演进（与本设计快照不符，以 `docs/architecture.md` §4 为准）**：`max_steps` 默认已改 `1000`（`TWINKLE_AGENT_MAX_STEPS` env 可配，非 8）；`LLMClient.stream` 事件改为 `TextDelta | Finish`（`tool_calls` 累积进 `Finish`，由 `finish_reason` 区分，无独立 `ToolCalls`/`Done` 类型）；`run_unary` 已移除（流式专用）。
> 前置：[Phase 0 设计](../phase-0-design.md) 已实现并验收（两进程骨架 + echo 闭环）。
> 参考实现：`D:\code\opensource\gitcode\jiuwenswarm`（`jiuwenclaw/` 为 agent 应用层，`.py` 源码在 `enterprise_dev` 分支）。

## 1. 目标

在 Phase 0 的两进程骨架上，**只替换 `agentserver/server.py` 的 echo handler**，接入真模型 + agent loop 闭环 + 短期会话记忆。gateway 全链路（web_channel / message_handler / channel_manager / agent_client）零改动——这是 Phase 0 把接缝钉死的回报。

闭环：用户多轮提问 → 模型在跨轮上下文中判断是否调只读工具 → 调用 → 结果回灌 → 再决策 → 跨轮记住上文。直接接真模型，不做 mock 阶段。长期记忆用 stub 埋接口形状，不回炉。

## 2. 已定边界（决策记录）

| 决策点 | 选定 | 理由 |
|---|---|---|
| LLM 提供方 | OpenAI 兼容 | `messages` 列表 + function calling；base_url 可配，兼容大量端点 |
| 会话记录归谁持有 | **AgentServer** | gateway 真不动；E2A params 每轮只带本轮输入 + session_id |
| session_id 谁造 | **浏览器** | 新会话生成 uuid 随 req 带入；gateway 透传；对齐 jiuwenclaw channel 驱动 session |
| 流式接缝形态 | `AgentLoop.run_stream` 是 async generator，yield `E2AResponse` | loop 对 ws 零依赖，单测无需起 ws |
| Phase 1 工具集 | **web_fetch + web_search**（slim 重写） | 常用只读工具，不碰 deferred/Phase 2 子系统 |
| 存储持久化 | in-memory（Phase 1） | M2 只要会话内跨轮记忆；落盘是 YAGNI；SessionStore 接口允许后续换 |
| 长期记忆 | stub（空实现） | 埋接口形状，将来换真实现调用方不动 |

## 3. 模块结构

全部新增在 `agentserver` 内；gateway 零改动。

```
twinkle/agentserver/
  server.py            # [改] handler 不再内联 echo,dispatch 到 AgentLoop
  agent_loop.py         # [新] AgentLoop.run_stream / run_unary —— 核心闭环
  llm_client.py         # [新] LLMClient: openai SDK 薄封装, base_url/api_key 走 config
  session_store.py      # [新] SessionStore: in-memory dict[session_id, list[msg]]
  memory.py             # [新] LongTermMemory: recall/store stub
  tools/
    registry.py         # [新] ToolRegistry: schemas() / execute(name, args) —— 最小版
    web_fetch.py        # [新] slim 重写: 抓 URL → markdown/纯文本
    web_search.py       # [新] slim 重写: 单免费 provider 搜索
twinkle/config.py       # [改] 加 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
web/src/services/webClient.ts  # [改] 新会话造 uuid 并随 req 带 session_id
```

> 注：Phase 1 的 `ToolRegistry` 是最小版（静态注册、手写 schema）。Phase 2 扩成动态注册 + `tool_catalog` + 任务规划，到时再演进，不回炉。

## 4. 接口边界（五处）

### ① SessionStore（AgentServer 持有）

```python
class SessionStore:
    def get_messages(self, session_id: str) -> list[dict]
    def append(self, session_id: str, message: dict) -> None
    # 存的就是 OpenAI 原生 messages: role/content/tool_calls/tool_call_id
    # Phase 3 在此加 truncate/compress, Phase 1 不实现
```

不加归一化层——直接存 OpenAI `messages`，最小认知负担。in-memory `dict[session_id, list[dict]]`。

### ② LLMClient（openai SDK 薄封装）

```python
class LLMClient:
    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[LLMEvent]
# LLMEvent = TextDelta(str) | ToolCalls(list[dict]) | Done(finish_reason: str)
```

`base_url` + `api_key` + `model` 走 `config.py`，任何 OpenAI 兼容端点都能指。

### ③ ToolRegistry（最小版）

```python
class ToolRegistry:
    def schemas(self) -> list[dict]
        # -> OpenAI tools=[{"type":"function","function":{name,description,parameters}}]
    async def execute(self, name: str, args: dict) -> str
        # 只读工具,返回文本;未知 name 返回错误串
```

Phase 1 静态注册 web_fetch + web_search；Phase 2 演进为动态注册。

### ④ LongTermMemory（stub）

```python
class LongTermMemory:
    def recall(self, query: str) -> list[str]: return []   # 永远空
    def store(self, fact: str) -> None: pass
```

agent loop 在 build messages 前调 `recall`；stub 返回空即 no-op。将来换真实现，调用方一行不动——这是「不回炉」的落点。

### ⑤ AgentLoop（核心闭环）

```python
class AgentLoop:
    def __init__(self, llm: LLMClient, store: SessionStore,
                 tools: ToolRegistry, memory: LongTermMemory): ...
    async def run_stream(self, env: E2AEnvelope) -> AsyncIterator[E2AResponse]
    async def run_unary(self, env: E2AEnvelope) -> E2AResponse
```

## 5. agent loop 闭环轨迹（一次 chat.send）

1. `server.py` handler 解析 `E2AEnvelope`，取 `session_id` + `params.query`，调 `loop.run_stream(env)`。
2. loop：`store.append(session_id, {role:"user", content:query})`；`memory.recall(query)`（stub 空）；`msgs = store.get_messages(session_id)`。
3. **ReAct 循环**，`max_steps=8` 守护防死循环：
   - `llm.stream(messages=msgs, tools=tool_registry.schemas())` 流式产出。
   - `TextDelta` → 包成 `E2AResponse(response_kind="e2a.chunk", body.result.content=delta)` **yield**。
   - `ToolCalls` → 对每个 `tool_call` 调 `tool_registry.execute(name, args)`，把 `{role:"tool", tool_call_id, content:result}` append 进 store；进入下一轮 `llm.stream`（带工具结果回灌）。
   - `Done(finish_reason="stop")` → 把 assistant 消息 append 进 store，**yield** 终止帧 `response_kind="e2a.complete"`，break。
   - 达 `max_steps` 仍未收敛 → **yield** `e2a.error` 终止。
4. `server.py` `async for chunk in loop.run_stream(env): await ws.send(chunk.model_dump_json())`——和 Phase 0 `_echo_stream` 消费方式完全一致。

工具结果回灌是 agent loop 多步的命门：tool 消息 append 进 store 后，下一轮 `get_messages` 自然带上，模型据此再决策。

## 6. 工具重写说明（slim 版，非搬运）

jiuwenclaw 工具绑死 `openjiuwen.core.foundation.tool`（`@tool`/`ToolCard`），Twinkle 无此框架。故「迁移」= 参考其实现、在 Twinkle 自有 ToolRegistry 下重写，砍掉 deferred 依赖。

- **web_fetch**：参考 `jiuwenclaw/agentserver/tools/web_fetch_tools.py`（713 行）。砍 Jina Reader、trafilatura。保留：http GET（`requests`/`httpx`）、charset 探测、HTML 去标签转文本、长度裁剪、错误简报。返回 markdown/纯文本。
- **web_search**：参考 `jiuwenclaw/agentserver/tools/web_search/`（11 文件多 provider orchestrator）。砍 paid provider、orchestrator、quality 层。只留**单个免费 provider**（实现期定，倾向 DuckDuckGo Lite HTML 解析，无 key）。返回条目列表文本。

明确不迁：`command_tools`（危险工具，roadmap 推迟）、`memory_tools`（绑长期记忆，roadmap 不做）、`todo/task_tools`（Phase 2 任务规划）。

## 7. 关键取舍

- **gateway 零改动**：会话状态归 AgentServer，E2A 每轮只带本轮输入 + session_id。兑现 Phase 0 衔接点承诺。
- **in-memory 存储**：Phase 1 单用户、M2 只要会话内跨轮记忆。SessionStore 接口允许后续换 SQLite，不回炉。
- **OpenAI function calling 原生协议**：不自造 JSON schema，工具调用与回灌走 SDK 原生结构。
- **不 mock 模型，工具可薄**：roadmap「直接接真模型」针对模型链路；工具本身允许是 slim 实现。
- **max_steps=8**：工具循环可能不收敛，硬上限，超出发 `e2a.error`。
- **stub 长期记忆**：接口形状钉死，Phase 1 不实现真逻辑，后续不回炉。

## 8. 测试

- **AgentLoop 单测**：mock `LLMClient`（产出预设 TextDelta + ToolCalls + Done），断言 store 被正确 append、tool 被调、终止帧正确。全程不起 ws——这是 async generator 接缝的回报。
- **SessionStore / ToolRegistry / LongTermMemory**：各自纯函数级单测。
- **web_fetch / web_search**：mock http 响应，断言解析与裁剪。
- **E2E**：复用 Phase 0 `tests/test_integration.py` 模式，断言从 echo 换成「真模型 + 只读工具」多轮闭环 + 跨轮记忆。
- **验收对齐 M2**：多轮提问 → 跨轮记住上文 → 调只读工具 → 结果整合进回答。

## 9. Phase 1 明确不做

动态工具注册 / `tool_catalog` / 任务规划（Phase 2）、上下文压缩（Phase 3）、长期记忆真实现、危险工具审批、command/memory/todo 工具、多 provider 搜索、Jina/trafilatura 富抓取、会话落盘持久化。

## 10. 下阶段衔接点（Phase 2）

`ToolRegistry` 从静态注册演进为动态注册 + `tool_catalog`；`SessionStore` 加 `truncate/compress`（Phase 3 上下文压缩）；`LongTermMemory` stub 换真实现（后续）。这些都在已定义接口上扩，不改调用方。
