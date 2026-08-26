"""集中式 Python logging setup —— 把进程日志路由到 ~/.twinkle/logs/。

LOG_DIR（= <WORKSPACE_DIR>/logs）下三个文件：
  gateway.log                    gateway 进程日志（按天轮转）
  server.log                     agentserver 进程日志（按天轮转）
  audit/permission_audit.jsonl   JSONL permission audit（不轮转，
                                 由 ToolPermissionLog 直接写）

Console（stderr）输出与文件并存（INFO+）。gateway.log 与 server.log 按天
轮转（midnight，backupCount=14）。audit 文件归 ToolPermissionLog 所有，
不在此处——保持解耦，使即便 setup_logging 从未被调用（如测试中）audit
仍工作。

在每个进程的 __main__ 中、asyncio.run() 之前调用一次
setup_logging("gateway"|"agentserver")。可重配：每次调用清空 root handler
并重建（安全，因 observability 只动 OTel，不动 Python logging）。
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
    """配置 root logging：stderr console + 每个 role 一个按天轮转的文件。

    效果幂等：清空已有 root handler 再重加。启动时调用一次或重复调用
    （如测试中）均安全。
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
