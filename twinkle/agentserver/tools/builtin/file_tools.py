"""file_tools —— 在 workspace 下读/写/编辑/列目录/glob 文件。

参考:openjiuwen SDK harness/tools/filesystem.py(2032 行,6 个 tool)。
Twinkle 保留 5 个(read_file/write_file/edit_file/list_files/glob);去掉
grep(用 command_exec rg/findstr)、delete/move(用 command_exec rm/mv)、
image/PDF/Notebook 读取、mtime/size stale-write 检查、.agent_history、
OS 沙箱、审批护栏。

安全(方案 b):workspace 路径限制(对照 command_exec 的
_resolve_workdir)+ 经 per-session FileReadRegistry 强制 read-before-write
(防盲写);无 stale 检查。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from twinkle.agentserver.todo import get_plan_todo_session_id
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.errors import ToolError
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
    """把 `file_path` 相对当前 workspace 解析;拒绝逃逸出 workspace 的路径。

    先检查 MEMBER_WORKSPACE ContextVar(运行 team member 时设置),
    回退到 WORKSPACE_DIR。相对路径拼接到 workspace 根下;绝对路径
    仅当解析后仍在 workspace 内时才接受。
    逃逸时抛 ValueError。
    """
    from twinkle.agentserver.team.context import MEMBER_WORKSPACE
    ws_override = MEMBER_WORKSPACE.get()
    root = (Path(ws_override) if ws_override else Path(WORKSPACE_DIR)).resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(root)  # raises ValueError if it escapes the workspace
    return candidate


def _is_binary(path: Path) -> bool:
    """启发式:已知二进制扩展名,或前 8 KiB 中出现 NUL 字节。"""
    if path.suffix.lower() in _BINARY_EXTS:
        return True
    try:
        with path.open("rb") as file_handle:
            chunk = file_handle.read(8192)
    except OSError:
        return False  # 让调用方统一暴露读错误
    return b"\x00" in chunk


class FileReadRegistry:
    """per-session 的已读路径集合:本 session 内 agent 读过的(已解析)路径。

    驱动 write_file/edit_file 的 read-before-write 守卫。同步方法:
    set.add / 成员判定在单个 event loop 上是原子的(内部无 await,
    无 TOCTOU),故无需 asyncio.Lock(一把长生命周期锁还会绑定到
    单个 event loop,跨 asyncio.run 测试 loop 会坏掉)。
    """

    def __init__(self) -> None:
        self._read: dict[str, set[str]] = {}

    def mark_read(self, session_id: str, path: str) -> None:
        self._read.setdefault(session_id, set()).add(path)

    def has_read(self, session_id: str, path: str) -> bool:
        return path in self._read.get(session_id, set())

    def clear(self, session_id: str) -> None:
        self._read.pop(session_id, None)


_registry = FileReadRegistry()  # 模块级单例;经 ContextVar 按 session 路由


@tool
async def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取 workspace 下的文本文件,带 offset/limit 分页。记录本次读取,以便 write_file/edit_file 强制 read-before-write。拒绝二进制文件。"""
    if not file_path:
        raise ToolError("file_path is required.", kind="validation")
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
        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")
    if not resolved.is_file():
        raise ToolError(f"file not found: {file_path}", kind="failed")
    if await asyncio.to_thread(_is_binary, resolved):
        raise ToolError(f"file is binary or unsupported: {file_path}", kind="failed")

    def _read() -> str:
        return resolved.read_text(encoding="utf-8", errors="replace")

    try:
        content = await asyncio.to_thread(_read)
    except OSError as exc:
        raise ToolError(f"failed to read file: {exc}", kind="failed")

    session_key = get_plan_todo_session_id()
    _registry.mark_read(session_key, str(resolved))
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
    """向 workspace 下的文件写入完整内容。覆盖已有文件要求本 session 先读过;新文件可直接创建。内容上限 5 MiB。"""
    if not file_path:
        raise ToolError("file_path is required.", kind="validation")
    content = content or ""
    data = content.encode("utf-8")
    if len(data) > _WRITE_MAX_BYTES:
        raise ToolError(f"content too large (>{_WRITE_MAX_BYTES} bytes).", kind="validation")

    try:
        resolved = _resolve_file_path(file_path)
    except ValueError:
        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")

    session_key = get_plan_todo_session_id()
    existed = resolved.is_file()
    if existed and not _registry.has_read(session_key, str(resolved)):
        raise ToolError(f"must read_file before overwriting existing file: {file_path}", kind="validation")

    def _write() -> str:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)
        return "update" if existed else "create"

    try:
        kind = await asyncio.to_thread(_write)
    except OSError as exc:
        raise ToolError(f"failed to write file: {exc}", kind="failed")

    _registry.mark_read(session_key, str(resolved))
    return json.dumps(
        {"file_path": file_path, "bytes_written": len(data), "type": kind},
        ensure_ascii=False,
    )


@tool
async def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """把文件中的 old_string 替换为 new_string(workspace 下)。要求本 session 先读过。old_string 须非空(新建文件用 write_file)。设 replace_all 替换多处出现。"""
    if not file_path:
        raise ToolError("file_path is required.", kind="validation")
    if not old_string:
        raise ToolError(f"old_string is empty; use write_file to create a new file: {file_path}", kind="validation")

    try:
        resolved = _resolve_file_path(file_path)
    except ValueError:
        raise ToolError(f"path is outside the project workspace: {file_path}", kind="validation")
    if not resolved.is_file():
        raise ToolError(f"file not found: {file_path}", kind="failed")
    if await asyncio.to_thread(_is_binary, resolved):
        raise ToolError(f"file is binary or unsupported: {file_path}", kind="failed")

    session_key = get_plan_todo_session_id()
    if not _registry.has_read(session_key, str(resolved)):
        raise ToolError(f"must read_file before editing: {file_path}", kind="validation")

    def _read() -> str:
        return resolved.read_text(encoding="utf-8", errors="replace")

    try:
        content = await asyncio.to_thread(_read)
    except OSError as exc:
        raise ToolError(f"failed to read file: {exc}", kind="failed")

    count = content.count(old_string)
    if count == 0:
        raise ToolError(f"old_string not found in {file_path}", kind="failed")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string matches {count} times; set replace_all=True or provide a more specific old_string.", kind="validation")
    # str.replace 默认替换全部;单次替换需显式传 count=1。
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    n = count if replace_all else 1

    def _write() -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(new_content.encode("utf-8"))

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        raise ToolError(f"failed to write file: {exc}", kind="failed")

    _registry.mark_read(session_key, str(resolved))
    return json.dumps({"file_path": file_path, "replacements": n}, ensure_ascii=False)


@tool
async def list_files(path: str = ".", show_hidden: bool = False) -> str:
    """列出 workspace 下某目录的条目。设 show_hidden 以包含 dotfile。"""
    if not path:
        path = "."
    try:
        resolved = _resolve_file_path(path)
    except ValueError:
        raise ToolError(f"path is outside the project workspace: {path}", kind="validation")
    if not resolved.exists():
        raise ToolError(f"path not found: {path}", kind="failed")
    if not resolved.is_dir():
        raise ToolError(f"not a directory: {path}", kind="failed")

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
        raise ToolError(f"failed to list directory: {exc}", kind="failed")
    return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)


@tool
async def glob(pattern: str, path: str = ".") -> str:
    """查找 workspace 下匹配 glob 模式的文件(标准库 pathlib,无 ripgrep)。path 是基目录;pattern 不得包含 '..'。"""
    if not pattern:
        raise ToolError("pattern is required.", kind="validation")
    if ".." in pattern:
        raise ToolError(f"pattern must not contain '..': {pattern}", kind="validation")
    if not path:
        path = "."
    try:
        resolved = _resolve_file_path(path)
    except ValueError:
        raise ToolError(f"path is outside the project workspace: {path}", kind="validation")
    if not resolved.is_dir():
        raise ToolError(f"path not found or not a directory: {path}", kind="failed")

    root = Path(WORKSPACE_DIR).resolve()

    def _glob() -> list[str]:
        matches = []
        for p in resolved.glob(pattern):
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                continue  # 纵深防御:丢弃任何逃逸出 workspace 的结果
            matches.append(str(rel))
        return sorted(matches)

    try:
        matches = await asyncio.to_thread(_glob)
    except (OSError, ValueError, NotImplementedError) as exc:
        raise ToolError(f"glob failed: {exc}", kind="failed")
    return json.dumps({"pattern": pattern, "path": path, "matches": matches}, ensure_ascii=False)
