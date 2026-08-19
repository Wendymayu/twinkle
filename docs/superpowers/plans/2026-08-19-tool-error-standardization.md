# Tool Error Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify all tool-failure returns into a single `ToolError` exception + `format_tool_error` formatter, eliminating the 6 scattered error-prefix conventions (`[tool error]`/`[ERROR]:`/`[error]`/`[team unavailable]`/`[subagent unavailable]`/`[tool denied by user]`) across ~55 sites.

**Architecture:** Adopt openclaw's "Throw on failure instead of encoding errors in content" contract — tools `raise ToolError(message, kind=...)` on failure, `return str` on success. A single `format_tool_error()` in the agent loop's catch points renders every failure as `[tool error] {message}` (reusing the existing `TOOL_ERROR_PREFIX` constant). The `kind` field stays on the exception object only (for future RetryHook/retry decisions + observability B-plan); it is **never** rendered into content. This mirrors openclaw's `createErrorToolResult` + `coerceErrorMessage` (single chokepoint) and jiuwenswarm's "exception carries structured field" (AbilityExecutionError), but deliberately skips openclaw's full `ToolResult{content,details}` / jiuwenswarm's `ToolOutput{success,data,error}` / numeric `StatusCode` (~250) — Twinkle is a slim learning reimplementation with no "partial-output soft error" case (command_exec returns JSON on any exit code) and runs on OpenAI function-calling wire (content is a plain string, no `isError` field).

**Tech Stack:** Python 3, asyncio, pytest (no pytest-asyncio — `asyncio.run()` + fixtures from `tests/conftest.py`).

---

## Key Design Decisions (locked, from prior discussion)

1. **content form: unified prefix `[tool error]`** (not prefix-less). Twinkle runs on OpenAI function-calling wire — `role:tool` content is a plain string with no `isError` field, so the LLM can only judge "tool failed vs. normal return" from the content text. A unified prefix (reusing `TOOL_ERROR_PREFIX`) lets the LLM unambiguously recognize failures. openclaw can go prefix-less only because it has a first-class `isError` field; Twinkle does not. **Confirmed by user.**

2. **`format_tool_error` distinguishes ToolError vs unknown Exception:**
   - `ToolError` → `[tool error] {message}` (kind not rendered)
   - other `Exception` → `[tool error] {ExcType}: {message}` (keep type name for debugging)
   - `str` → `[tool error] {str}` (for `denied` etc. constructed directly in agent loop)
   - This makes the existing catch-all test `assert content == "[tool error] ValueError: boom"` (`test_agent_loop_failure.py:92`) **stay green unchanged** — ValueError is an unknown exception taking the `{ExcType}: {exc}` branch.

3. **`kind` is a reserved field, no current consumer.** RetryHook is NOT changed this plan (ToolError is not in `TRANSIENT_EXCEPTIONS` → not retried, matching today's soft-error no-retry behavior). Observability is explicitly out of scope (user said "先不考虑可观测"). `kind` exists only so a future RetryHook-by-kind / `is_error` metadata (B-plan) can adopt it with zero structural change. Per CLAUDE.md YAGNI this is borderline; it is retained because (a) it is 5 string values not a 250-entry enum, (b) two reference impls both keep a classification field, (c) it is the documented hand-off point for the deferred B-plan. If the reviewer disagrees, dropping `kind` is a one-line change (ToolError takes only `message`).

4. **Scope (what changes / what does NOT):**
   - CHANGES: catch-all 3 sites (`agent.py:659/664/778`), `denied` (`agent.py:661`), unknown-tool (`manager.py:47`), soft errors in `file_tools.py`(~30) / `command_exec.py`(7) / `web_search.py`(2) / `web_fetch.py`(2) / `team_tools.py`(8) / `subagent/tools.py`(1).
   - DOES NOT CHANGE: orphan fill `agent.py:857` (`[interrupted: ...]` — session-recovery synthetic message, not a tool-execution failure), gateway `[error]` (`message_handler.py:82/105` — browser-facing, not tool_result), permission-hook `deny_message` (`[ERROR]` in `permission_hook`/`permissions_models` — that is the permission system's own contract), `instrumentors/tool.py` (observability layer, out of scope — and the change is net-positive: soft errors now raise, so they hit the instrumentor's `except` branch and become counted as tool errors, fixing today's gap where `[ERROR]:`/`[error]` soft returns were never counted).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `twinkle/agentserver/tools/errors.py` | **Create** | `ToolError(Exception)` + `format_tool_error(source)` — single chokepoint |
| `twinkle/agentserver/tools/manager.py` | Modify `:44-52` | `execute()` unknown tool: `return` → `raise ToolError` |
| `twinkle/agentserver/agent.py` | Modify `:659,661,664,778` | catch-all + denied → `format_tool_error(...)` |
| `twinkle/agentserver/tools/builtin/file_tools.py` | Modify ~30 sites | `return "[ERROR]: ..."` → `raise ToolError("...", kind=...)` |
| `twinkle/agentserver/tools/builtin/command_exec.py` | Modify 7 sites | `return "[ERROR]: ..."` → `raise ToolError(...)` |
| `twinkle/agentserver/tools/builtin/web_search.py` | Modify `:228,242` | `return "[error] ..."` → `raise ToolError(...)` |
| `twinkle/agentserver/tools/builtin/web_fetch.py` | Modify `:148,179` | `return "[error] ..."` → `raise ToolError(...)` |
| `twinkle/agentserver/tools/builtin/team_tools.py` | Modify 8 sites | `return "[team unavailable]"` → `raise ToolError(..., kind="unavailable")` |
| `twinkle/agentserver/tools/builtin/subagent/tools.py` | Modify `:57` | `return "[subagent unavailable] ..."` → `raise ToolError(..., kind="unavailable")` |
| `tests/test_tool_errors.py` | **Create** | TDD tests for `ToolError` + `format_tool_error` |
| `tests/test_tool_manager.py` | Modify `:38-40` | unknown-tool: assert return → `pytest.raises(ToolError)` |
| `tests/test_command_exec.py` | Modify `:17,21-23,27` | soft-error asserts → `pytest.raises` |
| `tests/test_file_tools.py` | Modify `:282,287,294-295` | soft-error asserts → `pytest.raises` |
| `tests/test_web_fetch.py` | Modify `:111-113,131-134,140` | failure asserts → `pytest.raises` (success path `:94` unchanged) |
| `tests/test_web_search.py` | Modify `:102,148` | failure asserts → `pytest.raises` |
| `tests/test_team_tools.py` | Modify `:22-25` | unavailable assert → `pytest.raises` |

**kind assignment table** (used by soft-error conversions):
- `validation`: empty/missing param, path escapes workspace, empty old_string, `..` in pattern, unknown tool
- `denied`: safety blocklist rejection, user-denied approval
- `unavailable`: team/subagent not initialized, web engines unavailable
- `failed`: file not found / binary / read-write failure, command timeout / exec failure / start failure, web fetch failed, glob failed

---

### Task 1: ToolError + format_tool_error infrastructure (TDD)

**Files:**
- Create: `twinkle/agentserver/tools/errors.py`
- Test: `tests/test_tool_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_errors.py
"""ToolError + format_tool_error — single chokepoint for tool-failure content.

Mirrors openclaw createErrorToolResult + coerceErrorMessage: tools raise on
failure (never encode errors into content); the loop's catch points call
format_tool_error to render one unified [tool error] prefix.
"""
from __future__ import annotations

import pytest

from twinkle.agentserver.tools.errors import ToolError, format_tool_error
from twinkle.observability.attributes import TOOL_ERROR_PREFIX


def test_tool_error_carries_kind_but_str_is_just_message():
    e = ToolError("file_path is required", kind="validation")
    assert str(e) == "file_path is required"
    assert e.kind == "validation"
    assert isinstance(e, Exception)


def test_tool_error_default_kind_is_failed():
    assert ToolError("oops").kind == "failed"


def test_format_tool_error_for_toolerror_is_prefix_plus_message():
    # kind must NOT appear in content.
    out = format_tool_error(ToolError("file_path is required", kind="validation"))
    assert out == f"{TOOL_ERROR_PREFIX} file_path is required"
    assert "validation" not in out


def test_format_tool_error_for_unknown_exception_keeps_type_name():
    out = format_tool_error(ValueError("boom"))
    assert out == f"{TOOL_ERROR_PREFIX} ValueError: boom"


def test_format_tool_error_for_str_is_prefix_plus_text():
    out = format_tool_error("tool denied by user: bash — reason")
    assert out == f"{TOOL_ERROR_PREFIX} tool denied by user: bash — reason"


def test_format_tool_error_reuses_constant_not_literal():
    # The producer must reference TOOL_ERROR_PREFIX, not a literal, so it
    # cannot drift from the observability consumer (instrumentors/tool.py).
    assert format_tool_error(ToolError("x")).startswith(TOOL_ERROR_PREFIX)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.errors'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/tools/errors.py
"""Tool failure primitives — the single chokepoint for tool-error content.

Aligned with openclaw's contract: "Throw on failure instead of encoding
errors in `content`." Tools raise ToolError on failure, return str on success.
The agent loop's catch points call format_tool_error to render one unified
``[tool error]`` prefix (reusing TOOL_ERROR_PREFIX so producer and the
observability consumer cannot drift).

Why no numeric StatusCode (jiuwenswarm ~250-entry enum) and no
ToolResult{content,details} (openclaw) or ToolOutput{success,data,error}
(jiuwenswarm): Twinkle is a slim learning reimplementation on OpenAI
function-calling wire (content is a plain string, no isError field) with no
partial-output soft-error case. A prefix + kind field is the minimum that
solves the problem.
"""
from __future__ import annotations

from twinkle.observability.attributes import TOOL_ERROR_PREFIX


class ToolError(Exception):
    """Raise inside a tool on failure. Never encode errors into return content.

    ``kind`` stays on the exception object for future consumers (RetryHook
    retry-by-kind, observability is_error metadata) — it is NOT rendered into
    content by format_tool_error. Has no current consumer (YAGNI border); kept
    as the zero-cost hand-off point for the deferred observability B-plan.
    """

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def format_tool_error(source: "str | BaseException") -> str:
    """Render any tool failure into the unified ``[tool error] ...`` content.

    - ToolError        -> ``[tool error] {message}``        (kind not rendered)
    - other Exception  -> ``[tool error] {ExcType}: {msg}`` (keep type name for debugging)
    - str              -> ``[tool error] {str}``            (denied etc. built directly in the loop)

    The prefix reuses TOOL_ERROR_PREFIX so the producer cannot drift from the
    observability consumer (instrumentors/tool.py startswith check).
    """
    if isinstance(source, ToolError):
        return f"{TOOL_ERROR_PREFIX} {source}"
    if isinstance(source, BaseException):
        return f"{TOOL_ERROR_PREFIX} {type(source).__name__}: {source}"
    return f"{TOOL_ERROR_PREFIX} {source}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_errors.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/errors.py tests/test_tool_errors.py
git commit -m "feat(tools): add ToolError + format_tool_error single chokepoint"
```

---

### Task 2: ToolManager.execute unknown-tool raises ToolError

**Files:**
- Modify: `twinkle/agentserver/tools/manager.py:44-52`
- Test: `tests/test_tool_manager.py:38-40`

- [ ] **Step 1: Update the failing test**

```python
# tests/test_tool_manager.py  — replace test_unknown_tool_returns_error_string
import pytest
from twinkle.agentserver.tools.errors import ToolError
# (existing imports stay)

def test_unknown_tool_raises_tool_error() -> None:
    m = _make_manager()
    with pytest.raises(ToolError, match="unknown tool: nope"):
        asyncio.run(m.execute("nope", {}))
```

Also keep `test_execute_propagates_tool_exception` (`:48-62`) unchanged — it already asserts ValueError propagates from execute (ToolError is a subclass of Exception; that test still holds).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_manager.py::test_unknown_tool_raises_tool_error -v`
Expected: FAIL — execute returns `"[error] unknown tool: nope"`, does not raise.

- [ ] **Step 3: Modify execute**

```python
# twinkle/agentserver/tools/manager.py  — replace the execute method body
    async def execute(self, name: str, args: dict) -> str:
        t = self._tools.get(name)
        if t is None:
            raise ToolError(f"unknown tool: {name}", kind="validation")
        # Tool exceptions propagate (not swallowed here) so the @hook-decorated
        # _hooked_tool_call can fire ON_TOOL_EXCEPTION and RetryHook can retry
        # transient ones. The agent loop turns non-retried / exhausted failures
        # into a "[tool error] ..." tool_result string via format_tool_error —
        # loop still doesn't crash.
        return await t.invoke(args)
```

Add `from twinkle.agentserver.tools.errors import ToolError` to the top imports (next to the existing `from twinkle.agentserver.tools.base import Tool`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_manager.py -v`
Expected: PASS (all manager tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/manager.py tests/test_tool_manager.py
git commit -m "feat(tools): ToolManager.execute raises ToolError on unknown tool"
```

---

### Task 3: agent loop catch-all + denied → format_tool_error

**Files:**
- Modify: `twinkle/agentserver/agent.py:659,661,664,778`
- Test: `tests/test_agent_loop_failure.py` (verify unchanged)

- [ ] **Step 1: Verify the existing catch-all test still expects the same shape**

Run: `python -m pytest tests/test_agent_loop_failure.py::test_tool_non_transient_becomes_tool_error_and_loop_continues tests/test_agent_loop_failure.py::test_tool_transient_retried_once_then_becomes_tool_error -v`
Expected: PASS (these assert `"[tool error] ValueError: boom"` and `startswith("[tool error]")` + `"ConnectError" in content` — ValueError/ConnectError are unknown exceptions → `{ExcType}: {exc}` branch → identical output. These tests must stay green WITHOUT editing them; if they break, the format branch is wrong.)

- [ ] **Step 2: Modify the 3 catch-all sites + denied site**

Add import near top of `agent.py`:
```python
from twinkle.agentserver.tools.errors import ToolError, format_tool_error
```

Replace each of the 3 catch-all lines (current: `result = f"[tool error] {type(exc).__name__}: {exc}"`) with:
```python
                                            result = format_tool_error(exc)
```
(occurs at `agent.py:659`, `:664`, `:778` — same replacement at all three; the `except Exception as exc:` blocks stay).

Replace the denied block at `agent.py:661-662`:
```python
                                            else:
                                                result = (f"[tool denied by user: {hook_interrupt.data['tool']}] "
                                                          f"{hook_interrupt.data.get('reason', '')}")
```
with:
```python
                                            else:
                                                result = format_tool_error(
                                                    f"tool denied by user: {hook_interrupt.data['tool']} "
                                                    f"— {hook_interrupt.data.get('reason', '')}")
```

- [ ] **Step 3: Run catch-all tests to verify they pass unchanged**

Run: `python -m pytest tests/test_agent_loop_failure.py -v`
Expected: PASS (all 4 tests — the ValueError/ConnectError cases take the unknown-exception branch → `[tool error] ValueError: boom` / `[tool error] ConnectError: ...`, identical to before).

- [ ] **Step 4: Run the broader agent-loop + orphan + integration suites**

Run: `python -m pytest tests/test_agent_loop.py tests/test_orphan_cleanup.py tests/test_integration.py tests/test_agent_loop_with_hooks.py -v`
Expected: PASS (orphan `:857` is untouched — `[interrupted: ...]` stays as-is per scope).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/agent.py
git commit -m "refactor(agent): catch-all + denied route through format_tool_error"
```

---

### Task 4: file_tools.py soft errors → raise ToolError (~30 sites)

**Conversion pattern (applies to every site below):** `return "[ERROR]: {msg}"` → `raise ToolError("{msg}", kind="{kind}")`. The `@tool`-decorated async functions then propagate the ToolError out of `invoke()` → instrumentor `except` branch (now counted as a tool error — fixing today's gap) → agent loop catch → `format_tool_error` renders `[tool error] {msg}`.

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (sites listed below)
- Test: `tests/test_file_tools.py`

- [ ] **Step 1: Add import + convert all soft-error sites**

Add to `file_tools.py` imports:
```python
from twinkle.agentserver.tools.errors import ToolError
```

Apply these exact edits (old → new). Every `raise` replaces a `return`:

| Line | old | new |
|---|---|---|
| 94 | `        return "[ERROR]: file_path is required."` | `        raise ToolError("file_path is required", kind="validation")` |
| 109 | `        return f"[ERROR]: path is outside the project workspace: {file_path}"` | `        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")` |
| 111 | `        return f"[ERROR]: file not found: {file_path}"` | `        raise ToolError(f"file not found: {file_path}", kind="failed")` |
| 113 | `        return f"[ERROR]: file is binary or unsupported: {file_path}"` | `        raise ToolError(f"file is binary or unsupported: {file_path}", kind="failed")` |
| 121 | `        return f"[ERROR]: failed to read file: {exc}"` | `        raise ToolError(f"failed to read file: {exc}", kind="failed")` |
| 140 | `        return "[ERROR]: file_path is required."` | `        raise ToolError("file_path is required", kind="validation")` |
| 144 | `        return f"[ERROR]: content too large (>{_WRITE_MAX_BYTES} bytes)."` | `        raise ToolError(f"content too large (>{_WRITE_MAX_BYTES} bytes).", kind="validation")` |
| 149 | `        return f"[ERROR]: path is outside the project workspace: {file_path}"` | `        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")` |
| 154 | `        return f"[ERROR]: must read_file before overwriting existing file: {file_path}"` | `        raise ToolError(f"must read_file before overwriting existing file: {file_path}", kind="validation")` |
| 164 | `        return f"[ERROR]: failed to write file: {exc}"` | `        raise ToolError(f"failed to write file: {exc}", kind="failed")` |
| 177 | `        return "[ERROR]: file_path is required."` | `        raise ToolError("file_path is required", kind="validation")` |
| 179 | `        return f"[ERROR]: old_string is empty; use write_file to create a new file: {file_path}"` | `        raise ToolError(f"old_string is empty; use write_file to create a new file: {file_path}", kind="validation")` |
| 184 | `        return f"[ERROR]: path is outside the project workspace: {file_path}"` | `        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")` |
| 186 | `        return f"[ERROR]: file not found: {file_path}"` | `        raise ToolError(f"file not found: {file_path}", kind="failed")` |
| 188 | `        return f"[ERROR]: file is binary or unsupported: {file_path}"` | `        raise ToolError(f"file is binary or unsupported: {file_path}", kind="failed")` |
| 192 | `        return f"[ERROR]: must read_file before editing: {file_path}"` | `        raise ToolError(f"must read_file before editing: {file_path}", kind="validation")` |
| 200 | `        return f"[ERROR]: failed to read file: {exc}"` | `        raise ToolError(f"failed to read file: {exc}", kind="failed")` |
| 204 | `        return f"[ERROR]: old_string not found in {file_path}"` | `        raise ToolError(f"old_string not found in {file_path}", kind="failed")` |
| 206 | `        return f"[ERROR]: old_string matches {count} times; set replace_all=True or provide a more specific old_string."` | `        raise ToolError(f"old_string matches {count} times; set replace_all=True or provide a more specific old_string.", kind="validation")` |
| 218 | `        return f"[ERROR]: failed to write file: {exc}"` | `        raise ToolError(f"failed to write file: {exc}", kind="failed")` |
| 232 | `        return f"[ERROR]: path is outside the project workspace: {path}"` | `        raise ToolError(f"path is outside the project workspace: {path}", kind="validation")` |
| 234 | `        return f"[ERROR]: path not found: {path}"` | `        raise ToolError(f"path not found: {path}", kind="failed")` |
| 236 | `        return f"[ERROR]: not a directory: {path}"` | `        raise ToolError(f"not a directory: {path}", kind="failed")` |
| 256 | `        return f"[ERROR]: failed to list directory: {exc}"` | `        raise ToolError(f"failed to list directory: {exc}", kind="failed")` |
| 264 | `        return "[ERROR]: pattern is required."` | `        raise ToolError("pattern is required", kind="validation")` |
| 266 | `        return f"[ERROR]: pattern must not contain '..': {pattern}"` | `        raise ToolError(f"pattern must not contain '..': {pattern}", kind="validation")` |
| 272 | `        return f"[ERROR]: path is outside the project workspace: {path}"` | `        raise ToolError(f"path is outside the project workspace: {path}", kind="validation")` |
| 274 | `        return f"[ERROR]: path not found or not a directory: {path}"` | `        raise ToolError(f"path not found or not a directory: {path}", kind="failed")` |
| 291 | `        return f"[ERROR]: glob failed: {exc}"` | `        raise ToolError(f"glob failed: {exc}", kind="failed")` |

Note: success-path returns (the `json.dumps(...)` / `out` text at `:130-133`, `:167-170`, `:221`, `:257`, `:292` and the `(no content...)` / `...[truncated]` lines) are **unchanged** — only failure `return "[ERROR]: ..."` lines become `raise ToolError(...)`.

- [ ] **Step 2: Update file_tools tests**

In `tests/test_file_tools.py`, the failure-path tests currently assert substring/`startswith("[ERROR]:")` on the invoke return value. Convert them to `pytest.raises(ToolError, match=...)`. Add import:
```python
import pytest
from twinkle.agentserver.tools.errors import ToolError
```

| Test (line) | old assertion | new assertion |
|---|---|---|
| `test_glob_rejects_dotdot` (`:280-282`) | `out = _invoke(file_tools.glob, pattern="../**"); assert "must not contain '..'" in out` | `with pytest.raises(ToolError, match="must not contain '\\.\\.'"): _invoke(file_tools.glob, pattern="../**")` |
| `test_glob_escape_base_rejected` (`:285-287`) | `out = _invoke(file_tools.glob, pattern="*.py", path="../../"); assert "outside the project workspace" in out` | `with pytest.raises(ToolError, match="outside the project workspace"): _invoke(file_tools.glob, pattern="*.py", path="../../")` |
| `test_glob_absolute_pattern_returns_error` (`:290-295`) | `out = _invoke(file_tools.glob, pattern="/abs/*"); assert out.startswith("[ERROR]:"); assert "glob failed" in out` | `with pytest.raises(ToolError, match="glob failed"): _invoke(file_tools.glob, pattern="/abs/*")` |

Also scan `tests/test_file_tools.py` for any other `assert "[ERROR]"` / `in out` failure asserts (e.g. empty path, workspace escape in read/write/edit) and convert them with the same pattern. If a test asserts a successful return, leave it. Run `grep -n "\[ERROR\]\|in out" tests/test_file_tools.py` to find them all.

- [ ] **Step 3: Run tests to verify**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "refactor(file_tools): soft errors raise ToolError instead of [ERROR]: string"
```

---

### Task 5: command_exec.py soft errors → raise ToolError (7 sites)

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/command_exec.py` (`:124,128,133,153,155,172,174`)
- Test: `tests/test_command_exec.py`

- [ ] **Step 1: Add import + convert sites**

Add import:
```python
from twinkle.agentserver.tools.errors import ToolError
```

| Line | old | new |
|---|---|---|
| 124 | `        return "[ERROR]: command cannot be empty."` | `        raise ToolError("command cannot be empty", kind="validation")` |
| 128 | `        return f"[ERROR]: command rejected for safety ({blocked_reason})."` | `        raise ToolError(f"command rejected for safety ({blocked_reason})", kind="denied")` |
| 133 | `        return "[ERROR]: workdir is outside the project workspace."` | `        raise ToolError("workdir is outside the project workspace", kind="validation")` |
| 153 | `            return f"[ERROR]: command failed to start: {exc}"` | `            raise ToolError(f"command failed to start: {exc}", kind="failed")` |
| 155 | `            return f"[ERROR]: background command failed: {err}"` | `            raise ToolError(f"background command failed: {err}", kind="failed")` |
| 172 | `        return f"[ERROR]: command timed out after {timeout_seconds}s."` | `        raise ToolError(f"command timed out after {timeout_seconds}s", kind="failed")` |
| 174 | `        return f"[ERROR]: command execution failed: {exc}"` | `        raise ToolError(f"command execution failed: {exc}", kind="failed")` |

Note: the success-path `return json.dumps({...})` at `:156-165` and `:176-186` (including non-zero exit code — that is normal output, not a failure) is **unchanged**.

- [ ] **Step 2: Update command_exec tests**

In `tests/test_command_exec.py`:
```python
import pytest
from twinkle.agentserver.tools.errors import ToolError
```

| Test (line) | old | new |
|---|---|---|
| `test_rejects_empty_command` (`:16-17`) | `assert asyncio.run(command_exec.command_exec.invoke({"command": ""})) == "[ERROR]: command cannot be empty."` | `with pytest.raises(ToolError, match="command cannot be empty"): asyncio.run(command_exec.command_exec.invoke({"command": ""}))` |
| `test_blocks_dangerous_pattern` (`:20-23`) | `out = asyncio.run(...); assert "rejected for safety" in out; assert "rm -rf" in out` | `with pytest.raises(ToolError, match="rejected for safety"): asyncio.run(command_exec.command_exec.invoke({"command": "rm -rf /"}))` and add a second assert inside: the match string can be `r"rejected for safety.*rm -rf"` to cover both |
| `test_rejects_workdir_escape` (`:26-27`) | `out = asyncio.run(...); assert "outside the project workspace" in out` | `with pytest.raises(ToolError, match="outside the project workspace"): asyncio.run(command_exec.command_exec.invoke({"command": "echo hi", "workdir": "../../"}))` |

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_command_exec.py tests/test_command_exec_safety.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/tools/builtin/command_exec.py tests/test_command_exec.py
git commit -m "refactor(command_exec): soft errors raise ToolError"
```

---

### Task 6: web_search.py + web_fetch.py soft errors → raise ToolError (4 sites)

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/web_search.py:228,242`
- Modify: `twinkle/agentserver/tools/builtin/web_fetch.py:148,179`
- Test: `tests/test_web_search.py`, `tests/test_web_fetch.py`

- [ ] **Step 1: Convert web_search**

Add import in `web_search.py`, then:

| Line | old | new |
|---|---|---|
| 228 | `        return "[error] empty query"` | `        raise ToolError("empty query", kind="validation")` |
| 242 | `    return f"[error] search engines unavailable: {' | '.join(errors)}"` | `    raise ToolError(f"search engines unavailable: {' | '.join(errors)}", kind="unavailable")` |

- [ ] **Step 2: Convert web_fetch**

Add import in `web_fetch.py`, then:

| Line | old | new |
|---|---|---|
| 148 | `        return "[error] empty url"` | `        raise ToolError("empty url", kind="validation")` |
| 179 | `    return f"[error] fetch failed: {' | '.join(errors)}"` | `    raise ToolError(f"fetch failed: {' | '.join(errors)}", kind="failed")` |

- [ ] **Step 3: Update web tests**

In `tests/test_web_search.py` and `tests/test_web_fetch.py` add:
```python
import pytest
from twinkle.agentserver.tools.errors import ToolError
```

web_search failure asserts (`:102`, `:148`): `assert "[error]" in out.lower()` → `with pytest.raises(ToolError, match="<the failure reason>"): asyncio.run(web_search.web_search.invoke({...}))`. The success-path asserts (`:102` is in a success test? — verify by reading each test's intent: if the test name says success/empty, check whether it expects a return or a raise; the `empty query` case raises).

web_fetch failure asserts:
| Line | old | new |
|---|---|---|
| `:111-113` | `out = ...; assert "[error]" in out.lower(); assert "TAVILY_API_KEY" in out; assert "denied" not in out` | `with pytest.raises(ToolError, match="TAVILY_API_KEY"): asyncio.run(web_fetch.web_fetch.invoke({"url": "https://en.wikipedia.org/wiki/Moon"}))` (the "denied not leaked" assert is now structurally guaranteed — the body never reaches content) |
| `:131-134` | `assert "[error]" in out.lower(); assert "403" in out; assert "tavily" in out.lower()` | `with pytest.raises(ToolError, match=r"403.*tavily"): asyncio.run(...)` (match is case-insensitive via `re` — use `match=r"(?i)403.*tavily"` if needed) |
| `:140` | `out = ...; assert "[error]" in out.lower()` | `with pytest.raises(ToolError, match="empty url"): asyncio.run(web_fetch.web_fetch.invoke({"url": "   "}))` |

The success path `:94` (`assert "[error]" not in out.lower()` — the Tavily-success test) is **unchanged**: success still returns content, and content no longer contains `[error]` so the assert stays green (it is now trivially true but harmless; do not touch to keep the diff minimal).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_web_search.py tests/test_web_fetch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/web_search.py twinkle/agentserver/tools/builtin/web_fetch.py tests/test_web_search.py tests/test_web_fetch.py
git commit -m "refactor(web_tools): soft errors raise ToolError"
```

---

### Task 7: team_tools.py + subagent/tools.py unavailable → raise ToolError (9 sites)

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/team_tools.py` (`:32,45,63,87,104,117,127,144`)
- Modify: `twinkle/agentserver/tools/builtin/subagent/tools.py:57`
- Test: `tests/test_team_tools.py`, `tests/test_subagent_tools.py`

- [ ] **Step 1: Convert team_tools**

Add import in `team_tools.py`, then replace all 8 occurrences of `return "[team unavailable]"` with:
```python
        raise ToolError("team feature not initialized on this loop", kind="unavailable")
```
(apply at lines `:32,45,63,87,104,117,127,144` — each is `team = CURRENT_TEAM.get(); if team is None: return "[team unavailable]"`; only the `return` line changes to `raise`).

Note: the `except TodoError as exc: return f"Error: {exc}"` blocks (`:50-51,68-69,95-96,108-109`) are **NOT** changed — TodoError is the todo store's own exception and returns a formatted string by design (not a tool-failure prefix). Leave them.

- [ ] **Step 2: Convert subagent/tools**

Add import in `subagent/tools.py`, then:
| Line | old | new |
|---|---|---|
| 57 | `        return "[subagent unavailable] executor not initialized on this loop"` | `        raise ToolError("subagent executor not initialized on this loop", kind="unavailable")` |

- [ ] **Step 3: Update team/subagent tests**

In `tests/test_team_tools.py`:
```python
import pytest
from twinkle.agentserver.tools.errors import ToolError
```
| Test (line) | old | new |
|---|---|---|
| `test_send_member_no_contextvar` (`:22-25`) | `out = asyncio.run(team_tools.send_member.func("researcher", "hi")); assert "team unavailable" in out` | `with pytest.raises(ToolError, match="team feature not initialized"): asyncio.run(team_tools.send_member.func("researcher", "hi"))` |

Scan `tests/test_team_tools.py` for any other `team unavailable` / `[team` asserts (other unavailable tests for create_task/claim_task/etc. with no ContextVar) and convert with the same pattern. Run `grep -n "team unavailable\|\[team" tests/test_team_tools.py` to find them all.

In `tests/test_subagent_tools.py`: find the test asserting `"[subagent unavailable]"` (or `"subagent unavailable"`) on invoke-without-executor and convert to `pytest.raises(ToolError, match="subagent executor not initialized")`. Run `grep -n "unavailable\|subagent" tests/test_subagent_tools.py` to locate.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_team_tools.py tests/test_subagent_tools.py tests/test_team_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/team_tools.py twinkle/agentserver/tools/builtin/subagent/tools.py tests/test_team_tools.py tests/test_subagent_tools.py
git commit -m "refactor(team/subagent): unavailable raises ToolError"
```

---

### Task 8: Full regression + verify no stray prefixes remain

- [ ] **Step 1: Confirm no leftover soft-error prefixes in tool code**

Run: `grep -rn '\[ERROR\]\|\[error\]\|\[team unavailable\]\|\[subagent unavailable\]' twinkle/agentserver/tools/`
Expected: **no matches** in `tools/builtin/` or `tools/manager.py`. (The only allowed `[tool error]` literal is now inside `format_tool_error` via `TOOL_ERROR_PREFIX`; orphan `[interrupted:` in `agent.py:857` is out of scope and allowed.)

- [ ] **Step 2: Full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS (pre-existing environmental failures per memory `phase6-cron-tests-environmental-failures.md` — croniter-not-installed + Windows mojibake/sidecar — are allowed; everything else green).

- [ ] **Step 3: Verify observability net-positive (no code change, just confirm)**

The instrumentor (`instrumentors/tool.py`) is unchanged. Soft errors now `raise` → they hit the `except` branch (`:42-46`, `error=True`, `record_exception`) instead of the dead `startswith` branch (`:34`). Confirm by running any web_fetch failure test with `TWINKLE_OTEL=1` if a local collector is up (per memory `labubu-local-collector.md`) — the tool span should now record an exception for soft errors. This is a confirm-only step; do not edit the instrumentor.

- [ ] **Step 4: Commit regression fixups (if any test surfaced an issue)**

```bash
git add -A
git commit -m "test: fix tool-error standardization regressions"
```

- [ ] **Step 5: Final commit (if squashing the feature)**

Only if the user asks. Per memory `no-direct-github-push.md`, do NOT push.

---

## Self-Review

**1. Spec coverage:** Spec = unify all tool-failure returns into ToolError + format_tool_error, unified `[tool error]` prefix, kind not in content, openclaw throw-on-failure contract.
- ToolError + format_tool_error → Task 1 ✓
- unknown tool → Task 2 ✓
- catch-all + denied → Task 3 ✓
- file_tools soft errors → Task 4 ✓
- command_exec soft errors → Task 5 ✓
- web_search/web_fetch → Task 6 ✓
- team/subagent unavailable → Task 7 ✓
- regression → Task 8 ✓
- Out of scope explicitly: orphan, gateway [error], permission-hook deny_message, instrumentor — documented in Key Design Decisions §4 ✓

**2. Placeholder scan:** No "TBD"/"implement later"/"similar to Task N". The two `grep`-to-find-all instructions (Task 4 Step 2, Task 7 Step 3) are concrete discovery commands, not placeholders — they name the exact pattern to grep and the exact conversion to apply. ✓

**3. Type consistency:** `ToolError(message, *, kind="failed")` — signature identical in Task 1 (definition), Task 2 (`raise ToolError(f"unknown tool: {name}", kind="validation")`), Tasks 4-7. `format_tool_error(source)` accepts `str | BaseException` — called as `format_tool_error(exc)` (Task 3 catch-all), `format_tool_error(ToolError(...))` (implied), `format_tool_error(f"...")` (Task 3 denied). `TOOL_ERROR_PREFIX` imported from `twinkle.observability.attributes` consistently. ✓

**One caveat to flag to reviewer:** Task 6 web test matches like `match=r"(?i)403.*tavily"` assume the failure message contains both substrings in order — verify against the actual `f"fetch failed: {' | '.join(errors)}"` content (the `errors` list entries) when executing; adjust the regex to the real message. This is the only step where the exact message text depends on runtime error aggregation and must be confirmed at execution time.
