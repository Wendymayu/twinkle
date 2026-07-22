"""Disk-backed short-term session memory.

Per-session layout under ``sessions_dir``::

    <sessions_dir>/<session_id>/
        metadata.json   # {session_id, title, created_at, last_message_at, ...}
        history.json    # JSONL, one record per appended message

Two layers: an in-memory cache (``dict[sid -> list[OpenAI msg]]``) for the
AgentLoop's hot reads, plus on-disk JSON for persistence across restarts.
``get_messages`` cold-hydrates from ``history.json`` on a cache miss so a ReAct
turn can resume with full prior context (system prompt, tool_calls, tool results).

Mirrors jiuwenclaw's ``session_metadata.py`` + ``session_history.py`` (file-per-
session, JSONL history, auto-title from first user message), minus jiuwenclaw's
async write-queue — Twinkle is single-user single-process, so a single
``asyncio.Lock`` serializing metadata read-modify-write is enough.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("twinkle.agentserver.session_store")

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

    # --- paths ---

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _metadata_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "metadata.json"

    def _history_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "history.json"

    # --- session lifecycle ---

    async def create_session(self, session_id: str, channel_id: str = "web") -> dict:
        """Idempotently create a session dir + metadata. Existing metadata is
        left untouched (a re-create never wipes a populated session)."""
        async with self._lock:
            return await self._create_session_locked(session_id, channel_id)

    async def _create_session_locked(
        self, session_id: str, channel_id: str = "web"
    ) -> dict:
        """Assumes ``self._lock`` is already held (re-entrant-safe helper so
        ``append`` can implicitly create without re-acquiring the lock)."""
        sdir = self._session_dir(session_id)
        sdir.mkdir(parents=True, exist_ok=True)
        mpath = self._metadata_path(session_id)
        if mpath.is_file():
            try:
                return json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                pass  # corrupt — fall through and rewrite defaults
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
        """Remove a session dir + evict the cache entry. Returns False if absent."""
        import shutil
        async with self._lock:
            sdir = self._session_dir(session_id)
            if not sdir.exists():
                self._cache.pop(session_id, None)
                return False
            shutil.rmtree(sdir, ignore_errors=True)
            self._cache.pop(session_id, None)
            return True

    def list_sessions(self, limit: int = 100) -> list[dict]:
        """List sessions sorted by last_message_at desc. Corrupt/missing
        metadata falls back to dir mtime (mirrors jiuwenclaw legacy fallback)."""
        out: list[dict] = []
        if not self._root.exists():
            return out
        for sdir in self._root.iterdir():
            if not sdir.is_dir():
                continue
            mpath = sdir / "metadata.json"
            try:
                meta = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                st = sdir.stat()
                meta = {
                    "session_id": sdir.name,
                    "title": "(无标题)",
                    "created_at": st.st_ctime,
                    "last_message_at": st.st_mtime,
                    "message_count": 0,
                    "channel_id": "web",
                }
            meta.setdefault("session_id", sdir.name)
            out.append(meta)
        out.sort(key=lambda m: m.get("last_message_at", 0), reverse=True)
        return out[:limit]

    def get_history(self, session_id: str) -> list[dict]:
        """Return raw history records for frontend display (newest last).
        Bad JSONL lines are skipped, never raised."""
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

    # --- message store (AgentLoop-facing) ---

    def get_messages(self, session_id: str) -> list[dict]:
        """Return OpenAI-native messages for the ReAct loop. Cache hit returns
        immediately; cache miss cold-hydrates from history.json."""
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
        """Append a message: update the in-memory cache, append a history.json
        record, and update metadata (count, last_message_at, auto-title on the
        first user message)."""
        async with self._lock:
            # ensure the session exists on disk (implicit create)
            sdir = self._session_dir(session_id)
            if not sdir.is_dir():
                await self._create_session_locked(session_id)
            # cache
            self._cache.setdefault(session_id, []).append(dict(message))
            # history record (preserve full OpenAI fields for cold reconstruction)
            role = message.get("role")
            record = {
                "id": f"{request_id or 'none'}:{role}",
                "role": role,
                "request_id": request_id,
                "channel_id": "web",
                "timestamp": time.time(),
                "content": message.get("content"),
                "event_type": event_type,
                "session_id": session_id,
                "tool_calls": message.get("tool_calls"),
                "tool_call_id": message.get("tool_call_id"),
            }
            with self._history_path(session_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            # metadata update
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
        meta["message_count"] = int(meta.get("message_count", 0)) + 1
        meta["last_message_at"] = time.time()
        if not meta.get("title") and role == "user":
            meta["title"] = _auto_title(content if isinstance(content, str) else "")
        mpath.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _record_to_openai(record: dict) -> dict:
        """Reconstruct an OpenAI-native message from a history record, dropping
        None-valued optional fields."""
        msg: dict[str, Any] = {}
        for k in _OPENAI_FIELDS:
            v = record.get(k)
            if v is not None:
                msg[k] = v
        return msg
