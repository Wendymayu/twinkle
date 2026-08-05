# Phase 11a — Workflow 引擎 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Workflow 引擎（PlanNode 基类 + WorkflowExecutor + 安全沙箱 + AST 校验 + fallback + `execute_workflow` 工具），使 Twinkle 具备代码驱动的确定性编排能力。

**Architecture:** 参考 jiuwenswarm SkillTurbo 模块，移植 PlanNode / Validator / Sandbox / json_utils，简化掉流式/日志/trace/resume/并发限流。Workflow 作为 `@tool` 注册到 AgentLoop，LLM 自主选择调用。失败时通过 SubagentExecutor 兜底。

**Tech Stack:** Python 3.11+ / asyncio / pydantic / ast（标准库）

## Global Constraints

- 不使用 `pytest-asyncio`——用 `asyncio.run()` + `free_port`/`port_factory` fixtures（`tests/conftest.py`）
- 配置类继承 `_StrictModel`（`twinkle/config/schema.py`），`extra="forbid"`
- 工具注册在 `tool_manager()` 中（`twinkle/agentserver/tools/__init__.py`）
- Hook 注册在 `build_agent_loop()` 中（`twinkle/agentserver/server.py`）
- `AbortError` → `HookInterrupt`（`twinkle/agentserver/hooks/base.py`）
- LLM 调用模式参照 `compression._summarize()`（`twinkle/agentserver/compression/__init__.py`）：`llm.stream(messages, tools=[])` + 收集 TextDelta

---

## File Structure

```
twinkle/agentserver/workflow/
    __init__.py           # 包导出
    node.py               # PlanNode ABC
    executor.py           # WorkflowExecutor
    validator.py           # PlanCodeValidator AST 校验
    json_utils.py          # extract_llm_json
    sandbox.py             # _SAFE_BUILTINS + 安全命名空间
    context.py             # ContextVar 桥接
    tools.py               # execute_workflow @tool 入口

tests/
    test_plan_node.py
    test_workflow_executor.py
    test_plan_code_validator.py
    test_plan_json_utils.py
    test_plan_sandbox.py
```

---

### Task 1: json_utils — 从 jiuwenswarm 移植 extract_llm_json

**Files:**
- Create: `twinkle/agentserver/workflow/json_utils.py`
- Create: `tests/test_plan_json_utils.py`

**Interfaces:**
- Produces: `extract_llm_json(raw: str | dict | list, expected_type: type = dict) -> Any`

这是零依赖的纯函数模块，先移植先测试，后续 Task 5（PlanNode.extract_json）和 Task 6（WorkflowExecutor）会用到。

- [ ] **Step 1: Write failing tests for extract_llm_json**

```python
# tests/test_plan_json_utils.py
import pytest
from twinkle.agentserver.workflow.json_utils import extract_llm_json


def test_extract_dict_passthrough():
    """Already a dict -> return as-is."""
    data = {"key": "value"}
    assert extract_llm_json(data) == data


def test_extract_list_passthrough():
    """Already a list -> return as-is."""
    data = [1, 2, 3]
    assert extract_llm_json(data, expected_type=list) == data


def test_extract_pure_json_string():
    """Plain JSON string -> parse and return."""
    raw = '{"name": "test", "count": 5}'
    assert extract_llm_json(raw) == {"name": "test", "count": 5}


def test_extract_json_code_block():
    """```json ... ``` code block -> extract and parse."""
    raw = 'Here is the result:\n```json\n{"result": 42}\n```\nDone.'
    assert extract_llm_json(raw) == {"result": 42}


def test_extract_json_code_block_no_lang():
    """``` ... ``` code block without 'json' label -> extract and parse."""
    raw = '```\n{"result": 42}\n```'
    assert extract_llm_json(raw) == {"result": 42}


def test_extract_embedded_json():
    """JSON embedded in text -> bracket counting extraction."""
    raw = 'The result is {"x": 1, "y": 2} as expected.'
    assert extract_llm_json(raw) == {"x": 1, "y": 2}


def test_extract_list_from_text():
    """List embedded in text -> bracket counting extraction."""
    raw = 'Items: [1, 2, 3] done.'
    assert extract_llm_json(raw, expected_type=list) == [1, 2, 3]


def test_extract_raises_on_invalid():
    """No valid JSON -> ValueError."""
    with pytest.raises(ValueError, match="无法从LLM输出中解析JSON"):
        extract_llm_json("no json here at all")


def test_extract_raises_on_wrong_type():
    """Valid JSON but wrong type -> ValueError."""
    raw = '[1, 2, 3]'
    with pytest.raises(ValueError, match="dict"):
        extract_llm_json(raw, expected_type=dict)


def test_extract_nested_json():
    """Nested JSON -> extract outermost."""
    raw = 'Result: {"outer": {"inner": 1}, "list": [1, 2]}'
    assert extract_llm_json(raw) == {"outer": {"inner": 1}, "list": [1, 2]}


def test_extract_json_with_escaped_quotes():
    """JSON with escaped quotes in string values."""
    raw = '{"text": "He said \\"hello\\""}'
    assert extract_llm_json(raw) == {"text": 'He said "hello"'}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plan_json_utils.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Create package init and json_utils module**

```python
# twinkle/agentserver/workflow/__init__.py
"""Workflow engine — code-driven deterministic orchestration."""
```

```python
# twinkle/agentserver/workflow/json_utils.py
"""JSON extraction from LLM output — ported from jiuwenswarm skill_turbo.json_utils."""
from __future__ import annotations

import json
import re
from typing import Any, Union


def extract_llm_json(
    raw: Union[str, dict, list],
    expected_type: type = dict,
) -> Any:
    """从LLM返回值中健壮地提取JSON。

    兼容四种返回形态：
      1. 已经是 dict/list（Agent 直接返回结构化对象）
      2. 纯 JSON 字符串
      3. ```json ... ``` 包裹的字符串
      4. 夹杂文本的响应（用括号计数法提取第一个完整 JSON 结构）
    """
    if isinstance(raw, expected_type):
        return raw
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"LLM返回了未预期的类型: {type(raw)}")

    first_error: json.JSONDecodeError | None = None
    try:
        result = json.loads(raw)
        if isinstance(result, expected_type):
            return result
        first_error = None
    except json.JSONDecodeError as e:
        first_error = e

    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        try:
            result = json.loads(code_block.group(1).strip())
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass

    open_char = "[" if expected_type == list else "{"
    close_char = "]" if expected_type == list else "}"
    candidate = _extract_outermost_json(raw, open_char, close_char)
    if candidate is not None:
        try:
            result = json.loads(candidate)
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass

    if first_error is not None:
        context_start = max(0, first_error.pos - 80)
        context_end = min(len(raw), first_error.pos + 80)
        error_context = raw[context_start:context_end].replace("\n", "\\n")
        raise ValueError(
            f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）："
            f"{first_error.msg}（第{first_error.lineno}行第{first_error.colno}列）。"
            f"出错位置附近：...{error_context}..."
        )
    raise ValueError(
        f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）：{raw[:300]}"
    )


def _extract_outermost_json(text: str, open_char: str, close_char: str) -> str | None:
    """括号计数法提取最外层完整的JSON结构。"""
    depth = 0
    start_idx = -1
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == close_char:
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    return text[start_idx : i + 1]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_plan_json_utils.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/workflow/__init__.py twinkle/agentserver/workflow/json_utils.py tests/test_plan_json_utils.py
git commit -m "feat(workflow): port extract_llm_json from jiuwenswarm"
```

---

### Task 2: PlanCodeValidator — AST 校验

**Files:**
- Create: `twinkle/agentserver/workflow/validator.py`
- Create: `tests/test_plan_code_validator.py`

**Interfaces:**
- Consumes: `HookInterrupt` from `twinkle/agentserver/hooks/base.py`
- Produces: `PlanCodeValidator.validate(plan_code: str) -> list[str]`（返回错误列表，空=通过）

Validator 在 `exec(plan_code)` 之前做 AST 级别的静态检查，防止危险代码执行。

- [ ] **Step 1: Write failing tests for PlanCodeValidator**

```python
# tests/test_plan_code_validator.py
import pytest
from twinkle.agentserver.workflow.validator import PlanCodeValidator


@pytest.fixture
def validator():
    return PlanCodeValidator()


def test_valid_import_only(validator):
    """Only 'from ... import' is allowed."""
    code = "from twinkle.agentserver.workflow.node import PlanNode"
    assert validator.validate(code) == []


def test_reject_bare_import(validator):
    """Bare 'import x' is rejected."""
    code = "import os"
    errors = validator.validate(code)
    assert len(errors) > 0
    assert any("import" in e for e in errors)


def test_reject_exec_call(validator):
    """exec() is forbidden."""
    code = "from twinkle.agentserver.workflow.node import PlanNode\nexec('hello')"
    errors = validator.validate(code)
    assert len(errors) > 0
    assert any("exec" in e for e in errors)


def test_reject_eval_call(validator):
    """eval() is forbidden."""
    code = "from twinkle.agentserver.workflow.node import PlanNode\neval('1+1')"
    errors = validator.validate(code)
    assert len(errors) > 0
    assert any("eval" in e for e in errors)


def test_reject_open_call(validator):
    """open() is forbidden."""
    code = "from twinkle.agentserver.workflow.node import PlanNode\nopen('/etc/passwd')"
    errors = validator.validate(code)
    assert len(errors) > 0
    assert any("open" in e for e in errors)


def test_reject_os_import(validator):
    """import os is forbidden."""
    code = "import os"
    errors = validator.validate(code)
    assert len(errors) > 0


def test_reject_subprocess_import(validator):
    """import subprocess is forbidden."""
    code = "import subprocess"
    errors = validator.validate(code)
    assert len(errors) > 0


def test_valid_from_import_allowed_prefix(validator):
    """from twinkle.agentserver.workflow... import is allowed."""
    code = "from twinkle.agentserver.workflow.node import PlanNode"
    assert validator.validate(code) == []


def test_reject_dunder_access(validator):
    """Accessing __import__ is forbidden."""
    code = "from twinkle.agentserver.workflow.node import PlanNode\nx = __import__"
    errors = validator.validate(code)
    assert len(errors) > 0


def test_reject_getattr_call(validator):
    """getattr() is forbidden."""
    code = "from twinkle.agentserver.workflow.node import PlanNode\ngetattr(obj, 'x')"
    errors = validator.validate(code)
    assert len(errors) > 0


def test_syntax_error_in_code(validator):
    """Syntax errors in plan_code are reported."""
    code = "this is not valid python {{{"
    errors = validator.validate(code)
    assert len(errors) > 0


def test_empty_code(validator):
    """Empty code passes validation (no dangerous constructs)."""
    assert validator.validate("") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plan_code_validator.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement PlanCodeValidator**

```python
# twinkle/agentserver/workflow/validator.py
"""PlanCodeValidator — AST-level static checks before exec(plan_code)."""
from __future__ import annotations

import ast
from typing import Any


# Forbidden function calls (dangerous builtins)
_FORBIDDEN_CALLS = frozenset({
    "exec", "eval", "compile", "open", "input",
    "getattr", "setattr", "delattr", "type",
    "__import__", "globals", "locals", "vars", "dir",
})

# Forbidden module names (bare import)
_FORBIDDEN_MODULES = frozenset({
    "os", "sys", "subprocess", "shutil", "signal",
    "ctypes", "socket", "http", "urllib", "asyncio.subprocess",
})

# Allowed import prefixes (from ... import only)
_ALLOWED_IMPORT_PREFIXES = ("twinkle.agentserver.workflow",)

# Forbidden dunder names
_FORBIDDEN_DUNDER = frozenset({
    "__import__", "__builtins__", "__code__",
    "__globals__", "__locals__", "__dict__",
})


class PlanCodeValidator:
    """AST-level validator for plan_code before exec().

    Checks:
    1. Only 'from ... import' allowed (no bare 'import x')
    2. from-imports must start with allowed prefix
    3. Forbidden function calls (exec, eval, open, getattr, etc.)
    4. Forbidden dunder access (__import__, __builtins__, etc.)
    """

    def validate(self, plan_code: str) -> list[str]:
        """Validate plan_code. Returns list of error strings (empty = pass)."""
        errors: list[str] = []
        try:
            tree = ast.parse(plan_code)
        except SyntaxError as e:
            return [f"语法错误: {e.msg} (line {e.lineno})"]

        for node in ast.walk(tree):
            # Check bare imports: import os
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_module = alias.name.split(".")[0]
                    if root_module in _FORBIDDEN_MODULES:
                        errors.append(f"禁止导入模块: {alias.name}")
                    else:
                        errors.append(f"只允许 from ... import 形式，禁止裸 import: {alias.name}")

            # Check from-imports
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    errors.append("禁止相对导入")
                    continue
                root_module = node.module.split(".")[0]
                if root_module in _FORBIDDEN_MODULES:
                    errors.append(f"禁止导入模块: {node.module}")
                # Check fromlist for dangerous names
                if node.names:
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_DUNDER:
                            errors.append(f"禁止导入: {alias.name}")

            # Check function calls
            elif isinstance(node, ast.Call):
                func_name = self._get_call_name(node.func)
                if func_name in _FORBIDDEN_CALLS:
                    errors.append(f"禁止调用: {func_name}()")

            # Check dunder attribute access
            elif isinstance(node, ast.Attribute):
                if node.attr in _FORBIDDEN_DUNDER:
                    errors.append(f"禁止访问: .{node.attr}")

            # Check dunder name access
            elif isinstance(node, ast.Name):
                if node.id in _FORBIDDEN_DUNDER:
                    errors.append(f"禁止访问: {node.id}")

        return errors

    def _get_call_name(self, node: ast.expr) -> str:
        """Extract function name from Call node (simple name only)."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_plan_code_validator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/workflow/validator.py tests/test_plan_code_validator.py
git commit -m "feat(workflow): add PlanCodeValidator AST checks"
```

---

### Task 3: Sandbox — 安全命名空间

**Files:**
- Create: `twinkle/agentserver/workflow/sandbox.py`
- Create: `tests/test_plan_sandbox.py`

**Interfaces:**
- Consumes: `PlanNode` from Task 5（用字符串引用避免循环依赖）
- Produces: `_SAFE_BUILTINS: dict`, `build_namespace() -> dict`, `safe_import() -> Any`

Sandbox 为 `exec(plan_code, namespace)` 提供隔离的命名空间，限制可用的内置函数和 import。

- [ ] **Step 1: Write failing tests for sandbox**

```python
# tests/test_plan_sandbox.py
import pytest
from twinkle.agentserver.workflow.sandbox import _SAFE_BUILTINS, build_namespace


def test_safe_builtins_has_len():
    assert "len" in _SAFE_BUILTINS


def test_safe_builtins_no_open():
    assert "open" not in _SAFE_BUILTINS


def test_safe_builtins_no_exec():
    assert "exec" not in _SAFE_BUILTINS


def test_safe_builtins_no_eval():
    assert "eval" not in _SAFE_BUILTINS


def test_safe_builtins_no_getattr():
    assert "getattr" not in _SAFE_BUILTINS


def test_build_namespace_has_plan_node():
    ns = build_namespace()
    assert "PlanNode" in ns


def test_build_namespace_has_hook_interrupt():
    ns = build_namespace()
    assert "HookInterrupt" in ns


def test_build_namespace_replaces_builtins():
    ns = build_namespace()
    assert "__builtins__" in ns
    assert isinstance(ns["__builtins__"], dict)
    assert "open" not in ns["__builtins__"]


def test_exec_in_sandbox_cannot_import_os():
    ns = build_namespace()
    with pytest.raises(ImportError):
        exec("import os", ns)


def test_exec_in_sandbox_cannot_import_subprocess():
    ns = build_namespace()
    with pytest.raises(ImportError):
        exec("import subprocess", ns)


def test_exec_in_sandbox_can_use_safe_builtins():
    ns = build_namespace()
    exec("result = len([1, 2, 3])", ns)
    assert ns["result"] == 3


def test_exec_in_sandbox_cannot_open_file():
    ns = build_namespace()
    with pytest.raises(NameError):
        exec("open('/etc/passwd')", ns)


def test_exec_in_sandbox_cannot_exec():
    ns = build_namespace()
    with pytest.raises(NameError):
        exec("exec('print(1)')", ns)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plan_sandbox.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement sandbox**

```python
# twinkle/agentserver/workflow/sandbox.py
"""Sandbox — safe namespace for exec(plan_code) isolation."""
from __future__ import annotations

import importlib
from typing import Any

# Safe builtins: ~40 safe functions, no open/exec/eval/getattr
_SAFE_BUILTINS: dict[str, Any] = {
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "chr": chr, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "hex": hex, "int": int,
    "isinstance": isinstance, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "oct": oct,
    "ord": ord, "range": range, "repr": repr, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip,
    # Exception classes (needed for try/except in skill code)
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError, "StopIteration": StopIteration,
    "AttributeError": AttributeError,
}

# Forbidden top-level modules
_FORBIDDEN_MODULES = frozenset({
    "os", "sys", "subprocess", "shutil", "signal",
    "ctypes", "socket", "http", "urllib",
})

# Allowed import prefixes
_ALLOWED_IMPORT_PREFIXES = ("twinkle.agentserver.workflow",)


def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
    """Custom __import__ replacement — only allow whitelisted imports."""
    root_module = name.split(".")[0]
    if root_module in _FORBIDDEN_MODULES:
        raise ImportError(f"Workflow sandbox 禁止导入: {name}")
    if not any(name.startswith(prefix) for prefix in _ALLOWED_IMPORT_PREFIXES):
        raise ImportError(f"Workflow sandbox 只允许导入: {_ALLOWED_IMPORT_PREFIXES}, 拒绝: {name}")
    return importlib.import_module(name, *args, **kwargs)


def build_namespace() -> dict[str, Any]:
    """Build a sandboxed namespace for exec(plan_code)."""
    from twinkle.agentserver.hooks.base import HookInterrupt
    from twinkle.agentserver.workflow.node import PlanNode

    builtins = dict(_SAFE_BUILTINS)
    builtins["__import__"] = _safe_import
    return {
        "__builtins__": builtins,
        "PlanNode": PlanNode,
        "HookInterrupt": HookInterrupt,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_plan_sandbox.py -v`
Expected: PASS (some tests may fail if PlanNode doesn't exist yet — that's OK, we'll fix in Task 5)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/workflow/sandbox.py tests/test_plan_sandbox.py
git commit -m "feat(workflow): add sandbox for exec(plan_code) isolation"
```

---

### Task 4: PlanNode 基类

**Files:**
- Create: `twinkle/agentserver/workflow/node.py`
- Create: `tests/test_plan_node.py`

**Interfaces:**
- Consumes: `HookInterrupt` from `twinkle/agentserver/hooks/base.py`
- Produces: `PlanNode` ABC（`_execute`, `run`, `execute_subplan`, `set_runtime_callbacks`, `has_tool`, `call_tool`, `call_llm`, `extract_json`）

这是 Workflow 引擎的核心抽象。参考 jiuwenswarm `plan_node.py`，砍掉流式/日志回调。

- [ ] **Step 1: Write failing tests for PlanNode**

```python
# tests/test_plan_node.py
import asyncio
import pytest
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.hooks.base import HookInterrupt


class EchoNode(PlanNode):
    """Simple node that returns inputs unchanged."""
    async def _execute(self, inputs: dict):
        return {"echo": inputs}


class FailNode(PlanNode):
    """Node that always raises."""
    async def _execute(self, inputs: dict):
        raise RuntimeError("deliberate failure")


class HitlInterruptNode(PlanNode):
    """Node that raises HookInterrupt."""
    async def _execute(self, inputs: dict):
        raise HookInterrupt("HITL interrupt")


class ParentNode(PlanNode):
    """Node that executes sub-plans sequentially."""
    async def _execute(self, inputs: dict):
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            inputs[sub.plan_name] = result
        return inputs


def test_node_echo():
    node = EchoNode("echo", "echo inputs")
    result = asyncio.run(node.run({"x": 1}))
    assert result == {"echo": {"x": 1}}


def test_node_run_with_fallback():
    """Fallback callback is called on exception."""
    fallback_called = False

    async def fallback(node, inputs, exc):
        nonlocal fallback_called
        fallback_called = True
        return {"fallback": True}

    node = FailNode("fail", "always fails")
    node._fallback_callback = fallback
    result = asyncio.run(node.run({}))
    assert fallback_called
    assert result == {"fallback": True}


def test_node_run_without_fallback_raises():
    """Without fallback callback, exception propagates."""
    node = FailNode("fail", "always fails")
    with pytest.raises(RuntimeError, match="deliberate failure"):
        asyncio.run(node.run({}))


def test_node_hook_interrupt_not_caught_by_fallback():
    """HookInterrupt is never caught by fallback — always propagates."""
    fallback_called = False

    async def fallback(node, inputs, exc):
        nonlocal fallback_called
        fallback_called = True
        return {"fallback": True}

    node = HitlInterruptNode("hitl", "interrupt test")
    node._fallback_callback = fallback
    with pytest.raises(HookInterrupt):
        asyncio.run(node.run({}))
    assert not fallback_called


def test_execute_subplan():
    """Parent executes sub-plans sequentially."""
    child1 = EchoNode("child1", "first child")
    child2 = EchoNode("child2", "second child")
    parent = ParentNode("parent", "parent node", sub_plans=[child1, child2])
    result = asyncio.run(parent.run({"start": True}))
    assert result["child1"] == {"echo": {"start": True}}
    assert result["child2"] == {"echo": {"start": True}}


def test_set_runtime_callbacks_propagates():
    """Callbacks are recursively propagated to all sub-plans."""
    child = EchoNode("child", "child node")
    parent = ParentNode("parent", "parent node", sub_plans=[child])

    called = []
    async def mock_llm(prompt, **kwargs):
        called.append(prompt)
        return "mock response"

    parent.set_runtime_callbacks(call_llm=mock_llm)
    # child should have the same callback
    assert child._call_llm_callback is mock_llm


def test_call_llm_raises_without_callback():
    """call_llm raises RuntimeError if callback not set."""
    node = EchoNode("echo", "echo")
    with pytest.raises(RuntimeError, match="回调未初始化"):
        asyncio.run(node.call_llm("test prompt"))


def test_has_tool_raises_without_callback():
    """has_tool raises RuntimeError if callback not set."""
    node = EchoNode("echo", "echo")
    with pytest.raises(RuntimeError, match="回调未初始化"):
        node.has_tool("some_tool")


def test_node_repr():
    node = EchoNode("echo", "echo inputs")
    assert "echo" in repr(node)


def test_subplan_depth_auto_set():
    """Sub-plan depth is auto-set to parent.depth + 1."""
    child = EchoNode("child", "child")
    parent = ParentNode("parent", "parent", sub_plans=[child])
    assert child.depth == 1
    assert parent.depth == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plan_node.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement PlanNode**

```python
# twinkle/agentserver/workflow/node.py
"""PlanNode ABC — recursive execution node for deterministic workflow orchestration."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Union

from twinkle.agentserver.hooks.base import HookInterrupt

logger = logging.getLogger(__name__)

__all__ = ["HookInterrupt", "PlanNode"]


class PlanNode(ABC):
    """规划节点 —— 递归结构，子类实现 async _execute，run 自带 fallback。

    与 skill code 的契约：
    1. 子类必须实现 async def _execute(self, inputs: dict) -> Any
    2. 禁止覆盖 run()——框架统一处理异常和 fallback
    3. plan_name 在同一 workflow 内唯一
    4. 节点访问外部能力仅通过回调：has_tool / call_tool / call_llm / extract_json
    5. HookInterrupt 不走 fallback，直接向上抛
    """

    def __init__(
        self,
        plan_name: str,
        instruction: str,
        sub_plans: list[PlanNode] | None = None,
        depth: int = 0,
    ):
        self.plan_name = plan_name
        self.instruction = instruction
        self.depth = depth
        self.sub_plans = sub_plans or []
        self._update_subplans_depth()

        # Callbacks (injected by Executor)
        self._has_tool_callback: Callable[[str], bool] | None = None
        self._call_tool_callback: Callable[..., Awaitable[Any]] | None = None
        self._call_llm_callback: Callable[..., Awaitable[str]] | None = None
        self._fallback_callback: Callable[[PlanNode, dict[str, Any], Exception], Awaitable[Any]] | None = None
        self._extract_json_callback: Callable[..., Any] | None = None
        self._before_subplan_execute: Callable[[PlanNode, dict[str, Any]], Awaitable[None]] | None = None
        self._after_subplan_execute: Callable[[PlanNode, dict[str, Any], Any], Awaitable[None]] | None = None

    def _update_subplans_depth(self) -> None:
        """Recursively update all descendant depths."""
        pending = [(subplan, self.depth + 1) for subplan in self.sub_plans]
        while pending:
            node, depth = pending.pop()
            node.depth = depth
            pending.extend((sub, depth + 1) for sub in node.sub_plans)

    def set_runtime_callbacks(self, **kwargs) -> None:
        """Inject callbacks, recursively propagate to all sub_plans."""
        for key, value in kwargs.items():
            if value is not None:
                setattr(self, f"_{key}_callback", value)
        for node in self.sub_plans:
            node.set_runtime_callbacks(**kwargs)

    # --- Capability methods (delegate to callbacks) ---

    def has_tool(self, tool_name: str) -> bool:
        if self._has_tool_callback is None:
            raise RuntimeError("PlanNode has_tool 回调未初始化")
        return self._has_tool_callback(tool_name)

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        if self._call_tool_callback is None:
            raise RuntimeError("PlanNode call_tool 回调未初始化")
        return await self._call_tool_callback(tool_name, **kwargs)

    async def call_llm(self, prompt: str, system_prompt: str = "") -> str:
        if self._call_llm_callback is None:
            raise RuntimeError("PlanNode call_llm 回调未初始化")
        return await self._call_llm_callback(prompt, system_prompt=system_prompt)

    def extract_json(self, raw: Union[str, dict, list], expected_type: type = dict) -> Any:
        if self._extract_json_callback is None:
            raise RuntimeError("PlanNode extract_json 回调未初始化")
        return self._extract_json_callback(raw, expected_type)

    # --- Execution ---

    @abstractmethod
    async def _execute(self, inputs: dict[str, Any]) -> Any: ...

    async def run(self, inputs: dict[str, Any]) -> Any:
        """Template method — not overridable, built-in fallback."""
        try:
            return await self._execute(inputs)
        except HookInterrupt:
            raise
        except Exception as e:
            logger.warning("[PlanNode] node failed name=%s error=%r", self.plan_name, e)
            if self._fallback_callback is None:
                raise
            return await self._fallback_callback(self, inputs, e)

    async def execute_subplan(self, subplan: PlanNode, inputs: dict[str, Any]) -> Any:
        """Execute a sub-plan with before/after callbacks."""
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)
        try:
            result = await subplan.run(inputs)
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, result)
            return result
        except HookInterrupt:
            raise
        except Exception as e:
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, e)
            raise

    def __repr__(self) -> str:
        return f"PlanNode(name={self.plan_name!r}, sub_plans={len(self.sub_plans)})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_plan_node.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/workflow/node.py tests/test_plan_node.py
git commit -m "feat(workflow): add PlanNode ABC with fallback and HookInterrupt"
```

---

### Task 5: ContextVar 桥接

**Files:**
- Create: `twinkle/agentserver/workflow/context.py`

**Interfaces:**
- Produces: `workflow_executor_ctx: ContextVar[WorkflowExecutor | None]`

ContextVar 让 `execute_workflow` 工具函数（在 `tools.py` 中）能从当前上下文获取 WorkflowExecutor 实例，不需要全局变量或参数传递。同 SubagentContextHook 的模式。

- [ ] **Step 1: Implement context module**

```python
# twinkle/agentserver/workflow/context.py
"""ContextVar bridge — lets execute_workflow tool access the WorkflowExecutor."""
from __future__ import annotations

from contextvars import ContextVar

# None = not in a workflow context; set by WorkflowContextHook
workflow_executor_ctx: ContextVar["WorkflowExecutor | None"] = ContextVar(
    "workflow_executor_ctx", default=None
)
```

- [ ] **Step 2: Commit**

```bash
git add twinkle/agentserver/workflow/context.py
git commit -m "feat(workflow): add ContextVar bridge for executor access"
```

---

### Task 6: WorkflowExecutor

**Files:**
- Create: `twinkle/agentserver/workflow/executor.py`
- Create: `tests/test_workflow_executor.py`

**Interfaces:**
- Consumes: `PlanNode` (Task 4), `PlanCodeValidator` (Task 2), `build_namespace` (Task 3), `extract_llm_json` (Task 1), `LLMClient`, `ToolManager`, `SubagentExecutor`, `WorkflowConfig`
- Produces: `WorkflowExecutor` class

Executor 是编排引擎的核心：校验 → 加载 → 绑定回调 → 执行。回调封装了 call_tool / call_llm / has_tool / fallback / extract_json。

- [ ] **Step 1: Write failing tests for WorkflowExecutor**

```python
# tests/test_workflow_executor.py
import asyncio
import json
import pytest
from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.hooks.base import HookInterrupt


class AddNode(PlanNode):
    """Adds two numbers from inputs."""
    async def _execute(self, inputs: dict):
        return {"sum": inputs.get("a", 0) + inputs.get("b", 0)}


class MultiplyNode(PlanNode):
    """Multiplies result from previous node."""
    async def _execute(self, inputs: dict):
        prev = inputs.get("sum", 0)
        return {"product": prev * inputs.get("factor", 1)}


class PipelineNode(PlanNode):
    """Runs sub-plans sequentially, passing inputs."""
    async def _execute(self, inputs: dict):
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            inputs.update(result)
        return inputs


# Simple plan_code that defines a root PlanNode
_PLAN_CODE = '''
from twinkle.agentserver.workflow.node import PlanNode

class SimpleNode(PlanNode):
    async def _execute(self, inputs):
        return {"result": inputs.get("x", 0) * 2}

root = SimpleNode("simple", "double x")
'''

# Plan_code with invalid syntax
_BAD_SYNTAX_CODE = "this is not valid python {{{"

# Plan_code with forbidden import
_FORBIDDEN_CODE = "import os\nroot = None"


@pytest.fixture
def executor():
    """Minimal executor with no real LLM/tools."""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config.schema import WorkflowConfig
    return WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(),
    )


def test_execute_simple_plan(executor):
    """Load and execute a simple plan_code."""
    result = asyncio.run(executor.execute_workflow(_PLAN_CODE, {"x": 5}))
    assert result["result"] == 10


def test_execute_rejects_bad_syntax(executor):
    """Syntax errors in plan_code are rejected."""
    from twinkle.agentserver.workflow.validator import PlanCodeValidationError
    with pytest.raises(PlanCodeValidationError):
        asyncio.run(executor.execute_workflow(_BAD_SYNTAX_CODE, {}))


def test_execute_rejects_forbidden_import(executor):
    """Forbidden imports in plan_code are rejected."""
    from twinkle.agentserver.workflow.validator import PlanCodeValidationError
    with pytest.raises(PlanCodeValidationError):
        asyncio.run(executor.execute_workflow(_FORBIDDEN_CODE, {}))


def test_execute_with_fallback():
    """Node failure triggers fallback via SubagentExecutor."""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config.schema import WorkflowConfig

    fallback_called = False

    class FakeSubagentExecutor:
        async def execute_subagent(self, task_spec, **kwargs):
            nonlocal fallback_called
            fallback_called = True
            return f"[Fallback] {task_spec.objective}"

    executor = WorkflowExecutor(
        llm=None, tools=None,
        subagent_executor=FakeSubagentExecutor(),
        config=WorkflowConfig(enable_fallback=True),
    )

    fail_code = '''
from twinkle.agentserver.workflow.node import PlanNode

class FailNode(PlanNode):
    async def _execute(self, inputs):
        raise RuntimeError("boom")

root = FailNode("fail", "always fails")
'''
    result = asyncio.run(executor.execute_workflow(fail_code, {}))
    assert fallback_called
    assert "Fallback" in result


def test_execute_timeout():
    """Execution timeout raises ExecutionTimeoutError."""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor, ExecutionTimeoutError
    from twinkle.config.schema import WorkflowConfig

    executor = WorkflowExecutor(
        llm=None, tools=None, subagent_executor=None,
        config=WorkflowConfig(execution_timeout=0.1),
    )

    slow_code = '''
import asyncio
from twinkle.agentserver.workflow.node import PlanNode

class SlowNode(PlanNode):
    async def _execute(self, inputs):
        await asyncio.sleep(10)
        return {"done": True}

root = SlowNode("slow", "takes forever")
'''
    with pytest.raises(ExecutionTimeoutError):
        asyncio.run(executor.execute_workflow(slow_code, {}))


def test_execute_hook_interrupt_propagates():
    """HookInterrupt is not caught by fallback."""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config.schema import WorkflowConfig

    executor = WorkflowExecutor(
        llm=None, tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=True),
    )

    interrupt_code = '''
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.hooks.base import HookInterrupt

class InterruptNode(PlanNode):
    async def _execute(self, inputs):
        raise HookInterrupt("stop")

root = InterruptNode("interrupt", "HITL test")
'''
    with pytest.raises(HookInterrupt):
        asyncio.run(executor.execute_workflow(interrupt_code, {}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workflow_executor.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement WorkflowExecutor**

```python
# twinkle/agentserver/workflow/executor.py
"""WorkflowExecutor — orchestrates PlanNode tree execution with validation, sandboxing, and fallback."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import TYPE_CHECKING, Any

from twinkle.agentserver.workflow.json_utils import extract_llm_json
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.workflow.sandbox import build_namespace
from twinkle.agentserver.workflow.validator import PlanCodeValidator

if TYPE_CHECKING:
    from twinkle.agentserver.llm_client import LLMClient
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.tools.builtin.subagent.executor import SubagentExecutor
    from twinkle.config.schema import WorkflowConfig

logger = logging.getLogger(__name__)


class PlanCodeValidationError(Exception):
    """Raised when plan_code fails AST validation."""


class ExecutionTimeoutError(Exception):
    """Raised when workflow execution exceeds the configured timeout."""


class FallbackLimitExceededError(Exception):
    """Raised when fallback count exceeds the configured limit."""


class WorkflowExecutor:
    """Orchestrates PlanNode tree execution: validate → load → bind callbacks → execute."""

    def __init__(
        self,
        llm: "LLMClient | None",
        tools: "ToolManager | None",
        subagent_executor: "SubagentExecutor | None",
        config: "WorkflowConfig",
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._subagent_executor = subagent_executor
        self._config = config
        self._validator = PlanCodeValidator()
        self._fallback_count = 0

    async def execute_workflow(self, plan_code: str, inputs: dict) -> Any:
        """Validate → load → bind callbacks → execute (with timeout)."""
        root = self._prepare_root_node(plan_code)
        self._fallback_count = 0
        try:
            return await asyncio.wait_for(
                root.run(inputs),
                timeout=self._config.execution_timeout,
            )
        except asyncio.TimeoutError:
            raise ExecutionTimeoutError(
                f"Workflow execution timed out after {self._config.execution_timeout}s"
            )

    def _prepare_root_node(self, plan_code: str) -> PlanNode:
        """Validate → load → extract root → deep copy → bind callbacks."""
        errors = self._validator.validate(plan_code)
        if errors:
            raise PlanCodeValidationError(errors)

        namespace = self._load_plan_namespace(plan_code)
        root = self._extract_root_node(namespace)
        root = copy.deepcopy(root)
        self._bind_node_callbacks(root)
        return root

    def _load_plan_namespace(self, plan_code: str) -> dict[str, Any]:
        """exec(plan_code, sandboxed_namespace)."""
        namespace = build_namespace()
        try:
            exec(plan_code, namespace)  # noqa: S102
        except Exception as e:
            logger.error("[WorkflowExecutor] plan code load failed: %s", e, exc_info=True)
            raise PlanCodeValidationError(f"规划代码执行失败: {e}") from e
        return namespace

    def _extract_root_node(self, namespace: dict[str, Any]) -> PlanNode:
        """Extract 'root' PlanNode from namespace."""
        root = namespace.get("root")
        if root is None:
            raise PlanCodeValidationError("plan_code 未定义 'root' 变量")
        if not isinstance(root, PlanNode):
            raise PlanCodeValidationError(f"'root' 不是 PlanNode 实例: {type(root)}")
        return root

    def _bind_node_callbacks(self, root: PlanNode) -> None:
        """Inject callbacks: call_tool / call_llm / has_tool / fallback / extract_json."""
        root.set_runtime_callbacks(
            has_tool=self._has_tool_wrapper,
            use_tool=self._call_tool_wrapper,
            call_llm=self._call_llm_wrapper,
            fallback=self._fallback_wrapper,
            extract_json=self._extract_json_wrapper,
        )

    # --- Callback wrappers ---

    def _has_tool_wrapper(self, tool_name: str) -> bool:
        if self._tools is None:
            return False
        return self._tools.get(tool_name) is not None

    async def _call_tool_wrapper(self, tool_name: str, **kwargs: Any) -> Any:
        if self._tools is None:
            raise RuntimeError(f"ToolManager 未初始化，无法调用工具: {tool_name}")
        result = await self._tools.execute(tool_name, kwargs)
        # Try to parse as JSON
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result

    async def _call_llm_wrapper(self, prompt: str, system_prompt: str = "") -> str:
        """Call LLM via stream + collect TextDelta (same as compression._summarize)."""
        if self._llm is None:
            raise RuntimeError("LLMClient 未初始化，无法调用 LLM")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        collected: list[str] = []
        async for event in self._llm.stream(messages, tools=[]):
            from twinkle.agentserver.llm_client import TextDelta
            if isinstance(event, TextDelta):
                collected.append(event.content)
        return "".join(collected)

    async def _fallback_wrapper(self, node: PlanNode, inputs: dict, exc: Exception) -> Any:
        """Fallback: delegate to SubagentExecutor."""
        if not self._config.enable_fallback:
            raise exc
        if self._subagent_executor is None:
            raise exc
        self._fallback_count += 1
        if self._fallback_count > self._config.max_fallback_count:
            raise FallbackLimitExceededError(
                f"Workflow fallback 次数超过上限: {self._config.max_fallback_count}"
            ) from exc
        logger.warning("[WorkflowExecutor] fallback for node=%s error=%r", node.plan_name, exc)
        from twinkle.agentserver.tools.builtin.subagent.models import SubagentTaskSpec
        task_spec = SubagentTaskSpec(
            objective=f"[Workflow fallback] {node.plan_name}: {node.instruction}"
        )
        result = await self._subagent_executor.execute_subagent(task_spec)
        return {"node": node.plan_name, "status": "degraded", "result": result}

    def _extract_json_wrapper(self, raw: Any, expected_type: type = dict) -> Any:
        return extract_llm_json(raw, expected_type)
```

- [ ] **Step 4: Add WorkflowConfig to config schema**

Add to `twinkle/config/schema.py`:

```python
class WorkflowConfig(_StrictModel):
    execution_timeout: float = 300.0      # 整棵树超时（秒）
    max_fallback_count: int = 3           # 最大 fallback 次数
    enable_fallback: bool = True          # 是否启用 fallback
```

Add `workflow: WorkflowConfig = WorkflowConfig()` to `TwinkleConfig`.

- [ ] **Step 5: Update workflow __init__.py**

```python
# twinkle/agentserver/workflow/__init__.py
"""Workflow engine — code-driven deterministic orchestration."""
from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode

__all__ = ["PlanNode", "WorkflowExecutor"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_workflow_executor.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/workflow/executor.py twinkle/agentserver/workflow/__init__.py twinkle/config/schema.py tests/test_workflow_executor.py
git commit -m "feat(workflow): add WorkflowExecutor with validation, sandbox, and fallback"
```

---

### Task 7: execute_workflow 工具 + ContextVar Hook

**Files:**
- Create: `twinkle/agentserver/workflow/tools.py`
- Modify: `twinkle/agentserver/tools/__init__.py`（注册工具）
- Modify: `twinkle/agentserver/server.py`（注册 Hook）

**Interfaces:**
- Consumes: `workflow_executor_ctx` (Task 5), `WorkflowExecutor` (Task 6)
- Produces: `execute_workflow` @tool, `WorkflowContextHook`

`execute_workflow` 工具注册到 AgentLoop，LLM 看到"可用 workflow: pptx-craft"等描述后自主选择调用。`WorkflowContextHook` 在每个 ReAct 轮次前设置 ContextVar，让工具函数能获取 executor。

- [ ] **Step 1: Write the execute_workflow tool**

```python
# twinkle/agentserver/workflow/tools.py
"""execute_workflow @tool — LLM-visible entry point for workflow execution."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.workflow.context import workflow_executor_ctx

logger = logging.getLogger(__name__)


def _scan_workflows() -> dict[str, str]:
    """Scan <WORKFLOWS_DIR>/*/root.py for available workflows.

    Returns: {workflow_name: instruction} for tool description.
    """
    from twinkle.config import settings
    workflows_dir = getattr(settings, "workflows_dir", None)
    if not workflows_dir:
        return {}
    workflows_dir = Path(workflows_dir)
    if not workflows_dir.exists():
        return {}
    result = {}
    for d in sorted(workflows_dir.iterdir()):
        if d.is_dir() and (d / "root.py").exists():
            result[d.name] = f"workflow: {d.name}"
    return result


def _build_tool_description() -> str:
    """Build dynamic tool description listing available workflows."""
    workflows = _scan_workflows()
    if not workflows:
        return "执行预定义的 Workflow，用于结构化多步骤任务。（当前无可用 workflow）"
    lines = ["执行预定义的 Workflow，用于结构化多步骤任务。", "", "可用 workflow:"]
    for name, desc in workflows.items():
        lines.append(f"- {name}")
    return "\n".join(lines)


@tool
async def execute_workflow(workflow_name: str, inputs: str = "{}") -> str:
    """Execute a predefined workflow for structured multi-step tasks."""
    executor = workflow_executor_ctx.get()
    if executor is None:
        return "Error: WorkflowExecutor 未初始化"

    # Load plan_code from <WORKFLOWS_DIR>/<workflow_name>/root.py
    from twinkle.config import settings
    workflows_dir = getattr(settings, "workflows_dir", None)
    if not workflows_dir:
        return "Error: workflows_dir 未配置"

    root_path = Path(workflows_dir) / workflow_name / "root.py"
    if not root_path.exists():
        return f"Error: workflow '{workflow_name}' 不存在"

    plan_code = root_path.read_text(encoding="utf-8")
    try:
        parsed_inputs = json.loads(inputs)
    except json.JSONDecodeError:
        return f"Error: inputs 不是有效的 JSON: {inputs}"

    try:
        result = await executor.execute_workflow(plan_code, parsed_inputs)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except Exception as e:
        return f"Error: workflow 执行失败: {e}"
```

- [ ] **Step 2: Write the WorkflowContextHook**

```python
# Add to twinkle/agentserver/workflow/tools.py (or a separate hook file)

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookInputs


class WorkflowContextHook(AgentHook):
    """Sets workflow_executor_ctx ContextVar before each ReAct iteration."""

    def __init__(self, executor):
        self._executor = executor

    async def before_invoke(self, ctx: HookContext, inputs: HookInputs) -> None:
        workflow_executor_ctx.set(self._executor)
```

- [ ] **Step 3: Register tool in tools/__init__.py**

Add to `twinkle/agentserver/tools/__init__.py`:

```python
from twinkle.agentserver.tools.builtin import workflow as workflow_tools
# In tool_manager():
tm.register(workflow_tools.execute_workflow)
```

- [ ] **Step 4: Register hook in server.py**

Add to `build_agent_loop()` in `twinkle/agentserver/server.py`:

```python
from twinkle.agentserver.workflow.tools import WorkflowContextHook
from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.config import settings

workflow_executor = WorkflowExecutor(
    llm=llm, tools=tools, subagent_executor=executor,
    config=settings.workflow,
)
# Add to the hook list:
WorkflowContextHook(workflow_executor),
```

- [ ] **Step 5: Add workflows_dir to config**

Add `workflows_dir` field to `WorkspaceConfig` or `TwinkleConfig` in `twinkle/config/schema.py`:

```python
# In WorkspaceConfig or a new WorkflowConfig:
workflows_dir: str = ""  # "" -> <workspace>/workflows
```

And derive the path in `_derive_paths()`.

- [ ] **Step 6: Run existing tests to verify nothing is broken**

Run: `python -m pytest tests/ -v --timeout=60`
Expected: All existing tests pass

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/workflow/tools.py twinkle/agentserver/tools/__init__.py twinkle/agentserver/server.py twinkle/config/schema.py
git commit -m "feat(workflow): add execute_workflow tool and WorkflowContextHook"
```

---

### Task 8: 验收测试 — 端到端集成

**Files:**
- Create: `tests/test_workflow_e2e.py`

端到端测试：定义一个 3 层 PlanNode 树，验证节点间 inputs 正确传递、fallback 正常工作、HookInterrupt 不被吞掉。

- [ ] **Step 1: Write end-to-end test**

```python
# tests/test_workflow_e2e.py
"""End-to-end integration test for the Workflow engine."""
import asyncio
import pytest
from twinkle.agentserver.workflow.executor import WorkflowExecutor, FallbackLimitExceededError
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.config.schema import WorkflowConfig


# 3-layer PlanNode tree
class LeafNode(PlanNode):
    """Leaf node that transforms inputs."""
    async def _execute(self, inputs: dict):
        return {"leaf_result": inputs.get("value", 0) * 2}


class BranchNode(PlanNode):
    """Branch node that executes two leaf sub-plans."""
    async def _execute(self, inputs: dict):
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            inputs[sub.plan_name] = result
        return inputs


class RootNode(PlanNode):
    """Root node that orchestrates the pipeline."""
    async def _execute(self, inputs: dict):
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            inputs.update(result)
        return inputs


def test_three_layer_tree():
    """3-layer PlanNode tree: root → branch → leaf, inputs correctly passed."""
    leaf1 = LeafNode("leaf1", "double value")
    leaf2 = LeafNode("leaf2", "double value again")
    branch = BranchNode("branch", "branch", sub_plans=[leaf1, leaf2])
    root = RootNode("root", "root", sub_plans=[branch])

    executor = WorkflowExecutor(
        llm=None, tools=None, subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )
    executor._bind_node_callbacks(root)

    result = asyncio.run(root.run({"value": 5}))
    assert result["leaf1"] == {"leaf_result": 10}
    assert result["leaf2"] == {"leaf_result": 10}


def test_fallback_with_subagent_executor():
    """Node failure triggers SubagentExecutor fallback."""
    class FailNode(PlanNode):
        async def _execute(self, inputs):
            raise RuntimeError("boom")

    class FakeSubagentExecutor:
        async def execute_subagent(self, task_spec, **kwargs):
            return "fallback result"

    root = FailNode("fail", "fails")
    executor = WorkflowExecutor(
        llm=None, tools=None,
        subagent_executor=FakeSubagentExecutor(),
        config=WorkflowConfig(enable_fallback=True),
    )
    executor._bind_node_callbacks(root)

    result = asyncio.run(root.run({}))
    assert result["status"] == "degraded"
    assert result["result"] == "fallback result"


def test_hook_interrupt_not_caught():
    """HookInterrupt is never caught by fallback."""
    class InterruptNode(PlanNode):
        async def _execute(self, inputs):
            raise HookInterrupt("HITL")

    class FakeSubagentExecutor:
        async def execute_subagent(self, task_spec, **kwargs):
            return "should not reach here"

    root = InterruptNode("interrupt", "HITL test")
    executor = WorkflowExecutor(
        llm=None, tools=None,
        subagent_executor=FakeSubagentExecutor(),
        config=WorkflowConfig(enable_fallback=True),
    )
    executor._bind_node_callbacks(root)

    with pytest.raises(HookInterrupt):
        asyncio.run(root.run({}))


def test_fallback_limit():
    """Exceeding max_fallback_count raises FallbackLimitExceededError."""
    class FailNode(PlanNode):
        async def _execute(self, inputs):
            raise RuntimeError("boom")

    class FakeSubagentExecutor:
        async def execute_subagent(self, task_spec, **kwargs):
            return "fallback"

    # Create a plan with multiple failing nodes
    fail1 = FailNode("fail1", "fails 1")
    fail2 = FailNode("fail2", "fails 2")

    class RootNode(PlanNode):
        async def _execute(self, inputs):
            r1 = await self.execute_subplan(self.sub_plans[0], inputs)
            r2 = await self.execute_subplan(self.sub_plans[1], inputs)
            return [r1, r2]

    root = RootNode("root", "root", sub_plans=[fail1, fail2])

    executor = WorkflowExecutor(
        llm=None, tools=None,
        subagent_executor=FakeSubagentExecutor(),
        config=WorkflowConfig(enable_fallback=True, max_fallback_count=1),
    )
    executor._bind_node_callbacks(root)

    with pytest.raises(FallbackLimitExceededError):
        asyncio.run(root.run({}))
```

- [ ] **Step 2: Run all workflow tests**

Run: `python -m pytest tests/test_plan_json_utils.py tests/test_plan_code_validator.py tests/test_plan_sandbox.py tests/test_plan_node.py tests/test_workflow_executor.py tests/test_workflow_e2e.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=60`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_workflow_e2e.py
git commit -m "test(workflow): add end-to-end integration tests"
```
