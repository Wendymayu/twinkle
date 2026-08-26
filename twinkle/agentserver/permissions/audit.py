"""审计写入 — 结构化 JSONL(对齐 jiuwenswarm ToolPermissionLog,非 DB)。

两条独立审计流共用 _append_jsonl:
  - ToolPermissionLog:权限决策审计(每次 check 写一行;ASK 流 2 行)。
  - 工具执行审计(AuditHook):每次工具调用的 name/args/result/outcome。

fail-soft:写失败不抛(审计不应阻断主流程)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from twinkle.agentserver.permissions.models import ToolPermissionLogEntry

log = logging.getLogger("twinkle.audit")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """追加写一行 JSON;失败只告警不抛(fail-soft)。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("audit write failed: %s", exc)


class ToolPermissionLog:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def log(self, entry: ToolPermissionLogEntry) -> None:
        _append_jsonl(self._path, entry.to_dict())
