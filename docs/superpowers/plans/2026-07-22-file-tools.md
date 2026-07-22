# File Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 file-operation tools (`read_file`, `write_file`, `edit_file`, `list_files`, `glob`) to Twinkle's agent, confined to the workspace and guarded by read-before-write.

**Architecture:** One self-contained module `twinkle/agentserver/tools/builtin/file_tools.py` with 5 `@tool` async functions + internal helpers (path confinement, binary detection, per-session read registry). Mirrors `command_exec`'s workspace-confinement model and `todo_tools`' session routing. No framework-layer or `command_exec` changes; registration is a single `ToolManager.register()` hop per tool in `tool_manager()`.

**Tech Stack:** Python (stdlib only: `pathlib`, `os`, `asyncio`, `json`), pytest with `asyncio.run()` (no pytest-asyncio), `monkeypatch` + `tmp_path` fixtures.

**Spec:** `docs/superpowers/specs/2026-07-22-file-tools-design.md`

**Note on commits:** Each task ends with a commit step (TDD frequent-commits). The user has deferred committing the spec; whether implementation commits proceed is confirmed at execution handoff. We are on `main` (the user's usual branch — recent history is direct-to-main).

**Assume venv:** All `python -m pytest` commands assume the project venv is active (`.venv/Scripts/python.exe` per CLAUDE.md).

---

## File Structure

- **Create:** `twinkle/agentserver/tools/builtin/file_tools.py` — 5 `@tool` functions + helpers (`_resolve_file_path`, `_is_binary`, `FileReadRegistry`, `_registry`). One responsibility: file ops under the workspace.
- **Create:** `tests/test_file_tools.py` — unit tests for helpers + 5 tools. One responsibility: verify `file_tools` behavior.
- **Modify:** `twinkle/agentserver/tools/__init__.py` — import `file_tools` + register 5 tools in `tool_manager()`.
- **Modify:** `tests/test_tool_manager.py` — add registration assertion for the 5 file tools.
- **Modify:** `docs/architecture.md` — sync §10 builtin listing to include `file_tools.py` + `test_file_tools.py` (the doc promises "与代码库同步维护").

---

## Task 1: Module skeleton + path/binary/registry helpers

**Files:**
- Create: `twinkle/agentserver/tools/builtin/file_tools.py`
- Test: `tests/test_file_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_file_tools.py`:

```python
import asyncio
import json

import pytest

from twinkle.agentserver.tools.builtin import file_tools


@pytest.fixture
def ws(monkeypatch, tmp_path):
    """Point file_tools at a tmp workspace with a fixed session id."""
    monkeypatch.setattr(file_tools, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(file_tools, "get_plan_todo_session_id", lambda: "test-sid")
    file_tools._registry.clear("test-sid")
    return tmp_path


def _invoke(tool, **args):
    return asyncio.run(tool.invoke(args))


# --- _resolve_file_path ---

def test_resolve_relative_under_workspace(ws):
    p = file_tools._resolve_file_path("a/b.txt")
    assert p == (ws / "a" / "b.txt").resolve()


def test_resolve_absolute_inside_workspace(ws):
    p = file_tools._resolve_file_path(str(ws / "c.txt"))
    assert p == (ws / "c.txt").resolve()


def test_resolve_rejects_relative_escape(ws):
    with pytest.raises(ValueError):
        file_tools._resolve_file_path("../../escape.txt")


def test_resolve_rejects_absolute_escape(ws):
    with pytest.raises(ValueError):
        file_tools._resolve_file_path(str(ws.parent / "outside.txt"))


# --- _is_binary ---

def test_is_binary_by_extension(ws):
    (ws / "x.png").write_bytes(b"not really png")
    assert file_tools._is_binary(ws / "x.png") is True


def test_is_binary_by_null_byte(ws):
    (ws / "x.dat").write_bytes(b"abc\x00def")
    assert file_tools._is_binary(ws / "x.dat") is True


def test_is_text_not_binary(ws):
    (ws / "x.txt").write_text("hello world", encoding="utf-8")
    assert file_tools._is_binary(ws / "x.txt") is False


# --- FileReadRegistry ---

def test_registry_mark_and_has_read():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    assert reg.has_read("s1", "/p/a") is True
    assert reg.has_read("s1", "/p/b") is False


def test_registry_session_isolation():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    assert reg.has_read("s2", "/p/a") is False


def test_registry_clear():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    reg.clear("s1")
    assert reg.has_read("s1", "/p/a") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.builtin.file_tools'` (the module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
"""file_tools — read/write/edit/list/glob files under the workspace.

Reference: openjiuwen SDK harness/tools/filesystem.py (2032 lines, 6 tools).
Twinkle keeps 5 (read_file/write_file/edit_file/list_files/glob); drops grep
(use command_exec rg/findstr), delete/move (use command_exec rm/mv), image/
PDF/Notebook read, mtime/size stale-write check, .agent_history, OS sandbox,
approval rails.

Safety (approach b): workspace path confinement (mirror command_exec
_resolve_workdir) + forced read-before-write via a per-session FileReadRegistry
(prevents blind overwrite); no stale check.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from twinkle.agentserver.plan_todo_context import get_plan_todo_session_id
from twinkle.agentserver.tools.decorator import tool
from twinkle.config import WORKSPACE_DIR

_BINARY_EXTS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".pdf", ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".ipynb",
}
_WRITE_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB


def _resolve_file_path(file_path: str) -> Path:
    """Resolve `file_path` against WORKSPACE_DIR; reject paths escaping it.

    Relative paths are joined under WORKSPACE_DIR; absolute paths are accepted
    only if they resolve inside it. Raises ValueError on escape.
    """
    root = Path(WORKSPACE_DIR).resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(root)  # raises ValueError if it escapes the workspace
    return candidate


def _is_binary(path: Path) -> bool:
    """Heuristic: known binary extensions, or a NUL byte in the first 8 KiB."""
    if path.suffix.lower() in _BINARY_EXTS:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False  # let the caller surface read errors uniformly
    return b"\x00" in chunk


class FileReadRegistry:
    """Per-session set of resolved paths the agent has read this session.

    Drives the read-before-write guard for write_file/edit_file. Sync methods:
    set.add / membership are atomic on a single event loop (no await inside,
    no TOCTOU), so no asyncio.Lock is needed (a long-lived lock would also bind
    to one event loop and break across asyncio.run test loops).
    """

    def __init__(self) -> None:
        self._read: dict[str, set[str]] = {}

    def mark_read(self, sid: str, path: str) -> None:
        self._read.setdefault(sid, set()).add(path)

    def has_read(self, sid: str, path: str) -> bool:
        return path in self._read.get(sid, set())

    def clear(self, sid: str) -> None:
        self._read.pop(sid, None)


_registry = FileReadRegistry()  # module-level singleton; session-routed via ContextVar
```

(Note: `tool`, `json`, `asyncio` are imported now because the 5 tools appended in Tasks 2–6 use them.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all 10 helper tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add file_tools skeleton + path/binary/read-registry helpers"
```

---

## Task 2: read_file

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (append `read_file`)
- Test: `tests/test_file_tools.py` (append read tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_tools.py`:

```python
# --- read_file ---

def test_read_file_returns_content_and_marks_registry(ws):
    (ws / "a.txt").write_text("line1\nline2\nline3", encoding="utf-8")
    out = _invoke(file_tools.read_file, file_path="a.txt")
    assert out == "line1\nline2\nline3"
    assert file_tools._registry.has_read("test-sid", str((ws / "a.txt").resolve())) is True


def test_read_file_not_found(ws):
    out = _invoke(file_tools.read_file, file_path="missing.txt")
    assert "file not found" in out


def test_read_file_binary_rejected(ws):
    (ws / "b.png").write_bytes(b"\x89PNG\r\n\x00")
    out = _invoke(file_tools.read_file, file_path="b.png")
    assert "binary or unsupported" in out


def test_read_file_pagination(ws):
    (ws / "p.txt").write_text("\n".join(f"l{i}" for i in range(50)), encoding="utf-8")
    out = _invoke(file_tools.read_file, file_path="p.txt", offset=10, limit=5)
    assert "l10" in out and "l14" in out
    assert "l15" not in out
    assert "truncated" in out and "50 total lines" in out


def test_read_file_escape_rejected(ws):
    out = _invoke(file_tools.read_file, file_path="../../outside.txt")
    assert "outside the project workspace" in out


def test_read_file_empty_path(ws):
    assert "file_path is required" in _invoke(file_tools.read_file, file_path="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `AttributeError: module 'twinkle.agentserver.tools.builtin.file_tools' has no attribute 'read_file'` (the tool is not defined yet).

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
@tool
async def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a text file under the workspace with offset/limit pagination. Records the read so write_file/edit_file can enforce read-before-write. Rejects binary files."""
    if not file_path:
        return "[ERROR]: file_path is required."
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 2000
    offset = max(0, offset)
    limit = max(1, min(limit, 2000))

    try:
        resolved = _resolve_file_path(file_path)
    except ValueError:
        return f"[ERROR]: path is outside the project workspace: {file_path}"
    if not resolved.is_file():
        return f"[ERROR]: file not found: {file_path}"
    if await asyncio.to_thread(_is_binary, resolved):
        return f"[ERROR]: file is binary or unsupported: {file_path}"

    def _read() -> str:
        return resolved.read_text(encoding="utf-8", errors="replace")

    try:
        content = await asyncio.to_thread(_read)
    except OSError as exc:
        return f"[ERROR]: failed to read file: {exc}"

    sid = get_plan_todo_session_id()
    _registry.mark_read(sid, str(resolved))
    lines = content.splitlines(keepends=True)
    total = len(lines)
    selected = lines[offset:offset + limit]
    out = "".join(selected)
    if not out:
        return f"(no content at offset {offset}; {total} total lines)"
    if total > offset + limit:
        out += f"\n...[truncated, {total} total lines, use offset to page]"
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all read_file tests + prior helper tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add read_file tool"
```

---

## Task 3: write_file (read-before-write guard)

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (append `write_file`)
- Test: `tests/test_file_tools.py` (append write tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_tools.py`:

```python
# --- write_file ---

def test_write_file_creates_new(ws):
    out = _invoke(file_tools.write_file, file_path="new.txt", content="hello")
    payload = json.loads(out)
    assert payload["type"] == "create"
    assert payload["bytes_written"] == 5
    assert (ws / "new.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_overwrite_requires_prior_read(ws):
    (ws / "e.txt").write_text("existing", encoding="utf-8")
    out = _invoke(file_tools.write_file, file_path="e.txt", content="new")
    assert "must read_file before overwriting" in out


def test_write_file_overwrite_after_read(ws):
    (ws / "e.txt").write_text("existing", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.write_file, file_path="e.txt", content="new")
    payload = json.loads(out)
    assert payload["type"] == "update"
    assert (ws / "e.txt").read_text(encoding="utf-8") == "new"


def test_write_file_too_large(ws):
    out = _invoke(file_tools.write_file, file_path="big.txt", content="x" * (5 * 1024 * 1024 + 1))
    assert "content too large" in out


def test_write_file_creates_parent_dirs(ws):
    _invoke(file_tools.write_file, file_path="sub/dir/n.txt", content="x")
    assert (ws / "sub" / "dir" / "n.txt").exists()


def test_write_file_escape_rejected(ws):
    out = _invoke(file_tools.write_file, file_path="../../out.txt", content="x")
    assert "outside the project workspace" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'write_file'`.

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
@tool
async def write_file(file_path: str, content: str) -> str:
    """Write full content to a file under the workspace. Overwriting an existing file requires a prior read_file in this session; new files can be created directly. Content capped at 5 MiB."""
    if not file_path:
        return "[ERROR]: file_path is required."
    content = content or ""
    data = content.encode("utf-8")
    if len(data) > _WRITE_MAX_BYTES:
        return f"[ERROR]: content too large (>{_WRITE_MAX_BYTES} bytes)."

    try:
        resolved = _resolve_file_path(file_path)
    except ValueError:
        return f"[ERROR]: path is outside the project workspace: {file_path}"

    sid = get_plan_todo_session_id()
    existed = resolved.is_file()
    if existed and not _registry.has_read(sid, str(resolved)):
        return f"[ERROR]: must read_file before overwriting existing file: {file_path}"

    def _write() -> str:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)
        return "update" if existed else "create"

    try:
        kind = await asyncio.to_thread(_write)
    except OSError as exc:
        return f"[ERROR]: failed to write file: {exc}"

    _registry.mark_read(sid, str(resolved))
    return json.dumps(
        {"file_path": file_path, "bytes_written": len(data), "type": kind},
        ensure_ascii=False,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all write_file tests + prior tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add write_file tool with read-before-write guard"
```

---

## Task 4: edit_file

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (append `edit_file`)
- Test: `tests/test_file_tools.py` (append edit tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_tools.py`:

```python
# --- edit_file ---

def test_edit_file_requires_prior_read(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz")
    assert "must read_file before editing" in out


def test_edit_file_single_replace(ws):
    (ws / "e.txt").write_text("foo bar baz", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="X")
    payload = json.loads(out)
    assert payload["replacements"] == 1
    assert (ws / "e.txt").read_text(encoding="utf-8") == "X bar baz"


def test_edit_file_multiple_without_replace_all_rejected(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz")
    assert "matches 2 times" in out


def test_edit_file_replace_all(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz", replace_all=True)
    payload = json.loads(out)
    assert payload["replacements"] == 2
    assert (ws / "e.txt").read_text(encoding="utf-8") == "baz bar baz"


def test_edit_file_old_string_not_found(ws):
    (ws / "e.txt").write_text("hello", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="zzz", new_string="y")
    assert "old_string not found" in out


def test_edit_file_empty_old_string_rejected(ws):
    (ws / "e.txt").write_text("hello", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="", new_string="y")
    assert "use write_file to create" in out


def test_edit_file_chain_after_write(ws):
    _invoke(file_tools.write_file, file_path="w.txt", content="aabbcc")
    out = _invoke(file_tools.edit_file, file_path="w.txt", old_string="bb", new_string="BB")
    payload = json.loads(out)
    assert payload["replacements"] == 1
    assert (ws / "w.txt").read_text(encoding="utf-8") == "aaBBcc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'edit_file'`.

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
@tool
async def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace old_string with new_string in a file under the workspace. Requires a prior read_file in this session. old_string must be non-empty (use write_file for new files). Set replace_all to replace multiple occurrences."""
    if not file_path:
        return "[ERROR]: file_path is required."
    if not old_string:
        return f"[ERROR]: old_string is empty; use write_file to create a new file: {file_path}"

    try:
        resolved = _resolve_file_path(file_path)
    except ValueError:
        return f"[ERROR]: path is outside the project workspace: {file_path}"
    if not resolved.is_file():
        return f"[ERROR]: file not found: {file_path}"
    if await asyncio.to_thread(_is_binary, resolved):
        return f"[ERROR]: file is binary or unsupported: {file_path}"

    sid = get_plan_todo_session_id()
    if not _registry.has_read(sid, str(resolved)):
        return f"[ERROR]: must read_file before editing: {file_path}"

    def _read() -> str:
        return resolved.read_text(encoding="utf-8", errors="replace")

    try:
        content = await asyncio.to_thread(_read)
    except OSError as exc:
        return f"[ERROR]: failed to read file: {exc}"

    count = content.count(old_string)
    if count == 0:
        return f"[ERROR]: old_string not found in {file_path}"
    if count > 1 and not replace_all:
        return f"[ERROR]: old_string matches {count} times; set replace_all=True or provide a more specific old_string."
    # str.replace replaces ALL by default; single replace needs an explicit count of 1.
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    n = count if replace_all else 1

    def _write() -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(new_content.encode("utf-8"))

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        return f"[ERROR]: failed to write file: {exc}"

    _registry.mark_read(sid, str(resolved))
    return json.dumps({"file_path": file_path, "replacements": n}, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all edit_file tests + prior tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add edit_file tool"
```

---

## Task 5: list_files

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (append `list_files`)
- Test: `tests/test_file_tools.py` (append list tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_tools.py`:

```python
# --- list_files ---

def test_list_files_lists_dir(ws):
    (ws / "a.txt").write_text("x")
    (ws / "b.py").write_text("y")
    (ws / "sub").mkdir()
    out = _invoke(file_tools.list_files, path=".")
    payload = json.loads(out)
    names = {e["name"] for e in payload["entries"]}
    assert names == {"a.txt", "b.py", "sub"}
    types = {e["name"]: e["type"] for e in payload["entries"]}
    assert types["a.txt"] == "file"
    assert types["sub"] == "dir"


def test_list_files_hidden_filtered_by_default(ws):
    (ws / ".hidden").write_text("x")
    (ws / "visible.txt").write_text("y")
    out = _invoke(file_tools.list_files, path=".")
    payload = json.loads(out)
    names = {e["name"] for e in payload["entries"]}
    assert "visible.txt" in names
    assert ".hidden" not in names


def test_list_files_show_hidden(ws):
    (ws / ".hidden").write_text("x")
    out = _invoke(file_tools.list_files, path=".", show_hidden=True)
    payload = json.loads(out)
    assert ".hidden" in {e["name"] for e in payload["entries"]}


def test_list_files_not_a_dir(ws):
    (ws / "f.txt").write_text("x")
    out = _invoke(file_tools.list_files, path="f.txt")
    assert "not a directory" in out


def test_list_files_escape_rejected(ws):
    out = _invoke(file_tools.list_files, path="../../")
    assert "outside the project workspace" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_files'`.

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
@tool
async def list_files(path: str = ".", show_hidden: bool = False) -> str:
    """List entries in a directory under the workspace. Set show_hidden to include dotfiles."""
    if not path:
        path = "."
    try:
        resolved = _resolve_file_path(path)
    except ValueError:
        return f"[ERROR]: path is outside the project workspace: {path}"
    if not resolved.exists():
        return f"[ERROR]: path not found: {path}"
    if not resolved.is_dir():
        return f"[ERROR]: not a directory: {path}"

    def _scan() -> list[dict]:
        entries = []
        with os.scandir(resolved) as it:
            for e in sorted(it, key=lambda x: x.name):
                if not show_hidden and e.name.startswith("."):
                    continue
                if e.is_dir():
                    t = "dir"
                elif e.is_file():
                    t = "file"
                else:
                    t = "other"
                entries.append({"name": e.name, "type": t})
        return entries

    try:
        entries = await asyncio.to_thread(_scan)
    except OSError as exc:
        return f"[ERROR]: failed to list directory: {exc}"
    return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all list_files tests + prior tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add list_files tool"
```

---

## Task 6: glob

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/file_tools.py` (append `glob`)
- Test: `tests/test_file_tools.py` (append glob tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_tools.py`:

```python
# --- glob ---

def test_glob_matches_non_recursive(ws):
    (ws / "a.py").write_text("x")
    (ws / "b.txt").write_text("y")
    (ws / "sub").mkdir()
    (ws / "sub" / "c.py").write_text("z")
    out = _invoke(file_tools.glob, pattern="*.py")
    payload = json.loads(out)
    assert payload["matches"] == ["a.py"]


def test_glob_recursive(ws):
    (ws / "a.py").write_text("x")
    (ws / "sub").mkdir()
    (ws / "sub" / "c.py").write_text("z")
    out = _invoke(file_tools.glob, pattern="**/*.py")
    payload = json.loads(out)
    assert payload["matches"]  # non-empty
    assert any("c.py" in m for m in payload["matches"])  # recursion reached the subdir


def test_glob_rejects_dotdot(ws):
    out = _invoke(file_tools.glob, pattern="../**")
    assert "must not contain '..'" in out


def test_glob_escape_base_rejected(ws):
    out = _invoke(file_tools.glob, pattern="*.py", path="../../")
    assert "outside the project workspace" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'glob'`.

- [ ] **Step 3: Write minimal implementation**

Append to `twinkle/agentserver/tools/builtin/file_tools.py`:

```python
@tool
async def glob(pattern: str, path: str = ".") -> str:
    """Find files under the workspace matching a glob pattern (stdlib pathlib, no ripgrep). path is the base directory; pattern must not contain '..'."""
    if not pattern:
        return "[ERROR]: pattern is required."
    if ".." in pattern:
        return f"[ERROR]: pattern must not contain '..': {pattern}"
    if not path:
        path = "."
    try:
        resolved = _resolve_file_path(path)
    except ValueError:
        return f"[ERROR]: path is outside the project workspace: {path}"
    if not resolved.is_dir():
        return f"[ERROR]: path not found or not a directory: {path}"

    root = Path(WORKSPACE_DIR).resolve()

    def _glob() -> list[str]:
        matches = []
        for p in resolved.glob(pattern):
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                continue  # defense-in-depth: drop any result that escapes the workspace
            matches.append(str(rel))
        return sorted(matches)

    try:
        matches = await asyncio.to_thread(_glob)
    except OSError as exc:
        return f"[ERROR]: glob failed: {exc}"
    return json.dumps({"pattern": pattern, "path": path, "matches": matches}, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_tools.py -v`
Expected: PASS — all glob tests + prior tests green.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/file_tools.py tests/test_file_tools.py
git commit -m "tools: add glob tool"
```

---

## Task 7: Register file tools in tool_manager() + verify

**Files:**
- Modify: `twinkle/agentserver/tools/__init__.py` (import + 5 registers)
- Test: `tests/test_tool_manager.py` (add registration tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_manager.py`:

```python
def test_tool_manager_registers_file_tools() -> None:
    tm = tool_manager()
    names = {t.card.name for t in tm.list()}
    assert {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "glob",
    } <= names


def test_file_tool_schemas_have_required_params() -> None:
    tm = tool_manager()
    by_name = {s["function"]["name"]: s for s in tm.schemas()}
    assert by_name["read_file"]["function"]["parameters"]["required"] == ["file_path"]
    assert by_name["write_file"]["function"]["parameters"]["required"] == ["file_path", "content"]
    assert by_name["edit_file"]["function"]["parameters"]["required"] == ["file_path", "old_string", "new_string"]
    assert by_name["list_files"]["function"]["parameters"]["required"] == []
    assert by_name["glob"]["function"]["parameters"]["required"] == ["pattern"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_manager.py -v`
Expected: FAIL — `KeyError: 'read_file'` (the tools are not registered in `tool_manager()` yet).

- [ ] **Step 3: Write minimal implementation**

Modify `twinkle/agentserver/tools/__init__.py`. Two edits:

Edit A — add `file_tools` to the builtin import line:

old:
```python
from twinkle.agentserver.tools.builtin import command_exec, todo_tools, web_fetch, web_search
```
new:
```python
from twinkle.agentserver.tools.builtin import command_exec, file_tools, todo_tools, web_fetch, web_search
```

Edit B — register the 5 file tools in `tool_manager()` (insert after the `command_exec` line):

old:
```python
    tm.register(command_exec.command_exec)
    tm.register(todo_tools.todo_create)
```
new:
```python
    tm.register(command_exec.command_exec)
    tm.register(file_tools.read_file)
    tm.register(file_tools.write_file)
    tm.register(file_tools.edit_file)
    tm.register(file_tools.list_files)
    tm.register(file_tools.glob)
    tm.register(todo_tools.todo_create)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_manager.py tests/test_file_tools.py -v`
Expected: PASS — new registration tests green + existing tool_manager tests still green (they use `<=` subset assertions, so adding tools does not break them).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/__init__.py tests/test_tool_manager.py
git commit -m "tools: register file tools in tool_manager()"
```

---

## Task 8: Full suite run + sync architecture doc

**Files:**
- Modify: `docs/architecture.md` (§10 listing)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — the entire suite green, including the new `test_file_tools.py` (≈35 tests) and the two new `test_tool_manager.py` tests. No existing test regressed.

- [ ] **Step 2: Sync docs/architecture.md §10 builtin + tests listing**

Edit A — add `file_tools.py` to the `builtin/` listing (§10):

old:
```
      builtin/             # 具体工具（web/shell/todo）：框架/实现分层
        web_fetch.py          # URL → markdown/文本
        web_search.py         # DuckDuckGo Lite 搜索
        command_exec.py       # 跨平台 shell 执行（blocklist + workspace 收敛 + 超时 + 后台）
        todo_tools.py         # @tool todo 工具：create / complete / list
```
new:
```
      builtin/             # 具体工具（web/shell/file/todo）：框架/实现分层
        web_fetch.py          # URL → markdown/文本
        web_search.py         # DuckDuckGo Lite 搜索
        command_exec.py       # 跨平台 shell 执行（blocklist + workspace 收敛 + 超时 + 后台）
        file_tools.py         # @tool 文件工具：read_file / write_file / edit_file / list_files / glob（workspace 收敛 + 先读后写）
        todo_tools.py         # @tool todo 工具：create / complete / list
```

Edit B — add `test_file_tools.py` to the tests listing (§10):

old:
```
  test_web_fetch.py         # web_fetch 单测
  test_web_search.py        # web_search 单测
```
new:
```
  test_web_fetch.py         # web_fetch 单测
  test_web_search.py        # web_search 单测
  test_file_tools.py        # 文件工具单测
```

(The §10 tests listing is already partially stale — missing `test_command_exec.py`, `test_todo_*.py`, `test_plan_todo_context.py`, `test_observability.py`, and it duplicates `test_tool_manager.py`. That broader cleanup is out of scope for this plan; only the two file-tools lines are added here.)

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: sync architecture.md §10 with file tools"
```

---

## Self-Review (ran before handoff)

**1. Spec coverage:** Every spec section maps to a task — §4 (self-contained module, approach A) → Task 1; §5 (registration) → Task 7; §6.1 read_file → Task 2; §6.2 write_file → Task 3; §6.3 edit_file → Task 4; §6.4 list_files → Task 5; §6.5 glob → Task 6; §7 safety model (confinement + read-before-write + binary/size + to_thread) → baked into Tasks 1–6; §8 return/error shapes → all tool steps; §11 testing → all test steps. §10 docs sync → Task 8. No gaps.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step contains complete code; every run step has exact command + expected output.

**3. Type consistency:** `FileReadRegistry.mark_read(sid, path)` / `has_read(sid, path)` are sync throughout (Tasks 1, 2, 3, 4) — tools call them without `await` (matches the spec §7.2 sync-no-lock deviation). `_resolve_file_path`, `_is_binary`, `_registry` names match across all tasks. Tool names (`read_file`/`write_file`/`edit_file`/`list_files`/`glob`) match between definitions (Tasks 2–6), registration (Task 7), and doc sync (Task 8). `glob` shadows no module import (stdlib `glob` is not imported; `pathlib.Path.glob` is used via instance method).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-22-file-tools.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

(Note: the user deferred committing the spec — say whether implementation commits [each task's Step 5] should proceed as written, or be skipped/batched, when you pick an execution mode.)
