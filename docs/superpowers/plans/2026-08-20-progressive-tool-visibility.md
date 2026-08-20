# Progressive Tool Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Twinkle 加 eager/deferred 二档工具可见性 + `tools_search`/`invoke_tool` 两 meta-tool + 导航清单,解决 MCP 工具接入后 schema 线性膨胀。

**Architecture:** 叠加层,不侵入核心——`tool_manager()` 不动;新增 `apply_progressive_tools(tm, config)` builder,启用则注册两 meta-tool(持有 tm 引用)+ 返回 `ProgressiveToolHook`;hook 实现 `before_invoke`(注导航 frozen_section)+ `before_model_call`(过滤 ctx.inputs.tools 为 eager)。接入点 `server.py create_agent` 的 auto-wired hook 列表(对齐 `SubagentContextHook` 模式)。

**Tech Stack:** Python 3.12, asyncio(`asyncio.run`,无 pytest-asyncio),pydantic config,现有 Tool/ToolManager/AgentHook 体系。

**Spec:** `docs/superpowers/specs/2026-08-20-progressive-tool-visibility-design.md`

**注:** commit step 按 TDD 节奏列出;实际执行遵守 `no-direct-github-push` 习惯,**每次 commit 前先问用户**。

---

## File Structure

- **Create** `twinkle/agentserver/tools/builtin/progressive_tools.py` — 两 meta-tool(`ToolsSearchTool`/`InvokeToolTool`)+ `META_NAMES` 常量。手写 Tool 实现(非 `@tool`,因需持有 tm 引用)。
- **Create** `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py` — `ProgressiveToolHook`(`before_invoke` 注导航 + `before_model_call` 过滤 tools)。
- **Create** `twinkle/agentserver/tools/progressive.py` — `apply_progressive_tools(tm, config) -> ProgressiveToolHook | None` builder。
- **Modify** `twinkle/config/schema.py` — 加 `ProgressiveToolConfig` + `TwinkleConfig.progressive_tool` 字段。
- **Modify** `twinkle/resources/config.yaml` — 加 `progressive_tool` 顶级段。
- **Modify** `twinkle/agentserver/server.py` `create_agent` — 接入 `apply_progressive_tools`(在 `mcp.register_into` 之后)。
- **Modify** `twinkle/agentserver/hooks/builtin/__init__.py` — 导出 `ProgressiveToolHook`(对齐现有 hook 导出)。
- **Create** `tests/test_progressive_tools.py` — meta-tool 单元测试。
- **Create** `tests/test_progressive_tool_hook.py` — hook 单元 + 端到端集成测试。
- **Create** `tests/test_progressive_config.py` — config 单元测试。

关键接口(已核实):
- `ToolCard(name: str, description: str, parameters: dict)`(`tools/base.py`)
- `Tool` 协议:`card: ToolCard` + `async invoke(self, args: dict) -> str`(`tools/base.py`)— **invoke 返回 str**
- `ToolManager.register(tool)` / `schemas() -> list[dict]` / `execute(name, args) -> str`(`tools/manager.py`)
- `PromptSection(name, content, priority)`(`prompts.py`)
- `HookContext`(含 `inputs`/`extra`/`agent`,`hooks/base.py`);`ModelCallInputs(messages, tools)` 可重赋 `tools`
- `ReActAgent(llm, store, tools, hooks=(), base_sections=, max_steps=)`(`agent.py`)
- mock LLM:`stream(messages, tools)` 签名(对齐 `test_agent_loop_context_assembly.py`)

---

## Task 1: 配置层(ProgressiveToolConfig)

**Files:**
- Create: `tests/test_progressive_config.py`
- Modify: `twinkle/config/schema.py`(加 `ProgressiveToolConfig` 类 + `TwinkleConfig` 字段)
- Modify: `twinkle/resources/config.yaml`(加 `progressive_tool` 段)

- [ ] **Step 1: Write the failing test**

Create `tests/test_progressive_config.py`:
```python
# tests/test_progressive_config.py
"""ProgressiveToolConfig: 默认关 + extra 禁止(对齐 _StrictModel)。"""
import pytest
from twinkle.config.schema import TwinkleConfig, ProgressiveToolConfig


def test_progressive_tool_defaults_disabled():
    cfg = TwinkleConfig()
    assert cfg.progressive_tool.enabled is False
    assert cfg.progressive_tool.eager_tools == []


def test_progressive_tool_extra_keys_rejected():
    with pytest.raises(Exception):
        ProgressiveToolConfig(enabled=True, bogus_field=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProgressiveToolConfig'`

- [ ] **Step 3: Write minimal implementation**

In `twinkle/config/schema.py`, add the class before `TwinkleConfig`(e.g. after `McpConfig`, around line 288):
```python
class ProgressiveToolConfig(_StrictModel):
    enabled: bool = False            # 默认关:行为等同现状(全量塞)
    eager_tools: list[str] = []      # 空=默认(33 内置全 + tools_search + invoke_tool)
```

In `TwinkleConfig`(around line 309, after `mcp: McpConfig = McpConfig()`), add:
```python
    progressive_tool: ProgressiveToolConfig = ProgressiveToolConfig()
```

In `twinkle/resources/config.yaml`, add a top-level section (e.g. after the `mcp:` block):
```yaml
progressive_tool:
  enabled: false        # 默认关:全量工具 schema 塞模型;开 + MCP 开 → MCP 工具进 deferred
  eager_tools: []       # 空=默认(33 内置全 + tools_search + invoke_tool);自定义则只列出的进 eager
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_progressive_config.py twinkle/config/schema.py twinkle/resources/config.yaml
git commit -m "feat(config): add ProgressiveToolConfig (default disabled)"
```

---

## Task 2: ToolsSearchTool(meta-tool 1)

**Files:**
- Create: `twinkle/agentserver/tools/builtin/progressive_tools.py`
- Create: `tests/test_progressive_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_progressive_tools.py`:
```python
# tests/test_progressive_tools.py
"""ToolsSearchTool: 按 deferred 工具注册名精确(大小写不敏感)查 schema,返回 JSON 字符串。"""
import asyncio
import json

from twinkle.agentserver.tools.builtin.progressive_tools import ToolsSearchTool, META_NAMES
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


def _tm_with_tools():
    @tool
    async def read_file(path: str) -> str:
        """read a file"""
        return f"content:{path}"

    @tool
    async def mcp_query(sql: str) -> str:
        """query db"""
        return f"rows:{sql}"

    m = ToolManager()
    m.register(read_file)
    m.register(mcp_query)
    return m


_EAGER = {"read_file", "tools_search", "invoke_tool"}


def test_tools_search_finds_deferred_by_exact_name():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "mcp_query"})))
    assert res["success"] is True
    assert len(res["matches"]) == 1
    assert res["matches"][0]["name"] == "mcp_query"
    assert "input_schema" in res["matches"][0]


def test_tools_search_case_insensitive():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "MCP_QUERY"})))
    assert res["success"] is True


def test_tools_search_miss_returns_guidance_message():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "nope"})))
    assert res["success"] is False
    assert res["matches"] == []
    assert "导航列表" in res["message"]


def test_tools_search_excludes_eager_tools():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "read_file"})))
    assert res["success"] is False  # eager 不在 deferred,搜不到


def test_meta_names_constant():
    assert META_NAMES == frozenset({"tools_search", "invoke_tool"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'ToolsSearchTool'`

- [ ] **Step 3: Write minimal implementation**

Create `twinkle/agentserver/tools/builtin/progressive_tools.py`:
```python
"""Progressive tool visibility meta-tools: tools_search + invoke_tool.

手写 Tool 实现(非 @tool,需持有 ToolManager 引用)。对齐 jiuwenswarm
deep_agent/rails/jiuwen_progressive_tool_rail.py 的两个 meta-tool,但简化:
deferred 工具实例统一在 ToolManager,invoke_tool 直接转调 tm.execute,
省 jiuwenswarm 的 resource_mgr 间接层。

invoke 返回 JSON 字符串(对齐 Twinkle Tool.invoke -> str 协议)。
"""
from __future__ import annotations

import json
from typing import Any

from twinkle.agentserver.tools.base import ToolCard

META_NAMES = frozenset({"tools_search", "invoke_tool"})

_TOOLS_SEARCH_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {
            "type": "string",
            "description": "按需可见工具的注册名称(与系统导航列表中的名称一致,精确匹配,忽略大小写)",
        }
    },
    "required": ["tool_name"],
}

_INVOKE_TOOL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string", "description": "按需可见工具的注册名称"},
        "arguments": {
            "type": "object",
            "description": "根据 tools_search 返回的 input_schema 构造的参数",
        },
    },
    "required": ["tool_name", "arguments"],
}


def deferred_schemas(tm: Any, eager_names) -> list[dict]:
    """返回 deferred 工具的 schema 列表(tm 全量 - eager - meta)。"""
    eager = set(eager_names) | META_NAMES
    return [s for s in tm.schemas() if s["function"]["name"] not in eager]


class ToolsSearchTool:
    """tools_search: 按 deferred 工具注册名精确(大小写不敏感)查完整 schema。"""

    def __init__(self, tm: Any, eager_names) -> None:
        self._tm = tm
        self._eager = set(eager_names)
        self.card = ToolCard(
            name="tools_search",
            description=(
                "按工具注册名查询按需可见(deferred)工具的完整 input_schema。"
                "入参 tool_name 须与系统导航列表中的名称一致(精确匹配,忽略大小写)。"
                "调用 invoke_tool 前必须先调本工具获取 schema,再据此构造 arguments。"
            ),
            parameters=_TOOLS_SEARCH_PARAMS,
        )

    async def invoke(self, args: dict) -> str:
        try:
            tool_name = str(args.get("tool_name", "")).strip()
            key = tool_name.lower()
            if not key:
                return json.dumps(
                    {"success": False, "matches": [], "message": "tool_name is required"},
                    ensure_ascii=False,
                )
            matches = []
            for s in deferred_schemas(self._tm, self._eager):
                name = s["function"]["name"]
                if name.lower() == key:
                    matches.append({
                        "name": name,
                        "description": s["function"]["description"],
                        "input_schema": s["function"]["parameters"],
                    })
            if matches:
                message = (
                    f"已找到工具 '{tool_name}',请根据 input_schema 构造 arguments "
                    f"后调用 invoke_tool。"
                )
            else:
                message = (
                    f"未找到名为 '{tool_name}' 的按需可见工具,"
                    f"请检查名称是否与导航列表一致。"
                )
            return json.dumps(
                {"success": bool(matches), "matches": matches,
                 "count": len(matches), "message": message},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "matches": [], "error": str(exc)},
                ensure_ascii=False,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_tools.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/progressive_tools.py tests/test_progressive_tools.py
git commit -m "feat(tools): add ToolsSearchTool meta-tool (deferred schema lookup)"
```

---

## Task 3: InvokeToolTool(meta-tool 2)

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/progressive_tools.py`(加 `InvokeToolTool`)
- Modify: `tests/test_progressive_tools.py`(加 invoke_tool 测试)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_progressive_tools.py`:
```python
from twinkle.agentserver.tools.builtin.progressive_tools import InvokeToolTool


def test_invoke_tool_runs_deferred_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    # mcp_query 是 deferred,转调 tm.execute
    result = asyncio.run(inv.invoke({"tool_name": "mcp_query",
                                     "arguments": {"sql": "SELECT 1"}}))
    assert result == "rows:SELECT 1"


def test_invoke_tool_rejects_eager_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    res = json.loads(asyncio.run(inv.invoke({"tool_name": "read_file",
                                            "arguments": {"path": "x"}})))
    assert res["success"] is False
    assert "不是按需可见工具" in res["error"]


def test_invoke_tool_rejects_unknown_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    res = json.loads(asyncio.run(inv.invoke({"tool_name": "nope",
                                            "arguments": {}})))
    assert res["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_tools.py::test_invoke_tool_runs_deferred_tool -v`
Expected: FAIL with `ImportError: cannot import name 'InvokeToolTool'`

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/progressive_tools.py`:
```python
class InvokeToolTool:
    """invoke_tool: 按名间接调用 deferred 工具(转调 ToolManager.execute)。"""

    def __init__(self, tm: Any, eager_names) -> None:
        self._tm = tm
        self._eager = set(eager_names)
        self.card = ToolCard(
            name="invoke_tool",
            description=(
                "按名间接调用按需可见(deferred)工具。先 tools_search 拿 schema,"
                "再据此构造 arguments。"
            ),
            parameters=_INVOKE_TOOL_PARAMS,
        )

    async def invoke(self, args: dict) -> str:
        try:
            tool_name = str(args.get("tool_name", "")).strip()
            arguments = args.get("arguments", {}) or {}
            deferred_names = {
                s["function"]["name"] for s in deferred_schemas(self._tm, self._eager)
            }
            if tool_name not in deferred_names:
                return json.dumps(
                    {"success": False,
                     "error": f"'{tool_name}' 不是按需可见工具"
                              f"(可能已在 tools 列表或不存在),请先 tools_search 确认。"},
                    ensure_ascii=False,
                )
            result = await self._tm.execute(tool_name, arguments)
            return result  # 被调工具本就返回 str(对齐 Tool.invoke 协议)
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": str(exc)}, ensure_ascii=False,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_tools.py -v`
Expected: PASS (8 tests: 5 from Task 2 + 3 new)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/progressive_tools.py tests/test_progressive_tools.py
git commit -m "feat(tools): add InvokeToolTool meta-tool (deferred tool indirect invoke)"
```

---

## Task 4: ProgressiveToolHook(单元:过滤 + 导航 + no-op)

**Files:**
- Create: `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py`
- Create: `tests/test_progressive_tool_hook.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_progressive_tool_hook.py`:
```python
# tests/test_progressive_tool_hook.py
"""ProgressiveToolHook: before_model_call 过滤 tools 为 eager;before_invoke 注导航;
无 deferred 时 no-op。对齐 SkillHook 模式。"""
import asyncio

from twinkle.agentserver.hooks.base import (
    HookContext, HookEvent, ModelCallInputs, InvokeInputs,
)
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.builtin.progressive_tools import (
    ToolsSearchTool, InvokeToolTool, META_NAMES,
)
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


def _setup(eager):
    @tool
    async def read_file(path: str) -> str:
        """read a file"""
        return f"content:{path}"

    @tool
    async def mcp_query(sql: str) -> str:
        """query db"""
        return f"rows:{sql}"

    m = ToolManager()
    m.register(read_file)
    m.register(mcp_query)
    m.register(ToolsSearchTool(m, eager))
    m.register(InvokeToolTool(m, eager))

    class _Agent:
        _tool_manager = m

    hook = ProgressiveToolHook(eager)
    hook.init(_Agent())
    return m, hook, _Agent


_EAGER = ["read_file", "tools_search", "invoke_tool"]


def test_before_model_call_filters_to_eager():
    m, hook, AgentCls = _setup(_EAGER)
    ctx = HookContext(
        agent=AgentCls(), event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=m.schemas()),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_model_call(ctx))
    names = [s["function"]["name"] for s in ctx.inputs.tools]
    assert "mcp_query" not in names        # deferred 被过滤
    assert "read_file" in names
    assert "tools_search" in names and "invoke_tool" in names


def test_before_invoke_injects_navigation():
    m, hook, AgentCls = _setup(_EAGER)
    ctx = HookContext(
        agent=AgentCls(), event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q", mode=""),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_invoke(ctx))
    navs = [s for s in ctx.extra.get("frozen_sections", [])
            if s.name == "tool_navigation"]
    assert len(navs) == 1
    content = navs[0].content
    assert "mcp_query" in content          # deferred 进导航
    assert "read_file" not in content      # eager 不进导航
    assert "tools_search" not in content   # meta 不进导航
    assert "不可直接调用" in content
    assert "tools_search" in content and "invoke_tool" in content  # 用法提示


def test_no_deferred_is_noop():
    # 只有 eager 工具 + meta,无 deferred → before_invoke 不注导航
    @tool
    async def read_file(path: str) -> str:
        """read"""
        return path

    m = ToolManager()
    m.register(read_file)
    m.register(ToolsSearchTool(m, _EAGER))
    m.register(InvokeToolTool(m, _EAGER))

    class _Agent:
        _tool_manager = m

    hook = ProgressiveToolHook(_EAGER)
    hook.init(_Agent())
    ctx = HookContext(
        agent=_Agent(), event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q", mode=""),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_invoke(ctx))
    navs = [s for s in ctx.extra.get("frozen_sections", [])
            if s.name == "tool_navigation"]
    assert navs == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_tool_hook.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProgressiveToolHook'`

- [ ] **Step 3: Write minimal implementation**

Create `twinkle/agentserver/hooks/builtin/progressive_tool_hook.py`:
```python
"""ProgressiveToolHook — eager/deferred 工具可见性。

对齐 jiuwenswarm JiuWenProgressiveToolRail,精简为两个事件:
- before_invoke: 注 deferred 工具导航 frozen_section(跨步稳定 + cache 友好,对齐 SkillHook)
- before_model_call: 过滤 ctx.inputs.tools 为 eager 名单

eager 名单构造时传入(来自 builder);tm 经 init(agent) 从 agent._tool_manager 取。
"""
from __future__ import annotations

import logging
from typing import Any

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.tools.builtin.progressive_tools import (
    META_NAMES, deferred_schemas,
)

log = logging.getLogger("twinkle.hooks.progressive_tool")

_NAV_PRIORITY = 70
_DESC_LIMIT = 160


class ProgressiveToolHook(AgentHook):
    """eager/deferred 工具渐进可见。enabled 由 builder 控制(关闭则不注册本 hook)。"""

    priority = 70

    def __init__(self, eager_names) -> None:
        self.eager_names = set(eager_names) | META_NAMES
        self._tm: Any = None

    def init(self, agent: Any) -> None:
        self._tm = getattr(agent, "_tool_manager", None)

    async def before_invoke(self, ctx: HookContext) -> None:
        if self._tm is None:
            return
        deferred = deferred_schemas(self._tm, self.eager_names)
        if not deferred:
            return  # 无 deferred → no-op(对齐 SkillHook 无 skill 时 no-op)
        entries = []
        for s in sorted(deferred, key=lambda x: x["function"]["name"]):
            name = s["function"]["name"]
            desc = s["function"].get("description", "") or ""
            brief = desc[:_DESC_LIMIT]
            entries.append(f"- {name}: {brief}")
        header = (
            "## 按需可见工具导航(不可直接调用)\n\n"
            "**重要提示:以下工具不在当前 tools 列表中,无法直接调用。**\n\n"
            "使用方法:\n"
            "1. **必须先**调用 `tools_search`,传入与导航列表一致的 `tool_name`,"
            "获取完整参数 schema。\n"
            "2. **然后**调用 `invoke_tool`,传入精确 `tool_name` 和根据 schema 构造的 `arguments`。\n\n"
            "**切勿直接调用以下工具——直接调用会失败。**\n\n"
        )
        content = header + "\n".join(entries)
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("tool_navigation", content, priority=_NAV_PRIORITY))

    async def before_model_call(self, ctx: HookContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not isinstance(tools, list):
            return
        inputs.tools = [
            t for t in tools
            if t.get("function", {}).get("name", "") in self.eager_names
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_tool_hook.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/progressive_tool_hook.py tests/test_progressive_tool_hook.py
git commit -m "feat(hooks): add ProgressiveToolHook (filter tools + inject navigation)"
```

---

## Task 5: builder + create_agent 接入 + 端到端集成 + 回归

**Files:**
- Create: `twinkle/agentserver/tools/progressive.py`(builder)
- Modify: `twinkle/agentserver/hooks/builtin/__init__.py`(导出 `ProgressiveToolHook`)
- Modify: `twinkle/agentserver/server.py` `create_agent`(接入 builder)
- Modify: `tests/test_progressive_tool_hook.py`(加端到端集成)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_progressive_tool_hook.py`:
```python
# --- 端到端集成:ReActAgent + ProgressiveToolHook + mock deferred 工具 --- #

from twinkle.agentserver.agent import ReActAgent, AgentRequest, normal_base_sections
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.progressive import apply_progressive_tools
from twinkle.config.schema import ProgressiveToolConfig


class _CapturingLLM:
    """捕获每步 system + tools;按 scripts 返回事件。"""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[str]] = []

    async def stream(self, messages, tools):
        self.seen_systems.append(messages[0]["content"])
        self.seen_tools.append([s["function"]["name"] for s in (tools or [])])
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _builtin_tm_with_deferred_mcp(tmp_path):
    """模拟 create_agent 的 tm:33 内置 + 1 mock MCP 工具(deferred)。"""
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()                       # 纯 33 内置
    # 模拟 MCP 灌入一个 deferred 工具
    @tool
    async def mcp_fake_query(sql: str) -> str:
        """fake mcp db query"""
        return f"rows:{sql}"
    tm.register(mcp_fake_query)
    return tm


def test_apply_disabled_returns_none_and_no_meta(tmp_path):
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=False))
    assert hook is None
    names = [t.card.name for t in tm.list()]
    assert "tools_search" not in names
    assert "invoke_tool" not in names


def test_apply_enabled_registers_meta_and_eager_has_all_builtin(tmp_path):
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    builtin_names = {t.card.name for t in tm.list()}
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=True))
    assert hook is not None
    names = [t.card.name for t in tm.list()]
    assert "tools_search" in names and "invoke_tool" in names
    # 默认 eager = 全内置 + meta(MCP 若有则不在)
    for name in builtin_names:
        assert name in hook.eager_names
    assert "tools_search" in hook.eager_names


def test_end_to_end_eager_filter_plus_navigation_and_invoke(tmp_path):
    tm = _builtin_tm_with_deferred_mcp(tmp_path)
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=True))
    assert hook is not None
    # 模型编排:step1 调 tools_search 找 mcp_fake_query;step2 调 invoke_tool 执行;step3 完成
    llm = _CapturingLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "tools_search",
                                           "arguments": '{"tool_name": "mcp_fake_query"}'}}]})],
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c2", "type": "function",
                              "function": {"name": "invoke_tool",
                                           "arguments": '{"tool_name": "mcp_fake_query", "arguments": {"sql": "SELECT 1"}}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = ReActAgent(llm, store, tm, hooks=(hook,),
                      base_sections=normal_base_sections(), max_steps=5)
    req = AgentRequest(session_id="s1", request_id="r1", query="query db")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    # 每步 tools 只含 eager + meta,deferred(mcp_fake_query)不暴露
    for step_tools in llm.seen_tools:
        assert "mcp_fake_query" not in step_tools
        assert "tools_search" in step_tools
        assert "invoke_tool" in step_tools
    # 导航在每步 system
    for sys_text in llm.seen_systems:
        assert "按需可见工具导航" in sys_text
        assert "mcp_fake_query" in sys_text
    # 跑了 3 步
    assert llm.calls == 3


def test_end_to_end_progressive_off_is_status_quo(tmp_path):
    """progressive 关闭 → 无 hook、无 meta-tool,行为等同现状(回归守卫)。"""
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=False))
    assert hook is None
    llm = _CapturingLLM([
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = ReActAgent(llm, store, tm, hooks=(),
                      base_sections=normal_base_sections(), max_steps=2)
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    step_tools = llm.seen_tools[0]
    assert "tools_search" not in step_tools      # 无 meta-tool
    assert "mcp_fake_query" not in step_tools
    # 无导航段
    assert "按需可见工具导航" not in llm.seen_systems[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_tool_hook.py::test_apply_disabled_returns_none_and_no_meta -v`
Expected: FAIL with `ImportError: cannot import name 'apply_progressive_tools'`

- [ ] **Step 3: Write minimal implementation**

Create `twinkle/agentserver/tools/progressive.py`:
```python
"""apply_progressive_tools — 渐进可见叠加层 builder。

接入 server.py create_agent(在 mcp.register_into 之后):
  hook = apply_progressive_tools(tools, settings.progressive_tool)
  if hook: all_hooks.append(hook)

enabled=False → 返回 None(no-op,行为等同现状)。
enabled=True  → 注册两 meta-tool + 返回 ProgressiveToolHook。
默认 eager = tool_manager() 的纯内置名 + meta(MCP 工具名不在 → 自动 deferred)。
"""
from __future__ import annotations

from typing import Any

from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.tools.builtin.progressive_tools import (
    META_NAMES, ToolsSearchTool, InvokeToolTool,
)


def apply_progressive_tools(tm: Any, config: Any):
    """启用渐进可见:注册 meta-tool + 返回 hook;否则返回 None。

    默认 eager 名单用 tool_manager() 的纯内置名(不含 MCP,因本函数在
    mcp.register_into 之后调用,tm 已含 MCP;用 tool_manager() 新建实例拿纯内置名,
    MCP 工具不在 → 自动落 deferred)。
    """
    if not getattr(config, "enabled", False):
        return None
    from twinkle.agentserver.tools import tool_manager
    builtin_names = {t.card.name for t in tool_manager()}   # 纯 33 内置
    eager = set(config.eager_tools) if config.eager_tools else builtin_names
    eager |= META_NAMES                                     # 强制 meta 进 eager
    tm.register(ToolsSearchTool(tm, eager))
    tm.register(InvokeToolTool(tm, eager))
    return ProgressiveToolHook(eager)
```

In `twinkle/agentserver/hooks/builtin/__init__.py`, add the export (follow the existing pattern, e.g. add alongside other hook imports):
```python
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
```
and add `ProgressiveToolHook` to `__all__` if the file maintains one.

In `twinkle/agentserver/server.py` `create_agent`, after line 76 (`get_mcp_manager().register_into(tools)`) and before `return ReActAgent(...)` (around line 111), insert into the `all_hooks` assembly. Add after the `all_hooks = list(hooks or []) + [...]` block and team hook append (around line 110):
```python
    # Progressive tool visibility (opt-in; default off = no-op)
    from twinkle.agentserver.tools.progressive import apply_progressive_tools
    progressive_hook = apply_progressive_tools(tools, settings.progressive_tool)
    if progressive_hook is not None:
        all_hooks.append(progressive_hook)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_tool_hook.py -v`
Expected: PASS (7 tests: 3 from Task 4 + 4 new)

- [ ] **Step 5: Run full regression suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — progressive 默认关,现有行为不变;若现有测试红,检查 `create_agent` 接入是否影响默认路径(enabled=False 应让 `apply_progressive_tools` 返回 None,不注册 meta-tool、不加 hook)。

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/tools/progressive.py twinkle/agentserver/hooks/builtin/__init__.py twinkle/agentserver/server.py tests/test_progressive_tool_hook.py
git commit -m "feat(tools): wire progressive tool visibility into create_agent (opt-in)"
```

---

## Self-Review

**1. Spec coverage:**
- §2 成功标准:progressive 关=现状(Task 5 `test_end_to_end_progressive_off_is_status_quo`);开+MCP=deferred 不暴露+tools_search 找到+invoke_tool 执行(Task 5 `test_end_to_end...`);prefix cache 稳定(导航 frozen_section 跨步套用 + eager 固定,现有 `test_frozen_sections_byte_stable` 机制保证)→ 覆盖。
- §3 范围:内置全 eager + MCP deferred(Task 5 apply 默认 eager);不碰 team/subagent(未触及);无 enable_for_models/阈值/向量/双语 → 覆盖。
- §4 架构:叠加层 + create_agent 接入(Task 5)→ 覆盖。
- §5.1 配置(Task 1);§5.2 两 meta-tool(Task 2/3);§5.3 hook(Task 4);§5.4 builder(Task 5)→ 覆盖。
- §5.2 修正:spec 写 meta-tool 返回 dict,实际对齐 `Tool.invoke -> str` 返回 `json.dumps` 字符串(Task 2/3 实现 + 测试用 `json.loads` 解析)→ 已在计划修正。
- §6 数据流(Task 5 端到端验证全链路)→ 覆盖。
- §7 错误处理:tools_search 未匹配(Task 2 `test_tools_search_miss_returns_guidance_message`);invoke_tool 拒绝 eager/未知(Task 3);meta-tool try/except(实现含)→ 覆盖。
- §8 测试 → 各 Task 测试覆盖。

**2. Placeholder scan:** 无 TBD/TODO;每步含完整代码/命令。`hooks/builtin/__init__.py` 导出 step 说"follow existing pattern"——该文件结构未逐行核实,执行时按现有 import 风格加入(非占位符,是明确指令)。

**3. Type consistency:** `ToolsSearchTool(tm, eager_names)` / `InvokeToolTool(tm, eager_names)` 构造签名 Task 2/3/5 一致;`ProgressiveToolHook(eager_names)` + `init(agent)` 取 `agent._tool_manager` Task 4/5 一致;`hook.eager_names` 属性 Task 5 测试访问与 Task 4 构造存 `self.eager_names` 一致;`deferred_schemas(tm, eager_names)` 模块函数 Task 2 定义、Task 4 hook import 复用、Task 3 InvokeToolTool 复用 → 一致。
