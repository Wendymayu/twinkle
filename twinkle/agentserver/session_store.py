"""In-memory short-term session memory.

Stores OpenAI-native messages per session_id. Phase 1 is single-user,
in-memory; the interface allows swapping in SQLite later without
touching callers (Phase 3 will add truncate/compress here).
"""
from __future__ import annotations


class SessionStore:
    def __init__(self) -> None:
        self._data: dict[str, list[dict]] = {}

    def get_messages(self, session_id: str) -> list[dict]:
        return list(self._data.get(session_id, []))

    def append(self, session_id: str, message: dict) -> None:
        self._data.setdefault(session_id, []).append(message)
