# 系统日志三分离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把两个进程的日志拆成 `~/.twinkle/logs/` 下的 `gateway.log`(按天轮转)、`server.log`(按天轮转)、`audit/permission_audit.jsonl`(不轮转),并保留 stderr 控制台输出。

**Architecture:** 新增中央模块 `twinkle/logging_config.py` 的 `setup_logging(role)`:在 root logger 上挂 `StreamHandler(stderr)` + 一个按角色的 `TimedRotatingFileHandler`(midnight/backupCount=14)。两个 `__main__.py` 改调它。权限审计 `audit.py` 不动,只把 `config.py` 里默认路径从 `.twinkle_data/permission_audit.jsonl` 改到 `logs/audit/permission_audit.jsonl`(首次写入时 `audit.py` 的 `mkdir(parents=True)` 自动建子目录)。日志层与 observability(OTel)解耦。

**Tech Stack:** Python stdlib `logging` + `logging.handlers.TimedRotatingFileHandler`;pytest(`asyncio.run`,无 pytest-asyncio);项目 venv。

参考 spec:[docs/superpowers/specs/2026-07-24-logging-split-design.md](../specs/2026-07-24-logging-split-design.md)

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `twinkle/logging_config.py` | 新增 | `setup_logging(role)`:stderr + 按角色按天轮转文件 |
| `twinkle/config.py` | 改 | 加 `LOG_DIR`;`PERMISSION_AUDIT_FILE` 默认指向 `logs/audit/permission_audit.jsonl` |
| `twinkle/agentserver/__main__.py` | 改 | `basicConfig` → `setup_logging("agentserver")` |
| `twinkle/gateway/__main__.py` | 改 | 删模块顶层 `basicConfig`,`__main__` guard 内调 `setup_logging("gateway")` |
| `twinkle/agentserver/permissions/audit.py` | 不动 | 保持 raw JSONL 追加 |
| `tests/test_logging_config.py` | 新增 | `setup_logging` 的 TDD 测试 |
| `tests/test_permissions_config.py` | 改 | 更新 audit 默认路径断言 |
| `CLAUDE.md` | 改 | 配置表加 `TWINKLE_LOG_DIR`、改 audit 行 |

---

## Task 1: config.py — 加 `LOG_DIR` 并改 audit 默认路径(TDD)

**Files:**
- Modify: `twinkle/config.py`(WORKSPACE_DIR 块后加 `LOG_DIR`;改 `PERMISSION_AUDIT_FILE` 默认)
- Test: `tests/test_permissions_config.py:41-43`

- [ ] **Step 1: 先改测试,让它断言新路径(此时应 FAIL)**

编辑 `tests/test_permissions_config.py`,把 audit 后缀断言从 `.twinkle_data/permission_audit.jsonl` 改成 `logs/audit/permission_audit.jsonl`。

`old_string`(第 41-43 行):
```python
    assert cfg.PERMISSION_AUDIT_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_audit.jsonl"
    )
```
`new_string`:
```python
    assert cfg.PERMISSION_AUDIT_FILE.replace("\\", "/").endswith(
        "logs/audit/permission_audit.jsonl"
    )
```
注意:同函数内 `PERMISSION_OVERRIDES_FILE` 的断言(第 38-40 行)保持不变(overrides 仍在 `.twinkle_data/`)。

- [ ] **Step 2: 跑测试确认 FAIL**

Run: `python -m pytest tests/test_permissions_config.py::test_override_paths_under_workspace -v`
Expected: FAIL,断言 `endswith("logs/audit/permission_audit.jsonl")` 不成立(当前仍 ends `.twinkle_data/permission_audit.jsonl`)。

- [ ] **Step 3: 改 config.py — 加 LOG_DIR**

在 `twinkle/config.py` 的 `WORKSPACE_DIR` 块之后、`# --- Sessions persistence` 注释之前插入 `LOG_DIR`。

`old_string`:
```python
WORKSPACE_DIR = os.path.expanduser(
    os.getenv("TWINKLE_WORKSPACE_DIR") or str(Path.home() / ".twinkle")
)

# --- Sessions persistence (disk-backed session store) ---
```
`new_string`:
```python
WORKSPACE_DIR = os.path.expanduser(
    os.getenv("TWINKLE_WORKSPACE_DIR") or str(Path.home() / ".twinkle")
)

# --- Process logs (gateway.log / server.log / audit JSONL) ---
# Defaults to <WORKSPACE_DIR>/logs (so ~/.twinkle/logs). gateway.log and
# server.log are daily-rotated by twinkle.logging_config.setup_logging; the
# JSONL permission audit lives under logs/audit/ (no rotation, written
# directly by ToolPermissionLog). Override the whole dir with TWINKLE_LOG_DIR.
LOG_DIR = os.getenv("TWINKLE_LOG_DIR") or str(Path(WORKSPACE_DIR) / "logs")

# --- Sessions persistence (disk-backed session store) ---
```

- [ ] **Step 4: 改 config.py — PERMISSION_AUDIT_FILE 指向 LOG_DIR**

`old_string`(约第 160-162 行):
```python
PERMISSION_AUDIT_FILE = os.getenv("TWINKLE_PERMISSION_AUDIT_FILE") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "permission_audit.jsonl"
)
```
`new_string`:
```python
PERMISSION_AUDIT_FILE = os.getenv("TWINKLE_PERMISSION_AUDIT_FILE") or str(
    Path(LOG_DIR) / "audit" / "permission_audit.jsonl"
)
```

- [ ] **Step 5: 跑测试确认 PASS**

Run: `python -m pytest tests/test_permissions_config.py -v`
Expected: PASS(全部用例,含 `test_override_paths_under_workspace`)。

- [ ] **Step 6: 提交**

```bash
git add twinkle/config.py tests/test_permissions_config.py
git commit -m "config: add LOG_DIR, move default audit path to logs/audit/"
```

---

## Task 2: logging_config.py — 中央日志模块(TDD)

**Files:**
- Create: `twinkle/logging_config.py`
- Test: `tests/test_logging_config.py`

- [ ] **Step 1: 写失败测试 `tests/test_logging_config.py`**

```python
import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from twinkle import logging_config


@pytest.fixture
def restore_root_logging():
    """快照 root logger 状态;测试后还原并关闭新增 handler。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        if h not in saved_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setattr(logging_config, "LOG_DIR", str(d))
    return d


def test_gateway_log_file_created_and_written(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("gateway")
    logging.getLogger("twinkle.gateway.test").info("hello-gw")
    for h in logging.getLogger().handlers:
        h.flush()
    gw = tmp_log_dir / "gateway.log"
    assert gw.is_file()
    assert "hello-gw" in gw.read_text(encoding="utf-8")


def test_server_log_file_created_and_written(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("agentserver")
    logging.getLogger("twinkle.agentserver.test").info("hello-srv")
    for h in logging.getLogger().handlers:
        h.flush()
    srv = tmp_log_dir / "server.log"
    assert srv.is_file()
    assert "hello-srv" in srv.read_text(encoding="utf-8")


def test_console_stderr_handler_retained(tmp_log_dir, restore_root_logging):
    # TimedRotatingFileHandler 是 StreamHandler 子类,要排除 FileHandler 子类
    logging_config.setup_logging("agentserver")
    root = logging.getLogger()
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


def test_setup_logging_idempotent_no_duplicate_file_handlers(
    tmp_log_dir, restore_root_logging
):
    logging_config.setup_logging("agentserver")
    logging_config.setup_logging("agentserver")
    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1


def test_gateway_role_does_not_create_server_log(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("gateway")
    assert not (tmp_log_dir / "server.log").exists()
    assert (tmp_log_dir / "gateway.log").is_file()
```

- [ ] **Step 2: 跑测试确认 FAIL**

Run: `python -m pytest tests/test_logging_config.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'twinkle.logging_config'`(或 import error)。

- [ ] **Step 3: 实现 `twinkle/logging_config.py`**

```python
"""Central Python logging setup — routes process logs to ~/.twinkle/logs/.

Three files under LOG_DIR (= <WORKSPACE_DIR>/logs):
  gateway.log                    gateway process logs (daily-rotated)
  server.log                     agentserver process logs (daily-rotated)
  audit/permission_audit.jsonl  JSONL permission audit (no rotation,
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
```

- [ ] **Step 4: 跑测试确认 PASS**

Run: `python -m pytest tests/test_logging_config.py -v`
Expected: PASS(5 个用例全过)。

- [ ] **Step 5: 提交**

```bash
git add twinkle/logging_config.py tests/test_logging_config.py
git commit -m "logging: add central setup_logging with daily-rotated per-role files"
```

---

## Task 3: agentserver/__main__.py — 改用 setup_logging

**Files:**
- Modify: `twinkle/agentserver/__main__.py`

- [ ] **Step 1: 改 `__main__.py`**

整文件替换为:

```python
"""Entry point: `python -m twinkle.agentserver`."""
import asyncio

from twinkle.agentserver.server import main
from twinkle.config import ensure_workspace_dir
from twinkle.logging_config import setup_logging

if __name__ == "__main__":
    setup_logging("agentserver")
    import twinkle.observability
    twinkle.observability.setup()
    ensure_workspace_dir()
    asyncio.run(main())
```

变化:删 `import logging` 与 `logging.basicConfig(...)`;改为 `setup_logging("agentserver")`。顺序保持 setup_logging → observability.setup() → ensure_workspace_dir → run(observability 只动 OTel,不碰 Python logging,无冲突)。

- [ ] **Step 2: 验证 import 无副作用**

Run: `python -c "import twinkle.agentserver.__main__"`
Expected: 无输出、无异常(import 时 `if __name__ == "__main__"` 块不执行,故不会启动服务、不会改全局 logging)。

- [ ] **Step 3: 跑相关测试确认未破**

Run: `python -m pytest tests/test_permissions_audit.py tests/test_logging_config.py -v`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add twinkle/agentserver/__main__.py
git commit -m "agentserver: wire setup_logging(\"agentserver\") at startup"
```

---

## Task 4: gateway/__main__.py — 删模块顶层 basicConfig,改用 setup_logging

**Files:**
- Modify: `twinkle/gateway/__main__.py`

- [ ] **Step 1: 改 `__main__.py`**

把模块顶层的 `import logging` + `logging.basicConfig(...)` 删掉,改成在 `__main__` guard 内调 `setup_logging("gateway")`。具体两处编辑:

编辑 a — 删 `import logging`,加 setup_logging import。
`old_string`:
```python
import asyncio
import logging

from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, GATEWAY_HOST, GATEWAY_PORT
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
```
`new_string`:
```python
import asyncio

from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, GATEWAY_HOST, GATEWAY_PORT
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel
from twinkle.logging_config import setup_logging
```

编辑 b — 在 `__main__` guard 内加 setup_logging 调用。
`old_string`:
```python
if __name__ == "__main__":
    asyncio.run(main())
```
`new_string`:
```python
if __name__ == "__main__":
    setup_logging("gateway")
    asyncio.run(main())
```

- [ ] **Step 2: 验证 import 无副作用(关键回归:此前顶层 basicConfig 会在 import 时执行)**

Run: `python -c "import twinkle.gateway.__main__"`
Expected: 无输出、无异常。验证:import 后 root logger 不应被挂上 StreamHandler(basicConfig 已不在顶层)。可加断言式检查:
Run: `python -c "import logging, twinkle.gateway.__main__ as m; assert not logging.getLogger().handlers, logging.getLogger().handlers; print('ok')"`
Expected: 输出 `ok`。

- [ ] **Step 3: 跑全量测试确认未破**

Run: `python -m pytest tests/ -v`
Expected: PASS(全部已过用例仍过)。

- [ ] **Step 4: 提交**

```bash
git add twinkle/gateway/__main__.py
git commit -m "gateway: move logging setup into __main__ guard via setup_logging"
```

---

## Task 5: CLAUDE.md — 更新配置表

**Files:**
- Modify: `CLAUDE.md`(配置表)

- [ ] **Step 1: 加 TWINKLE_LOG_DIR 行**

`old_string`:
```
| `TWINKLE_WORKSPACE_DIR` | `~/.twinkle` | Sandbox root for `command_exec`/`file_tools` — agent file ops confined under this. Defaults to the user home so generated files don't pollute the repo; override to point elsewhere |
```
`new_string`:
```
| `TWINKLE_WORKSPACE_DIR` | `~/.twinkle` | Sandbox root for `command_exec`/`file_tools` — agent file ops confined under this. Defaults to the user home so generated files don't pollute the repo; override to point elsewhere |
| `TWINKLE_LOG_DIR` | `<WORKSPACE>/logs` | Process log dir (gateway.log / server.log daily-rotated; audit under `logs/audit/`) |
```

- [ ] **Step 2: 改 audit 行路径**

`old_string`:
```
| `TWINKLE_PERMISSION_AUDIT_FILE` | `<WORKSPACE>/.twinkle_data/permission_audit.jsonl` | ToolPermissionLog JSONL |
```
`new_string`:
```
| `TWINKLE_PERMISSION_AUDIT_FILE` | `<WORKSPACE>/logs/audit/permission_audit.jsonl` | ToolPermissionLog JSONL (no rotation) |
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: add TWINKLE_LOG_DIR, update audit default path in config table"
```

---

## Task 6: 全量验证 + 可选手动冒烟

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: PASS(含新增 `test_logging_config.py` 与改过的 `test_permissions_config.py`,audit 三测试不变仍过)。

- [ ] **Step 2(可选):手动冒烟 — 确认 server.log 真有内容**

Run(后台起 agentserver,2 秒后读文件再杀进程):
```bash
python -m twinkle.agentserver &
sleep 2
cat ~/.twinkle/logs/server.log   # 应包含 "AgentServer listening on 127.0.0.1:18000"
```
Expected: `~/.twinkle/logs/server.log` 存在且含 listening 行;`~/.twinkle/logs/` 下无 `audit/`(本冒烟未触发权限审计,属正常)。随后终止后台进程。

注:完整 agent 行为需 `TWINKLE_LLM_API_KEY`,但 logging setup + ws 监听发生在 LLM 调用之前,故冒烟不需 API key 即可验证日志落盘。

- [ ] **Step 3: 若 Step 1/2 全绿,本计划完成**

无新增提交(纯验证步骤)。

---

## 验收对照 spec

- 三文件 `gateway.log`/`server.log`/`audit/permission_audit.jsonl` 落 `~/.twinkle/logs/` —— Task 1(config 路径)+ Task 2(文件 handler)+ Task 3/4(接线)。
- 控制台 stderr 保留 —— Task 2 `StreamHandler(sys.stderr)`。
- gateway/server 按天轮转 —— Task 2 `TimedRotatingFileHandler(when="midnight", backupCount=14)`。
- audit 不轮转、raw JSONL、fail-soft、自动建父目录 —— Task 1 改默认路径;audit.py 不动(契约由现有 3 测试守护)。
- 两个 `__main__.py` 调 `setup_logging` —— Task 3/4。
- CLAUDE.md 配置表更新 —— Task 5。
