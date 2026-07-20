# Phase 2 — 工具系统成形 设计

> 日期：2026-07-20
> 范围：把 `twinkle/agentserver/tools/` 从"静态手写 schema 的 registry"重写成 openjiuwen 风格的 `@tool` + `ToolCard`/`Tool`/`LocalFunction`/`ToolManager`，动态可注册，加最小手写 schema 抽取器。
> 参考：openjiuwen `core/foundation/tool/{base,function,tool,utils}` + `core/single_agent/ability_manager.py`（jiuwenswarm-instrumentor 插桩的目标）。

---

## 1. 目标与范围

### 做
- 引入 `@tool` 装饰器：把普通 Python 函数转成 `LocalFunction`，自动从签名 + docstring 抽取 OpenAI function-calling 的 JSON schema。
- 引入四层结构：`ToolCard`（纯元数据）/ `Tool`（接口）/ `LocalFunction`（本地函数这一种实现）/ `ToolManager`（容器，存 `dict[str, Tool]`）。
- 动态注册：`register(tool)` / `unregister(name)` / `list()` / `get(name)`，运行时用 Python 对象 add/remove 工具。
- `tool_catalog()` 查询：列出 `[{name, description}]`，供 UI / 可观测使用。
- 迁移 `web_fetch` / `web_search` 成 `@tool` 装饰的函数。

### 不做（明确砍，对齐 roadmap）
- 浏览器 RPC `tools.add`（jiuwenclaw 在 AbilityManager 之上另叠的一层，属企业级，roadmap 已砍企业级）。
- MCP stdio 子进程工具（openjiuwen 有 `foundation/tool/mcp/`，属重资源，twinkle 不做）。
- `tool_concurrency` 并发控制（单用户串行够用，roadmap 已标"倾向先砍"）。
- `plan_todo` 任务规划——拆到 Phase 2b 单独 spec，不在本 Phase。

### 命门约束
agent_loop 的调用面 `self._tools.schemas()` 与 `self._tools.execute(name, args)` **签名不变**。`ToolManager` 是现 `ToolRegistry` 的超集，agent_loop 零改动即应全绿——这是"不回炉"的硬验收。

---

## 2. 动态注册的含义（澄清）

"动态注册"在 twinkle 里特指：**运行时用 Python 对象 add/remove 工具**，对齐 openjiuwen `AbilityManager.add_ability(card, resource)` / `remove_ability(name)` / `list()`。它**不是**浏览器 RPC、也不是 MCP 子进程注册——那两层是 jiuwenclaw 在 AbilityManager 之上另叠的，属企业级，roadmap 已砍。

判定依据：openjiuwen `AbilityManager` 确有运行时 add/remove/list（已核实源码），故按用户指示"有动态注册就参考"——twinkle 实现这一层。

---

## 3. 四层结构：各是什么、关系、为什么分

### 3.1 结构图

```
┌─────────────────────────────────────────────────────────┐
│ ToolManager（容器）                                       │
│   dict[str, Tool]          ← 类型是 Tool，不是 LocalFunction │
│   register(tool) / unregister / list / get /             │
│   schemas / execute / catalog                             │
└──────────────────────────┬──────────────────────────────┘
                           │ 存（只认 Tool 接口：card + invoke）
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Tool（抽象接口，base.py）                                 │
│   card: ToolCard          ← 元数据（任何工具都得有）        │
│   async invoke(args) -> str   ← 执行入口（任何工具都得有）   │
└──────────────────────────┬──────────────────────────────┘
                           │ 实现（LocalFunction 是其中一种）
                           ▼
┌─────────────────────────────────────────────────────────┐
│ LocalFunction(Tool)                                      │
│   card: ToolCard   func: Callable                          │
│   invoke(args) = await self.func(**args)                    │
└──────────────────────────┬──────────────────────────────┘
                           │ 持有
                           ▼
┌─────────────────────────────────────────────────────────┐
│ ToolCard（纯元数据）                                       │
│   name / description / parameters                          │
└─────────────────────────────────────────────────────────┘
```

### 3.2 各层是什么

| 层 | 是什么 | 不是什么 |
|---|---|---|
| `ToolCard` | 工具的**纯描述数据**：name + description + parameters JSON schema | 不持有函数引用、不能执行 |
| `Tool` | "任何工具都得满足的接口"：有 `card` + `invoke` | 不是某个具体工具种类 |
| `LocalFunction` | "本地 Python 函数工具"这个**具体执行机制**：ToolCard + Callable，`invoke` 一行调 func | 不负责管理多个工具、不持有其它工具 |
| `ToolManager` | 工具的**容器与调度**：登记 / 查找 / 列举 / 执行 / 出 schema / 出 catalog | 不关心某个工具是本地函数还是别的——只认 `Tool` 接口 |

### 3.3 关系

- `LocalFunction` **持有** `ToolCard`（组合），并**实现** `Tool` 接口。
- `ToolManager` **存** `Tool`（`dict[str, Tool]`），不存 `LocalFunction` 类型本身。
- `ToolCard` 是被 `Tool` / `LocalFunction` / `ToolManager` 三层共同依赖的**纯数据底座**。

### 3.4 为什么分四层，而不是拍扁

1. **`ToolCard` 单独存在 → "工具的描述"和"工具的执行"解耦。** schema 要发给模型、catalog 要列给 UI/可观测看；这些场景**只读元数据、不带执行体**。ToolCard 是"可以脱离函数单独流动"的那一份数据。把 func 塞进 ToolCard，就再没有"纯描述"对象可拿——`schemas()`/`catalog()` 都得从"持函数引用的对象"里摘字段，边界糊掉。

2. **`Tool` 接口单独存在 → 让"manager 只认接口"字面成立。** manager 的三个方法全在 `Tool` 接口上操作：`schemas()` 走 `t.card.parameters`、`catalog()` 走 `t.card.name/description`、`execute()` 走 `t.invoke()`——没有一个 `isinstance(t, LocalFunction)` 分支。所以将来加 `McpTool(Tool)`（card 来自远端 schema、invoke 走子进程），manager 零改动直接存。没有 `Tool` 这层，manager 就得 `dict[str, LocalFunction]`，"只认接口"就是空头支票。

3. **`LocalFunction` 单独存在 → 它是"本地函数工具"这个工具种类的具名实体。** openjiuwen 还有 MCP 工具、REST API 工具——执行机制不同，但都实现 `Tool` 接口、都产出 `ToolCard`。`LocalFunction` 是"其中一种执行机制"，`invoke()` 是这一种的执行入口。twinkle Phase 2 只有这一种，但保留这层 = 给第二种工具（MCP）留接入点：加 `McpTool(Tool)` 喂给同一个 ToolManager 即可。

4. **`ToolManager` 存 `Tool` → 与"工具是哪种执行机制"无关。** 这是 openjiuwen AbilityManager 能同时管本地/MCP/REST 工具的根因；twinkle Phase 2 只用上一格，但**接口形状按这个根因钉死**，后续不回炉。

### 3.5 一句话总结

`ToolCard` 是"工具长什么样"（描述），`Tool` 是"任何工具都得满足的接口"（card + invoke），`LocalFunction` 是"本地函数工具"这个接口的**一种**实现，`ToolManager` 存 `Tool`、只认接口——四层是 **描述 / 接口 / 实现 / 容器** 的正交切分，不是层层冗余。

---

## 4. 模块结构

```
twinkle/agentserver/tools/
  base.py             # ToolCard + Tool(Protocol)
  local_function.py   # LocalFunction(Tool)
  schema_extractor.py # 签名/docstring → (name, description, parameters)
  decorator.py        # @tool → LocalFunction
  manager.py          # ToolManager，存 dict[str, Tool]
  web_fetch.py        # 迁移成 @tool
  web_search.py       # 迁移成 @tool
  __init__.py         # build_default_manager() + 导出 ToolManager/LocalFunction/tool
```

现有 `registry.py` 的职责拆进 `base.py` + `manager.py` + `schema_extractor.py` 后删除。分文件是因为每件有独立职责（对齐 openjiuwen `foundation/tool/{base,function,tool,utils}`），且每个文件能单独测。

---

## 5. 各模块定义

### 5.1 `base.py` — ToolCard + Tool 接口

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class ToolCard:
    name: str
    description: str
    parameters: dict   # OpenAI function-calling 的 parameters JSON schema

class Tool(Protocol):
    card: ToolCard
    async def invoke(self, args: dict) -> str: ...
```

比 openjiuwen `foundation/tool/base.py` 砍掉 `id` / `stateless` / `Input` / `Output` / 触发器层——twinkle 不做多 agent 共享、不做 OTel 插桩基类。YAGNI。

### 5.2 `local_function.py` — 本地函数工具

```python
@dataclass
class LocalFunction:
    card: ToolCard
    func: Callable[..., Awaitable[str]]

    async def invoke(self, args: dict) -> str:
        return await self.func(**args)
```

twinkle 工具返回 `str`（现有契约，agent_loop 直接 append 进 store）。不做 openjiuwen 的 `stream()` 生成器分支——工具暂不流式。`invoke()` 现在一行，是给将来留的扩展点（schema 校验、调用日志、结果截断都只改这里）。

### 5.3 `schema_extractor.py` — 最小手写抽取器

纯 stdlib（`inspect` / `typing`），~50 行，纯函数无副作用。

**类型映射**：

| Python 注解 | JSON schema type |
|---|---|
| `str` | `string` |
| `int` | `integer` |
| `float` | `number` |
| `bool` | `boolean` |
| `list` / `List[...]` | `array` |
| `dict` / `Dict[...]` | `object`（无 properties） |
| `Optional[X]` / `X \| None` | 解包取 X，参数标为非 required |

**required 判定**：无默认值的参数 → required；有默认值的参数 → 把默认值填进 schema 的 `default`。

**description**：函数整体描述取 docstring 首段；**不做 per-param 描述解析**（不解析 google/numpy 风格 `Args:` 块——YAGNI，需要 per-param 描述时用 `@tool(input_params={...})` 手写覆盖）。

**不支持**：嵌套对象、Pydantic model 入参、自定义类。遇到不认识的类型 → `{"type": "string"}` 兜底 + 不进 required（不让抽取器崩）。

**契约**：`extract(func) -> tuple[str, str, dict]`（name, description, parameters），纯函数，直接单测。

### 5.4 `decorator.py` — `@tool`

```python
def tool(func=None, *, name=None, description=None, input_params=None) -> LocalFunction:
    ...
```

支持三种用法：`@tool` / `@tool()` / `@tool(name=..., input_params={...})`。
- `name` 默认 `func.__name__`，可覆盖。
- `description` 默认 docstring 首段，可覆盖。
- `input_params` 默认从签名抽取，可手写覆盖。
返回 `LocalFunction`，可直接喂 `ToolManager.register()`。

### 5.5 `manager.py` — ToolManager

```python
class ToolManager:
    def register(self, tool: Tool) -> None: ...        # 动态注册
    def unregister(self, name: str) -> bool: ...      # 动态移除，返回是否曾存在
    def get(self, name: str) -> Tool | None: ...
    def list(self) -> list[Tool]: ...                  # openjiuwen 对齐
    def schemas(self) -> list[dict]: ...               # ← agent_loop 调用面不变
    async def execute(self, name: str, args: dict) -> str: ...  # ← 不变
    def catalog(self) -> list[dict]: ...               # [{name, description}]
```

`schemas()` 产 OpenAI function-calling 格式（`{type:"function", function:{name,description,parameters}}`）——agent_loop 直接喂 LLM。
`execute()` 保留现有契约：工具异常转成 `[tool error] {Type}: {msg}` 字符串，agent_loop 靠这个不崩。
`catalog()` 只读 `card.name`/`card.description`——纯元数据流动，不带 func。

### 5.6 `__init__.py` — 装配

`build_default_manager()`：建 `ToolManager`，`register(web_fetch_tool)`、`register(web_search_tool)`。导出 `ToolManager` / `LocalFunction` / `tool` / `build_default_manager`。

---

## 6. 迁移与零回炉验收

### 迁移
- `web_fetch.py` / `web_search.py`：函数体不动，加 `@tool`。若自动抽取丢了关键 per-param 描述，用 `@tool(input_params={...})` 手写覆盖。
- `registry.py` 删除，`build_default_registry()` → `build_default_manager()`。
- `agent_loop.py`：`__init__` 的 `tools: ToolRegistry` 注解改 `ToolManager`，其余零改动。
- `server.py`：`build_default_loop()` 内 `build_default_registry()` 换名 `build_default_manager()`。

### 零回炉验收
- `test_integration.py` / `test_agent_loop.py`：agent_loop 调用面不变，**应零改动全绿**。其中 integration test 的 `_reg_with_echo()` 改成用 `@tool` 定义 echo 工具（这是唯一一处测试侧改动，验证迁移路径本身）。

---

## 7. 测试策略

- `test_schema_extractor.py`（新）：纯函数单测——str/int/float/bool/list/dict/Optional/有默认值/无默认值/docstring 抽取/不认识类型兜底。
- `test_tool_decorator.py`（新）：`@tool` / `@tool()` / `@tool(name=, input_params=)` 三种用法产出正确 `LocalFunction`。
- `test_tool_manager.py`（从 `test_tool_registry.py` 演进）：register/unregister/list/get/catalog + execute 异常转字符串契约 + schemas 产 OpenAI 格式 + 动态 register 后 `schemas()`/`catalog()` 立刻可见。
- `test_local_function.py`（新）：`invoke()` 调透 func、参数透传。
- `test_base.py`（新，可选）：ToolCard 数据类构造；`Tool` Protocol 结构性（LocalFunction 满足接口）。

### M3 验收（对齐 roadmap"多工具正确选择"）
- 模型可见 ≥2 个工具的 schema、能选对、能执行。
- 动态 register 一个新工具后 `schemas()` 立刻包含它、`catalog()` 能列。
- agent_loop 零改动下全链路测试全绿（零回炉）。

---

## 8. 与参考实现 openjiuwen 的对照

| Twinkle | openjiuwen | 差异 |
|---|---|---|
| `tools/base.py` (`ToolCard`+`Tool`) | `foundation/tool/base.py` (`Tool`+`ToolCard`+`Input`/`Output`) | 砍 `Input`/`Output`/触发器基类 |
| `tools/local_function.py` | `foundation/tool/function/function.py` (`LocalFunction`) | 砍 `stream()` 生成器分支、schema 校验、trigger |
| `tools/decorator.py` (`@tool`) | `foundation/tool/tool.py` (`@tool`) | 砍 `card`/`stateless`/Pydantic model 路径，只留 dict schema 覆盖 |
| `tools/schema_extractor.py` | `foundation/tool/utils/callable_schema_extractor.py` | 最小手写版，不支持嵌套/Pydantic |
| `tools/manager.py` (`ToolManager`) | `core/single_agent/ability_manager.py` (`AbilityManager`) | 砍多 agent 共享 / 权限 / OTel，保留 add/remove/list/schemas/execute/catalog 形状 |

twinkle 是 openjiuwen 这几层的**最小子集 + 学习重写**：接口形状对齐（后续 plan_todo 等工具直接 `@tool` 落地、将来接 MCP 加 `McpTool(Tool)` 不回炉），实现砍到 twinkle 轻量定位够用为止。
