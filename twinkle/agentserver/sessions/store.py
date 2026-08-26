"""磁盘持久化的短期 session memory。

``sessions_dir`` 下每个 session 的布局::

    <sessions_dir>/<session_id>/
        metadata.json   # {session_id, title, created_at, last_message_at, ...}
        history.json    # JSONL,每条追加的消息一个 record

两层:内存 cache(``dict[session_id -> list[OpenAI msg]]``)供
AgentLoop 热读,加磁盘 JSON 供跨重启持久化。
``get_messages`` 在 cache miss 时从 ``history.json`` 冷加载,使一个 ReAct
turn 能带着完整上文(system prompt、tool_calls、tool results)继续。

对齐 jiuwenclaw 的 ``session_metadata.py`` + ``session_history.py``(file-per-
session、JSONL history、首条用户消息自动生成 title),去掉 jiuwenclaw 的
async write-queue —— Twinkle 是单用户单进程,一把 ``asyncio.Lock``
串行化 metadata 的 read-modify-write 就够了。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("twinkle.agentserver.sessions.store")

_TITLE_MAX_LEN = 50
_OPENAI_FIELDS = ("role", "content", "tool_calls", "tool_call_id")


def _auto_title(content: str) -> str:
    title = (content or "").strip().replace("\n", " ")
    if len(title) > _TITLE_MAX_LEN:
        return title[:_TITLE_MAX_LEN] + "..."
    return title


class SessionStore:
    def __init__(self, sessions_dir: str | Path) -> None:
        self._root = Path(sessions_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    # --- 路径 ---

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _metadata_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "metadata.json"

    def _history_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "history.json"

    # --- session 生命周期 ---

    async def create_session(self, session_id: str, channel_id: str = "web") -> dict:
        """幂等地创建 session 目录 + metadata。已有 metadata
        原样保留(重建不会清空已有 session)。"""
        async with self._lock:
            return await self._create_session_locked(session_id, channel_id)

    async def _create_session_locked(
        self, session_id: str, channel_id: str = "web"
    ) -> dict:
        """假定 ``self._lock`` 已持有(re-entrant-safe 辅助函数,让
        ``append`` 能隐式创建而无需重新获取锁)。"""
        sdir = self._session_dir(session_id)
        sdir.mkdir(parents=True, exist_ok=True)
        mpath = self._metadata_path(session_id)
        if mpath.is_file():
            try:
                return json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                pass  # 损坏 —— 落到下面重写默认值
        now = time.time()
        meta = {
            "session_id": session_id,
            "title": "",
            "created_at": now,
            "last_message_at": now,
            "message_count": 0,
            "channel_id": channel_id,
        }
        mpath.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    async def delete_session(self, session_id: str) -> bool:
        """删除 session 目录 + 逐出 cache 项。不存在则返回 False。"""
        import shutil
        async with self._lock:
            sdir = self._session_dir(session_id)
            if not sdir.exists():
                self._cache.pop(session_id, None)
                return False
            shutil.rmtree(sdir, ignore_errors=True)
            self._cache.pop(session_id, None)
            return True

    def list_sessions(self, limit: int = 100, include_subagents: bool | None = None) -> list[dict]:
        """按 last_message_at 降序列出 session。损坏/缺失的
        metadata 回退到目录 mtime(对齐 jiuwenclaw legacy 回退)。

        默认隐藏 id 含 ``__sub_`` 的子 agent session
        (由 SubagentExecutor spawn)以保持浏览器 session 列表干净。
        传 ``include_subagents=True`` 可包含它们(executor 的
        测试/未来管理视图用);``include_subagents=False`` 显式隐藏。
        当 ``include_subagents is None``(默认)时,回退到
        ``SUBAGENT_LIST_SESSIONS_FILTER`` 配置项(默认 True = 隐藏)。
        config import 是惰性的(方法内,try/except ImportError),使
        store.py 不在模块加载时 import config(避免循环 import)。"""
        if include_subagents is None:
            try:
                from twinkle.config import SUBAGENT_LIST_SESSIONS_FILTER
                include_subagents = not SUBAGENT_LIST_SESSIONS_FILTER
            except ImportError:
                include_subagents = False
        out: list[dict] = []
        if not self._root.exists():
            return out
        for sdir in self._root.iterdir():
            if not sdir.is_dir():
                continue
            session_id = sdir.name
            if not include_subagents and "__sub_" in session_id:
                continue
            mpath = sdir / "metadata.json"
            try:
                meta = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                st = sdir.stat()
                meta = {
                    "session_id": session_id,
                    "title": "(无标题)",
                    "created_at": st.st_ctime,
                    "last_message_at": st.st_mtime,
                    "message_count": 0,
                    "channel_id": "web",
                }
            meta.setdefault("session_id", session_id)
            # 从 history.json 推导可见计数(非 system record),
            # 这样 stored count 被 system prompt 虚高的 legacy session
            # 也能正确显示;history 缺失/不可读时回退到 stored 值。见 issue #5。
            history = self.get_history(session_id)
            if history:
                meta["message_count"] = sum(
                    1 for r in history if r.get("role") != "system"
                )
            out.append(meta)
        out.sort(key=lambda m: m.get("last_message_at", 0), reverse=True)
        return out[:limit]

    def get_history(self, session_id: str) -> list[dict]:
        """返回原始 history record 供前端展示(最新在后)。
        坏 JSONL 行跳过,绝不抛异常。"""
        hpath = self._history_path(session_id)
        if not hpath.is_file():
            return []
        out: list[dict] = []
        for line in hpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping corrupt history line in %s", session_id)
        return out

    def list_files(self, session_id: str) -> list[dict]:
        """列出 session 目录下的顶层文件。扁平(不递归)—— Twinkle
        session 目录是扁平的。未知 session 返回 []。"""
        sdir = self._session_dir(session_id)
        if not sdir.is_dir():
            return []
        out: list[dict] = []
        for p in sdir.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "is_dir": p.is_dir(), "size": st.st_size})
        out.sort(key=lambda f: f["name"])
        return out

    def read_file(self, session_id: str, name: str) -> str:
        """从 session 目录读文件的文本内容。``name`` 必须是
        纯文件名 —— 路径穿越会被拒绝(name 来自浏览器,
        内容会回显到预览面板)。"""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"unsafe file name: {name!r}")
        base = self._session_dir(session_id).resolve()
        target = (base / name).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"path escapes session dir: {name!r}")
        p = self._session_dir(session_id) / name
        if not p.is_file():
            raise FileNotFoundError(f"no such file: {name}")
        return p.read_text(encoding="utf-8")

    # --- 消息存储(面向 AgentLoop) ---

    def get_messages(self, session_id: str) -> list[dict]:
        """返回 OpenAI 原生 messages 供 ReAct 循环用。Cache 命中
        立即返回;cache miss 从 history.json 冷加载。"""
        cached = self._cache.get(session_id)
        if cached is not None:
            return list(cached)
        msgs = [self._record_to_openai(r) for r in self.get_history(session_id)]
        self._cache[session_id] = msgs
        return list(msgs)

    async def append(
        self,
        session_id: str,
        message: dict,
        request_id: str | None = None,
        event_type: str | None = None,
    ) -> None:
        """追加一条 message:更新内存 cache,追加一条 history.json
        record,并更新 metadata(count、last_message_at、首条用户消息时自动 title)。"""
        async with self._lock:
            # 确保 session 在磁盘上存在(隐式创建)
            sdir = self._session_dir(session_id)
            if not sdir.is_dir():
                await self._create_session_locked(session_id)
            # 缓存 OpenAI 原生 message,使热的 ReAct turn 喂给
            # 模型的与冷重启重建的完全一致 —— reasoning 等
            # 非 OpenAI 字段在此丢弃(thinking 每轮重新生成,
            # 绝不回放进 prompt)。
            self._cache.setdefault(session_id, []).append(self._record_to_openai(message))
            # history record(保留完整 OpenAI 字段供冷重建)
            role = message.get("role")
            record = {
                "id": f"{request_id or 'none'}:{role}",
                "role": role,
                "request_id": request_id,
                "channel_id": "web",
                "timestamp": time.time(),
                "content": message.get("content"),
                "reasoning": message.get("reasoning"),
                "event_type": event_type,
                "session_id": session_id,
                "tool_calls": message.get("tool_calls"),
                "tool_call_id": message.get("tool_call_id"),
            }
            with self._history_path(session_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            # 更新 metadata
            self._update_metadata(session_id, role, message.get("content"))

    def _update_metadata(self, session_id: str, role: str | None, content: Any) -> None:
        mpath = self._metadata_path(session_id)
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            now = time.time()
            meta = {
                "session_id": session_id, "title": "",
                "created_at": now, "last_message_at": now,
                "message_count": 0, "channel_id": "web",
            }
        # system-role 消息是注入的 base prompt —— 不是
        # 用户可见的对话 turn —— 所以不计入
        # message_count(前端在 fromHistory 中过滤掉 `system`;
        # 计入会使每个 session 显示的计数虚高一,#5)。
        if role != "system":
            meta["message_count"] = int(meta.get("message_count", 0)) + 1
        meta["last_message_at"] = time.time()
        if not meta.get("title") and role == "user":
            meta["title"] = _auto_title(content if isinstance(content, str) else "")
        mpath.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _record_to_openai(record: dict) -> dict:
        """从 history record 重建 OpenAI 原生 message,丢弃
        值为 None 的可选字段。"""
        msg: dict[str, Any] = {}
        for k in _OPENAI_FIELDS:
            v = record.get(k)
            if v is not None:
                msg[k] = v
        return msg
