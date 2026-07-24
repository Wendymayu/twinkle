"""Central Python logging setup — routes process logs to ~/.twinkle/logs/.

Three files under LOG_DIR (= <WORKSPACE_DIR>/logs):
  gateway.log                    gateway process logs (daily-rotated)
  server.log                     agentserver process logs (daily-rotated)
  audit/permission_audit.jsonl   JSONL permission audit (no rotation,
                                 written directly by ToolPermissionLog)

Console (stderr) output is retained alongside the files (INFO+). gateway.log
and server.log rotate daily (midnight, backupCount=14). The audit file is
owned by ToolPermissionLog, NOT here — it stays decoupled so audit still
works even if setup_logging is never called (e.g. in tests).

Call setup_logging("gateway"|"agentserver") once from each process's __main__
before asyncio.run(). Reconfigurable: each call clears root handlers and
rebuilds (safe because observability only touches OTel, not Python logging).
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from twinkle.config import LOG_DIR

_LOG_FORMAT = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
_ROLE_FILE = {"gateway": "gateway.log", "agentserver": "server.log"}


def setup_logging(role: str) -> None:
    """Configure root logging: stderr console + one daily-rotated file per role.

    Idempotent in effect: clears existing root handlers, then re-adds. Safe to
    call once at startup or again (e.g. in tests).
    """
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_LOG_FORMAT)
    root.addHandler(console)

    filename = _ROLE_FILE.get(role)
    if filename:
        fh = TimedRotatingFileHandler(
            log_dir / filename, when="midnight", backupCount=14, encoding="utf-8"
        )
        fh.setFormatter(_LOG_FORMAT)
        root.addHandler(fh)
