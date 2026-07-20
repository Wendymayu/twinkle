# Phase 2 — 工具系统成形 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `twinkle/agentserver/tools/` 从静态手写 schema 的 `ToolRegistry` 重写成 openjiuwen 风格的四层（ToolCard / Tool / LocalFunction / ToolManager）+ `@tool` 装饰器 + 最小手写 schema 抽取器，动态可注册，agent_loop 调用面零回炉。

**Architecture:** 自底向上构建——先纯数据 `ToolCard` + 接口 `Tool`，再 `schema_extractor`（纯函数），再 `LocalFunction`（实现 Tool），再 `@tool` 装饰器（拼 extractor + LocalFunction），最后 `ToolManager`（容器）。全部 TDD，每任务独立可测。最后一步把 `agent_loop`/`server` 从 `ToolRegistry` 切到 `ToolManager` 并删旧 `registry.py`，验收现有 `test_agent_loop`/`test_integration` 零逻辑改动下全绿。

**Tech Stack:** Python ≥3.11，stdlib（`inspect`/`typing`/`dataclasses`），pytest。无新依赖。

## Global Constraints

- 纯 stdlib，零新依赖（`inspect` / `typing` / `dataclasses`）。
- 工具返回 `str`（现有契约，agent_loop 直接 append 进 store）——任何工具的 `invoke` 必须返回 `str`。
- `ToolManager.execute` 必须保留现有错误契约：未知工具返回 `[error] unknown tool: {name}`；工具抛异常返回 `[tool error] {ExcType}: {msg}`——agent_loop 靠这个不崩。
- agent_loop 调用面 `self._tools.schemas()` 与 `self._tools.execute(name, args)` 签名不变。
- spec：`docs/superpowers/specs/2026-07-20-phase2-tool-system-design.md`（commit `67c243f`）。本计划是其实现，唯一细化：web_fetch/web_search 不在自身模块加 `@tool`，而在 `__init__.py` 用 `tool(web_fetch.web_fetch)` 注册——保留它们作为可单测的普通 async 函数，test_web_fetch/test_web_search 零改动。
- 测试运行命令：`.venv/Scripts/python.exe -m pytest -q`（Windows + Git Bash 环境）。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `twinkle/agentserver/tools/base.py` | `ToolCard` dataclass + `Tool` Protocol | 新建 |
| `twinkle/agentserver/tools/schema_extractor.py` | `extract(func) -> (name, description, parameters)` 纯函数 | 新建 |
| `twinkle/agentserver/tools/local_function.py` | `LocalFunction`（实现 Tool） | 新建 |
| `twinkle/agentserver/tools/decorator.py` | `@tool` / `tool()` / `tool(fn)` → `LocalFunction` | 新建 |
| `twinkle/agentserver/tools/manager.py` | `ToolManager`：register/unregister/list/get/schemas/execute | 新建 |
| `twinkle/agentserver/tools/__init__.py` | `build_default_manager()` + 导出 | 重写 |
| `twinkle/agentserver/tools/registry.py` | 旧 `ToolRegistry` | 删除 |
| `twinkle/agentserver/tools/web_fetch.py` | `web_fetch` async 函数 | 不变 |
| `twinkle/agentserver/tools/web_search.py` | `web_search` async 函数 | 不变 |
| `twinkle/agentserver/agent_loop.py` | `tools: ToolRegistry`→`ToolManager` 注解 | 改 1 import + 1 注解 |
| `twinkle/agentserver/server.py` | `build_default_registry`→`build_default_manager` | 改 2 行 |
| `tests/test_base.py` | ToolCard 构造 + Tool 结构性 | 新建 |
| `tests/test_schema_extractor.py` | 类型映射/required/default/docstring/兜底 | 新建 |
| `tests/test_local_function.py` | invoke 透传 | 新建 |
| `tests/test_tool_decorator.py` | 三种 @tool 用法 | 新建 |
| `tests/test_tool_manager.py` | register/unregister/list/get/schemas/execute 契约 | 新建（替代 test_tool_registry.py） |
| `tests/test_tool_registry.py` | 旧 | 删除 |
| `tests/test_agent_loop.py` | `_reg_with_echo_tool` 改用 `tool()` | 改 helper |
| `tests/test_integration.py` | `_reg_with_echo` 改用 `tool()` | 改 helper |
| `docs/architecture.md` | §4.5/§10 工具节同步 | 改 |

---

## Task 1: `base.py` — ToolCard + Tool 接口

**Files:**
- Create: `twinkle/agentserver/tools/base.py`
- Test: `tests/test_base.py`

**Interfaces:**
- Produces: `ToolCard(name: str, description: str, parameters: dict)` dataclass；`Tool` Protocol（`card: ToolCard` + `async def invoke(self, args: dict) -> str`）。后续所有任务的类型锚点。

- [ ] **Step 1: Write the failing test**

`tests/test_base.py`:
```python
from dataclasses import is_dataclass

from twinkle.agentserver.tools.base import Tool, ToolCard


def test_toolcard_is_dataclass_with_three_fields() -> None:
    c = ToolCard(name="echo", description="echoes", parameters={"type": "object"})
    assert is_dataclass(ToolCard)
    assert c.name == "echo"
    assert c.description == "echoes"
    assert c.parameters == {"type": "object"}


def test_tool_protocol_has_card_and_invoke() -> None:
    # Tool is a structural Protocol: any object with `card` + async `invoke` satisfies it.
    attrs = {n for n in dir(Tool) if not n.startswith("_")}
    assert "card" in Tool.__annotations__
    assert hasattr(Tool, "invoke")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.base'`

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/base.py`:
```python
"""Foundation layer: ToolCard (pure metadata) + Tool (interface).

Twinkle's four-layer tool model (aligned with openjiuwen
foundation/tool/base.py, cut to a minimal subset):
  ToolCard        — pure description data (name/description/parameters)
  Tool            — the interface any tool kind must satisfy (card + invoke)
  LocalFunction   — local-Python-function implementation of Tool
  ToolManager     — container of Tool, knows only the Tool interface
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolCard:
    name: str
    description: str
    parameters: dict  # OpenAI function-calling `parameters` JSON schema


class Tool(Protocol):
    """Any tool must expose its metadata card and an invoke entry point."""

    card: ToolCard

    async def invoke(self, args: dict) -> str: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_base.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/base.py tests/test_base.py
git commit -m "Phase 2: add ToolCard + Tool Protocol (base layer)"
```

---

## Task 2: `schema_extractor.py` — 最小手写抽取器

**Files:**
- Create: `twinkle/agentserver/tools/schema_extractor.py`
- Test: `tests/test_schema_extractor.py`

**Interfaces:**
- Consumes: `ToolCard`（from Task 1，仅用于组装返回值——实际上 extractor 不依赖 ToolCard，返回裸 tuple）
- Produces: `extract(func) -> tuple[str, str, dict]` 返回 `(name, description, parameters)`。纯函数无副作用。

- [ ] **Step 1: Write the failing test**

`tests/test_schema_extractor.py`:
```python
from typing import Optional

from twinkle.agentserver.tools.schema_extractor import extract


def _fn_basic(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its visible text."""
    return ""


def _fn_plain(a: str, b: int) -> str:
    return ""


def _fn_optional(a: str, b: Optional[int] = None) -> str:
    return ""


def _fn_list_optional(a: str, tags: Optional[list] = None) -> str:
    """Has a list param."""
    return ""


def _fn_nodecs(x: str) -> str:
    return ""


def _fn_floats(rate: float, enabled: bool = False) -> str:
    """A float and a bool."""
    return ""


def test_name_from_function_name() -> None:
    name, _, _ = extract(_fn_basic)
    assert name == "_fn_basic"


def test_description_from_docstring() -> None:
    _, desc, _ = extract(_fn_basic)
    assert desc == "Fetch a URL and return its visible text."


def test_description_empty_when_no_docstring() -> None:
    _, desc, _ = extract(_fn_nodecs)
    assert desc == ""


def test_required_is_params_without_defaults() -> None:
    _, _, params = extract(_fn_plain)
    assert params["required"] == ["a", "b"]
    assert params["type"] == "object"


def test_type_mapping_basic() -> None:
    _, _, params = extract(_fn_plain)
    props = params["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}


def test_default_value_recorded_and_not_required() -> None:
    _, _, params = extract(_fn_basic)
    props = params["properties"]
    assert props["url"] == {"type": "string"}
    assert props["max_chars"] == {"type": "integer", "default": 8000}
    assert params["required"] == ["url"]


def test_optional_unwrapped_and_not_required() -> None:
    _, _, params = extract(_fn_optional)
    props = params["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}  # Optional[int] -> integer, no default here
    assert params["required"] == ["a"]


def test_optional_list_maps_to_array() -> None:
    _, _, params = extract(_fn_list_optional)
    props = params["properties"]
    assert props["tags"] == {"type": "array"}


def test_float_and_bool_types() -> None:
    _, _, params = extract(_fn_floats)
    props = params["properties"]
    assert props["rate"] == {"type": "number"}
    assert props["enabled"] == {"type": "boolean", "default": False}
    assert params["required"] == ["rate"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schema_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.schema_extractor'`

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/schema_extractor.py`:
```python
"""Minimal hand-written schema extractor.

Turns a Python function's signature + docstring into the OpenAI
function-calling `parameters` JSON schema. Pure stdlib, ~50 lines.

Supported type mapping:
  str -> string, int -> integer, float -> number, bool -> boolean,
  list/List[...] -> array, dict/Dict[...] -> object (no properties).
  Optional[X] / X | None -> unwrap X, mark non-required.
Unknown types fall back to {"type": "string"}.

No per-param description parsing (YAGNI). Override via @tool(input_params=...).
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, get_args, get_origin, get_type_hints

_PRIMITIVE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_TYPES_NONE = (type(None),)


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """Return (inner_type, is_optional). Detects Optional[X] / X | None."""
    origin = get_origin(tp)
    if origin is typing.Union:
        args = [a for a in get_args(tp) if a not in _TYPES_NONE]
        is_optional = len(args) < len(get_args(tp))
        inner = args[0] if args else str
        return inner, is_optional
    return tp, False


def _type_to_schema(tp: Any) -> dict:
    inner, _ = _unwrap_optional(tp)
    if inner in _PRIMITIVE:
        return {"type": _PRIMITIVE[inner]}
    origin = get_origin(inner)
    if origin in (list, typing.List):
        return {"type": "array"}
    if origin in (dict, typing.Dict):
        return {"type": "object"}
    if inner is dict:
        return {"type": "object"}
    if inner is list:
        return {"type": "array"}
    return {"type": "string"}  # unknown -> safe fallback


def _description_from_docstring(func: Callable) -> str:
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    first_para = doc.split("\n\n")[0].strip()
    # collapse internal newlines to spaces
    return " ".join(first_para.split())


def extract(func: Callable) -> tuple[str, str, dict]:
    """Return (name, description, parameters) extracted from `func`."""
    name = func.__name__
    description = _description_from_docstring(func)

    hints = get_type_hints(func)
    sig = inspect.signature(func)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        tp = hints.get(pname, str)
        schema = _type_to_schema(tp)
        if param.default is not inspect.Parameter.empty:
            schema["default"] = param.default
        else:
            # Optional types (Optional[X] with no default) are not required.
            _, is_optional = _unwrap_optional(tp)
            if not is_optional:
                required.append(pname)
        properties[pname] = schema

    parameters = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return name, description, parameters
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schema_extractor.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/schema_extractor.py tests/test_schema_extractor.py
git commit -m "Phase 2: add minimal schema extractor"
```

---

## Task 3: `local_function.py` — 本地函数工具

**Files:**
- Create: `twinkle/agentserver/tools/local_function.py`
- Test: `tests/test_local_function.py`

**Interfaces:**
- Consumes: `ToolCard` (Task 1).
- Produces: `LocalFunction` dataclass with `card: ToolCard` + `func: Callable[..., Awaitable[str]]` + `async def invoke(self, args: dict) -> str` returning `await self.func(**args)`. Implements `Tool` Protocol structurally.

- [ ] **Step 1: Write the failing test**

`tests/test_local_function.py`:
```python
import asyncio
from dataclasses import is_dataclass

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.local_function import LocalFunction


async def _echo(text: str) -> str:
    return f"echo:{text}"


def test_localfunction_is_dataclass() -> None:
    assert is_dataclass(LocalFunction)


def test_invoke_passes_kwargs_and_returns_str() -> None:
    lf = LocalFunction(
        card=ToolCard(name="echo", description="", parameters={}),
        func=_echo,
    )
    out = asyncio.run(lf.invoke({"text": "hi"}))
    assert out == "echo:hi"


def test_localfunction_satisfies_tool_protocol() -> None:
    lf = LocalFunction(
        card=ToolCard(name="echo", description="", parameters={}),
        func=_echo,
    )
    assert lf.card.name == "echo"
    assert hasattr(lf, "invoke")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_local_function.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.local_function'`

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/local_function.py`:
```python
"""LocalFunction — the local-Python-function implementation of Tool.

Bundles a ToolCard (metadata) with a Callable (execution) and exposes a
single `invoke` entry point. This is one specific tool kind; future MCP
tools would be a sibling implementation of the same Tool interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from twinkle.agentserver.tools.base import ToolCard


@dataclass
class LocalFunction:
    card: ToolCard
    func: Callable[..., Awaitable[str]]

    async def invoke(self, args: dict) -> str:
        return await self.func(**args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_local_function.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/local_function.py tests/test_local_function.py
git commit -m "Phase 2: add LocalFunction (local-fn Tool impl)"
```

---

## Task 4: `decorator.py` — `@tool` 装饰器

**Files:**
- Create: `twinkle/agentserver/tools/decorator.py`
- Test: `tests/test_tool_decorator.py`

**Interfaces:**
- Consumes: `extract` (Task 2), `LocalFunction` (Task 3), `ToolCard` (Task 1).
- Produces: `tool(func=None, *, name=None, description=None, input_params=None) -> LocalFunction`. Supports `@tool`, `@tool()`, `@tool(name=, input_params=)`, and bare `tool(fn)`.

- [ ] **Step 1: Write the failing test**

`tests/test_tool_decorator.py`:
```python
import asyncio

from twinkle.agentserver.tools.decorator import tool


async def _fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL."""
    return url


@tool
async def _plain(url: str) -> str:
    """Plain decorated."""
    return url


@tool()
async def _called(url: str) -> str:
    """Called-no-args decorated."""
    return url


@tool(name="renamed", description="custom desc", input_params={"type": "object", "properties": {}, "required": []})
async def _override(url: str) -> str:
    return url


def test_bare_tool_returns_localfunction() -> None:
    assert _plain.card.name == "_plain"
    assert _plain.card.description == "Plain decorated."
    assert _plain.card.parameters["required"] == ["url"]


def test_called_no_args_returns_localfunction() -> None:
    assert _called.card.name == "_called"


def test_override_name_description_params() -> None:
    assert _override.card.name == "renamed"
    assert _override.card.description == "custom desc"
    assert _override.card.parameters == {"type": "object", "properties": {}, "required": []}


def test_bare_call_form_tool_fn() -> None:
    lf = tool(_fetch)
    assert lf.card.name == "_fetch"
    assert lf.card.parameters["required"] == ["url"]
    assert lf.card.parameters["properties"]["max_chars"] == {"type": "integer", "default": 8000}


def test_decorated_function_invokable_via_invoke() -> None:
    out = asyncio.run(_plain.invoke({"url": "u"}))
    assert out == "u"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tool_decorator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.decorator'`

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/decorator.py`:
```python
"""@tool decorator — converts a plain async function into a LocalFunction.

Usage:
  @tool                       # bare
  @tool()                     # called, no args
  @tool(name=..., input_params=...)   # override
  tool(fn)                    # non-decorator form (used by build_default_manager)
"""
from __future__ import annotations

from typing import Callable, Optional

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.schema_extractor import extract


def tool(
    func: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    input_params: Optional[dict] = None,
) -> LocalFunction:
    def _build(f: Callable) -> LocalFunction:
        ex_name, ex_desc, ex_params = extract(f)
        card = ToolCard(
            name=name if name is not None else ex_name,
            description=description if description is not None else ex_desc,
            parameters=input_params if input_params is not None else ex_params,
        )
        return LocalFunction(card=card, func=f)

    if func is not None:
        return _build(func)
    return _build  # type: ignore[return-value]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tool_decorator.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/decorator.py tests/test_tool_decorator.py
git commit -m "Phase 2: add @tool decorator"
```

---

## Task 5: `manager.py` — ToolManager

**Files:**
- Create: `twinkle/agentserver/tools/manager.py`
- Create: `tests/test_tool_manager.py`
- Delete: `tests/test_tool_registry.py` (replaced)

**Interfaces:**
- Consumes: `Tool` (Task 1), `LocalFunction`/`tool` (Tasks 3/4).
- Produces: `ToolManager` with `register(tool: Tool)` / `unregister(name) -> bool` / `get(name) -> Tool | None` / `list() -> list[Tool]` / `schemas() -> list[dict]` / `async execute(name, args) -> str`. `schemas()` 产 OpenAI function-calling 格式。`execute` 错误契约同旧 ToolRegistry。

- [ ] **Step 1: Write the failing test (the evolved registry tests + new dynamic ones)**

`tests/test_tool_manager.py`:
```python
import asyncio

from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


async def _echo(text: str) -> str:
    """echo back text"""
    return f"echo:{text}"


def _make_manager() -> ToolManager:
    m = ToolManager()
    m.register(tool(_echo))
    return m


def test_schemas_are_openai_function_defs() -> None:
    m = _make_manager()
    schemas = m.schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "_echo",
                "description": "echo back text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_unknown_tool_returns_error_string() -> None:
    m = _make_manager()
    assert asyncio.run(m.execute("nope", {})) == "[error] unknown tool: nope"


def test_execute_passes_kwargs() -> None:
    m = _make_manager()
    assert asyncio.run(m.execute("_echo", {"text": "hi"})) == "echo:hi"


def test_execute_swallows_tool_exception_as_error_string() -> None:
    async def _boom(x: str) -> str:
        raise ValueError("boom")
    m = ToolManager()
    m.register(tool(_boom))
    out = asyncio.run(m.execute("_boom", {"x": "1"}))
    assert out == "[tool error] ValueError: boom"


def test_unregister_returns_true_when_present_false_when_absent() -> None:
    m = _make_manager()
    assert m.unregister("_echo") is True
    assert m.unregister("_echo") is False
    assert m.get("_echo") is None


def test_list_returns_all_registered() -> None:
    m = _make_manager()
    assert [t.card.name for t in m.list()] == ["_echo"]


def test_dynamic_register_visible_in_schemas_immediately() -> None:
    async def _later(n: int) -> str:
        """later"""
        return str(n)
    m = _make_manager()
    m.register(tool(_later))
    names = {s["function"]["name"] for s in m.schemas()}
    assert names == {"_echo", "_later"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tool_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.manager'`

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/manager.py`:
```python
"""ToolManager — container of Tool. Knows only the Tool interface.

Aligned with openjiuwen core/single_agent/ability_manager.py, cut to a
minimal subset: register/unregister/list/get/schemas/execute. No catalog()
(YAGNI — list() covers enumeration, schemas() covers the model view).
"""
from __future__ import annotations

from twinkle.agentserver.tools.base import Tool


class ToolManager:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.card.name] = tool

    def unregister(self, name: str) -> bool:
        existed = name in self._tools
        if existed:
            del self._tools[name]
        return existed

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.card.name,
                    "description": t.card.description,
                    "parameters": t.card.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, args: dict) -> str:
        t = self._tools.get(name)
        if t is None:
            return f"[error] unknown tool: {name}"
        try:
            return await t.invoke(args)
        except Exception as exc:  # tool failures must not crash the loop
            return f"[tool error] {type(exc).__name__}: {exc}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tool_manager.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Delete obsolete test file**

```bash
git rm tests/test_tool_registry.py
```

- [ ] **Step 6: Run full suite to confirm nothing else broke yet**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: FAIL — `test_agent_loop.py` and `test_integration.py` still import the OLD `ToolRegistry` from `registry.py` (still exists), so they PASS; the suite should be GREEN except possibly the deleted-file references are already gone. Confirm: all remaining tests PASS (22 original minus test_tool_registry's 3, plus new ones). The old `registry.py` still exists so test_agent_loop/test_integration still import fine.

> Note: `registry.py` is NOT deleted in this task — it stays so agent_loop/tests keep working. Task 6 swaps the imports and deletes `registry.py`.

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/tools/manager.py tests/test_tool_manager.py
git commit -m "Phase 2: add ToolManager + test_tool_manager; remove test_tool_registry"
```

---

## Task 6: Wire `agent_loop`/`server`/`__init__` to ToolManager; delete `registry.py`

**Files:**
- Rewrite: `twinkle/agentserver/tools/__init__.py`
- Modify: `twinkle/agentserver/agent_loop.py:16,27`
- Modify: `twinkle/agentserver/server.py:20,48`
- Delete: `twinkle/agentserver/tools/registry.py`
- New test: `tests/test_default_manager.py`

**Interfaces:**
- Consumes: `ToolManager` (Task 5), `tool` (Task 4), `web_fetch`/`web_search` (unchanged).
- Produces: `build_default_manager() -> ToolManager` registered with `web_fetch` + `web_search`. Exports `ToolManager`, `LocalFunction`, `tool`, `build_default_manager`. agent_loop now types `tools: ToolManager`.

- [ ] **Step 1: Write the failing test for the default manager**

`tests/test_default_manager.py`:
```python
from twinkle.agentserver.tools import build_default_manager


def test_default_manager_registers_web_fetch_and_web_search() -> None:
    m = build_default_manager()
    names = {t.card.name for t in m.list()}
    assert names == {"web_fetch", "web_search"}


def test_default_manager_schemas_have_required_url_or_query() -> None:
    m = build_default_manager()
    by_name = {s["function"]["name"]: s for s in m.schemas()}
    assert by_name["web_fetch"]["function"]["parameters"]["required"] == ["url"]
    assert by_name["web_search"]["function"]["parameters"]["required"] == ["query"]
    assert by_name["web_fetch"]["function"]["parameters"]["properties"]["max_chars"] == {
        "type": "integer", "default": 8000
    }
    assert by_name["web_search"]["function"]["parameters"]["properties"]["max_results"] == {
        "type": "integer", "default": 5
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_default_manager.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_default_manager'` (or similar)

- [ ] **Step 3: Rewrite `__init__.py`**

`twinkle/agentserver/tools/__init__.py` (full replacement):
```python
"""AgentServer tools package + default manager builder."""
from __future__ import annotations

from twinkle.agentserver.tools import web_fetch, web_search
from twinkle.agentserver.tools.base import Tool, ToolCard
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.manager import ToolManager


def build_default_manager() -> ToolManager:
    """Register the default read-only tools via the @tool decorator."""
    m = ToolManager()
    m.register(tool(web_fetch.web_fetch))
    m.register(tool(web_search.web_search))
    return m


__all__ = [
    "Tool",
    "ToolCard",
    "LocalFunction",
    "tool",
    "ToolManager",
    "build_default_manager",
]
```

- [ ] **Step 4: Swap agent_loop import + type hint**

In `twinkle/agentserver/agent_loop.py`:
- Line 16: change `from twinkle.agentserver.tools.registry import ToolRegistry` → `from twinkle.agentserver.tools.manager import ToolManager`
- Line 27: change `tools: ToolRegistry,` → `tools: ToolManager,`

No other change to agent_loop. The `self._tools.schemas()` / `self._tools.execute(...)` calls are unchanged.

- [ ] **Step 5: Swap server.py**

In `twinkle/agentserver/server.py`:
- Line 20: change `from twinkle.agentserver.tools import build_default_registry` → `from twinkle.agentserver.tools import build_default_manager`
- Line 48: change `tools = build_default_registry()` → `tools = build_default_manager()`

- [ ] **Step 6: Delete obsolete registry.py**

```bash
git rm twinkle/agentserver/tools/registry.py
```

- [ ] **Step 7: Run new test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_default_manager.py -v`
Expected: PASS (2 tests)

- [ ] **Step 8: Run full suite — expect TWO failures (the test helpers still use old ToolRegistry)**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: FAIL — `tests/test_agent_loop.py` and `tests/test_integration.py` import `from twinkle.agentserver.tools.registry import ToolRegistry` which no longer exists. These are fixed in Task 7.

- [ ] **Step 9: Commit (suite red on old-test-helpers, fixed next task)**

```bash
git add -A
git commit -m "Phase 2: wire agent_loop/server to ToolManager; add build_default_manager; delete registry.py"
```

> Note: This task intentionally leaves `test_agent_loop.py`/`test_integration.py` red because they still import the deleted `registry` module. Task 7 migrates those helpers to `tool()`.

---

## Task 7: Migrate test helpers to `tool()`; zero-regression verification

**Files:**
- Modify: `tests/test_agent_loop.py:8,34-46`
- Modify: `tests/test_integration.py:19,38-49`
- Modify: `docs/architecture.md` (§4.5, §10, §11)

**Interfaces:**
- Consumes: `tool` decorator (Task 4), `ToolManager` (Task 5).

- [ ] **Step 1: Migrate `tests/test_agent_loop.py` echo helper**

Replace line 8 import:
`from twinkle.agentserver.tools.registry import ToolRegistry` → `from twinkle.agentserver.tools.decorator import tool`

Replace `_reg_with_echo_tool()` (lines 34-46) with:
```python
def _reg_with_echo_tool():
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    m = ToolManager()
    m.register(echo)
    return m
```

> Note: `test_agent_loop.py`'s assertions do not reference the registry type — they only rely on `reg.schemas()` and `reg.execute(name, args)`, both of which `ToolManager` provides with identical signatures. This is the zero-回炉 acceptance: agent_loop's logic is untouched, only the tool-container type changed.

- [ ] **Step 2: Migrate `tests/test_integration.py` echo helper**

Replace line 19 import:
`from twinkle.agentserver.tools.registry import ToolRegistry` → `from twinkle.agentserver.tools.decorator import tool`

Replace `_reg_with_echo()` (lines 38-49) with:
```python
def _reg_with_echo():
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"TOOL:{text}"

    m = ToolManager()
    m.register(echo)
    return m
```

- [ ] **Step 3: Run full suite to verify zero-regression green**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — all tests green. Count = (original 22) − (deleted test_tool_registry 3) + (test_base 2) + (test_schema_extractor 8) + (test_local_function 3) + (test_tool_decorator 5) + (test_tool_manager 7) + (test_default_manager 2) = 44 passing. (Exact count may vary by fixtures; the key assertion is **zero failures**.)

- [ ] **Step 4: Update architecture doc §4.5 (ToolRegistry → ToolManager)**

In `docs/architecture.md`, find §4.5 "ToolRegistry — 最小版工具管理" and replace its content to reflect the four-layer model:

Replace the §4.5 block (the lines describing `tools/registry.py` static registration of web_fetch/web_search) with:

```markdown
### 4.5 ToolManager — 四层工具系统（Phase 2）

[twinkle/agentserver/tools/](../twinkle/agentserver/tools/) 重写为 openjiuwen 风格四层：

- `base.py` — `ToolCard`（纯元数据：name/description/parameters）+ `Tool`（Protocol 接口：card + invoke）
- `local_function.py` — `LocalFunction`（本地 Python 函数这一种 Tool 实现）
- `decorator.py` — `@tool` 装饰器：函数 + docstring + 签名自动抽 schema 产 LocalFunction
- `schema_extractor.py` — 最小手写抽取器（str/int/float/bool/list/dict/Optional → JSON schema）
- `manager.py` — `ToolManager`：register/unregister/list/get/schemas/execute，存 `dict[str, Tool]`，只认 Tool 接口

agent_loop 调用面 `self._tools.schemas()` / `self._tools.execute(name, args)` 不变——ToolManager 是旧 ToolRegistry 的超集。
```

- [ ] **Step 5: Update architecture doc §10 module tree**

In `docs/architecture.md` §10, replace the `tools/` subtree block:

```
    tools/
      base.py              # ToolCard + Tool(Protocol)
      schema_extractor.py  # 签名/docstring → JSON schema
      local_function.py    # LocalFunction（本地函数 Tool 实现）
      decorator.py         # @tool 装饰器
      manager.py           # ToolManager（容器，存 dict[str, Tool]）
      web_fetch.py         # URL → markdown/文本
      web_search.py        # DuckDuckGo Lite 搜索
```

And in the `tests/` list replace `test_tool_registry.py` with `test_tool_manager.py`, add `test_base.py`, `test_schema_extractor.py`, `test_local_function.py`, `test_tool_decorator.py`, `test_default_manager.py`.

- [ ] **Step 6: Update architecture doc §11 jiuwenclaw 对照 row**

In §11 table, replace the row for tools:
`| agentserver/tools/manager.py (ToolManager, 4层) | agentserver/deep_agent + tools/tool_manager.py | 最小 ReAct 时砍 todo；Phase 2 重写为 openjiuwen 四层 |`

Actually: keep the existing agent_loop row; add a new row:
`| tools/{base,local_function,decorator,schema_extractor,manager} | openjiuwen foundation/tool/* + ability_manager.py | 四层最小子集，砍 MCP/Input/Output/触发器 |`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Phase 2: migrate test helpers to @tool; zero-regression green; sync architecture doc"
```

---

## Self-Review (run after writing — done inline)

**1. Spec coverage:**
- §1 范围/不做 → Task 1-7 cover 四层+抽取器+@tool+ToolManager+动态注册；browser RPC/MCP/concurrency 不做（计划无对应任务，正确）；plan_todo 不做（拆 Phase 2b，正确）。✓
- §3 四层结构 → Task 1 (ToolCard+Tool), Task 3 (LocalFunction), Task 5 (ToolManager). ✓
- §4 模块结构 → File Structure table matches exactly. ✓
- §5.1-5.6 各模块 → Task 1/2/3/4/5/6. ✓
- §6 迁移+零回炉 → Task 6 (wire) + Task 7 (migrate helpers + zero-regression). ✓ (细化: web_fetch/web_search 不在自身加 @tool，在 __init__ 注册——已在 Global Constraints 注明。)
- §7 测试 → test_base/test_schema_extractor/test_local_function/test_tool_decorator/test_tool_manager + 零回炉验收. catalog 测试已删（spec §5.5 砍 catalog）. test_default_manager 覆盖 M3"多工具正确选择"的前半（schemas 含 ≥2 工具 + 动态 register 立刻可见）. ✓
- §8 对照表 → architecture doc §11 更新. ✓

**2. Placeholder scan:** No TBD/TODO. All code blocks complete. ✓

**3. Type consistency:**
- `ToolCard(name, description, parameters)` consistent across Tasks 1/3/4. ✓
- `extract(func) -> tuple[str, str, dict]` consistent Tasks 2/4. ✓
- `LocalFunction(card, func)` + `invoke(args)->str` consistent Tasks 3/4/5. ✓
- `ToolManager.register(tool: Tool)` / `unregister(name)->bool` / `get(name)->Tool|None` / `list()->list[Tool]` / `schemas()->list[dict]` / `execute(name, args)->str` consistent Tasks 5/6/7. ✓
- `tool(func=None, *, name, description, input_params)` consistent Tasks 4/6/7. ✓
- Error contract strings `[error] unknown tool: {name}` and `[tool error] {Type}: {msg}` match between Task 5 impl and the old test_tool_registry assertions carried into test_tool_manager. ✓

No issues found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-phase2-tool-system.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
