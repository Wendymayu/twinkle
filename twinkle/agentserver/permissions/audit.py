"""ToolPermissionLog — 结构化 JSONL 审计(对齐 jiuwenswarm ToolPermissionLog,非 DB)。

每次 check 写一行;ASK 流产生 2 行(check 时 user_decision=None,响应后 user_decision=决策)。
fail-soft:写失败不抛(审计不应阻断主流程)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from twinkle.agentserver.permissions.models import ToolPermissionLogEntry

log = logging.getLogger("twinkle.permissions.audit")


class ToolPermissionLog:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def log(self, entry: ToolPermissionLogEntry) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("audit write failed: %s", exc)
