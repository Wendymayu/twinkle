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

from twinkle.agentserver.todo import get_plan_todo_session_id
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
    except (OSError, ValueError, NotImplementedError) as exc:
        return f"[ERROR]: glob failed: {exc}"
    return json.dumps({"pattern": pattern, "path": path, "matches": matches}, ensure_ascii=False)
