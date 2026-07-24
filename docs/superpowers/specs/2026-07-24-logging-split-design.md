# 系统日志三分离设计 — gateway.log / server.log / audit JSONL

- 日期: 2026-07-24
- 状态: 设计已确认,待写实现计划
- 范围: 仅日志输出层,不涉及业务逻辑

## 目标

把 Twinkle 两个进程的日志拆成 `~/.twinkle/logs/` 下的三个文件:

| 文件 | 内容 | 轮转 | 来源进程 |
|---|---|---|---|
| `logs/gateway.log` | gateway 进程全部日志 | 按天(`TimedRotatingFileHandler`, `backupCount=14`) | gateway |
| `logs/server.log` | agentserver 进程全部日志 | 按天(同上) | agentserver |
| `logs/audit/permission_audit.jsonl` | 权限审计 JSONL | **不轮转**,raw 追加 | agentserver(权限引擎) |

控制台(stderr)输出保留(文件 + 控制台并存)。审计文件不轮转(低频结构化数据,沿用现状),且**只进文件、不上控制台**(与现状一致)。

## 现状(已核实)

- 日志配置仅有两处近重复的 `logging.basicConfig(level=INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")`:[agentserver/__main__.py:9-12](twinkle/agentserver/__main__.py) 与 [gateway/__main__.py:20-23](twinkle/gateway/__main__.py)(后者在模块顶层、import 时即执行)。均只输出到 stderr。15 个模块用 `logging.getLogger("twinkle.*")` 取子 logger 并向 root 传播,无任何 FileHandler。
- 无中央 logging 模块;[twinkle/observability/](twinkle/observability/) 只做 OTel,不碰 Python logging(`setup()` 仅用自身 logger 输出状态/警告)。
- 权限审计**不是** Python logging:[audit.py](twinkle/agentserver/permissions/audit.py) 的 `ToolPermissionLog` 直接 `open("a")` 追加 JSONL,fail-soft,每次写都 `self._path.parent.mkdir(parents=True, exist_ok=True)`。路径来自 [config.py](twinkle/config.py) 的 `PERMISSION_AUDIT_FILE`,默认 `<WORKSPACE>/.twinkle_data/permission_audit.jsonl`。
- `WORKSPACE_DIR` 默认 `~/.twinkle`(`config.py:51-53`);`.twinkle_data/` 是惯用数据子目录。`*.log` 已被 [.gitignore](.gitignore) 第 51 行覆盖,无需改 gitignore。
- 无测试 import `twinkle.gateway.__main__`(已 grep 确认),故把 gateway 的 `basicConfig` 从模块顶层移入 `__main__` guard 安全。
- 无任何测试调用 `logging.basicConfig` / `setup_logging` / 读 `LOG_DIR`(已 grep 确认),日志层改动不影响现有测试。

## 设计

### 1. 路径常量(config.py)

- 新增 `LOG_DIR = os.getenv("TWINKLE_LOG_DIR") or str(Path(WORKSPACE_DIR) / "logs")` → 默认 `~/.twinkle/logs`(即"用户目录/.twinkle/logs")。
- `PERMISSION_AUDIT_FILE` 默认值由 `str(Path(WORKSPACE_DIR) / ".twinkle_data" / "permission_audit.jsonl")` 改为 `str(Path(LOG_DIR) / "audit" / "permission_audit.jsonl")`。`TWINKLE_PERMISSION_AUDIT_FILE` 环境变量仍可覆盖。

### 2. 新增中央模块 `twinkle/logging_config.py`

`setup_logging(role: "gateway" | "agentserver") -> None`:

1. `Path(LOG_DIR).mkdir(parents=True, exist_ok=True)`。
2. `root = logging.getLogger()`;清空 `root.handlers`(可重入,便于测试;安全——observability 只做 OTel、不加 Python logging handler)。`root.setLevel(logging.INFO)`。
3. 加 `logging.StreamHandler(sys.stderr)`,格式 `%(asctime)s %(name)s %(levelname)s %(message)s`(沿用现状格式)——控制台输出。
4. 按 role 加一个 `logging.handlers.TimedRotatingFileHandler`:
   - `gateway` → `Path(LOG_DIR) / "gateway.log"`
   - `agentserver` → `Path(LOG_DIR) / "server.log"`
   - 参数:`when="midnight"`, `backupCount=14`, `encoding="utf-8"`,同上 formatter。
5. **audit.log 不由本模块管理**——`ToolPermissionLog` 直接写(解耦:即使没调 `setup_logging`,审计仍能落盘)。

模块顶部 `LOG_DIR` 从 `twinkle.config` import,不重复定义。

### 3. 两个 `__main__.py` 改动

- [agentserver/__main__.py](twinkle/agentserver/__main__.py):`logging.basicConfig(...)` → `setup_logging("agentserver")`。启动顺序:setup_logging → `observability.setup()` → `ensure_workspace_dir()` → `asyncio.run(main())`。observability 不碰 Python logging,无冲突。
- [gateway/__main__.py](twinkle/gateway/__main__.py):删掉模块顶层的 `logging.basicConfig(...)`(本就是 import 时执行的坏味道);在 `__main__` guard 内、`asyncio.run(main())` 前调 `setup_logging("gateway")`。

### 4. audit.py — 不改动

`ToolPermissionLog` 保持原样:raw JSONL 追加、fail-soft、`self._path.parent.mkdir(parents=True, exist_ok=True)`。因默认 path 变为 `<LOG_DIR>/audit/permission_audit.jsonl`,首次写入时 `mkdir(parents=True)` 会自动建出 `~/.twinkle/logs/audit/` 子目录。**不轮转**(用户决策:低频结构化数据)。3 个现有 audit 测试零改动。

### 5. 最终行为

- gateway 进程:`~/.twinkle/logs/gateway.log`(按天轮转)+ stderr。
- agentserver 进程:`~/.twinkle/logs/server.log`(按天轮转)+ stderr;审计记录 → `~/.twinkle/logs/audit/permission_audit.jsonl`(追加,不轮转,只进文件)。
- 每个文件只被一个进程写,无跨进程写冲突。audit logger 走 `twinkle.permissions.audit`(仅用于"审计写失败"的 warning,向 root 传播→server.log+stderr),审计记录本身不经过 Python logging。

### 6. 文档

- [CLAUDE.md](CLAUDE.md) 配置表:`TWINKLE_PERMISSION_AUDIT_FILE` 默认值改为 `<WORKSPACE>/logs/audit/permission_audit.jsonl`;新增 `TWINKLE_LOG_DIR` 行(默认 `<WORKSPACE>/logs`)。
- `.gitignore` 已覆盖 `*.log`,不改。

## 测试(TDD)

- 新增 `tests/test_logging_config.py`(monkeypatch `logging_config.LOG_DIR` 到 tmp_path):
  - `setup_logging("gateway")` 后断言 `gateway.log` 被建、写一行能落到文件;`"agentserver"` 同理落 `server.log`。
  - 断言 root 上存在 `StreamHandler`(stderr)(控制台保留)。
  - 断言重复调用 `setup_logging` 不堆叠 handler(可重入)。
  - 断言未触发的角色文件不被建(如 gateway 模式下不建 `server.log`)。
- 现有 `tests/test_permissions_audit.py` 不动,跑通即证明 audit 契约未破。
- 全量 `python -m pytest tests/ -v`。

## 非目标 / 不做

- 不改 INFO 以上的日志级别(沿用默认)。
- 不给 audit 加轮转。
- 不把 gateway/server 改成结构化/JSON 日志(保持人类可读文本格式)。
- 不动 observability(OTel)。
- 不动 sessions / `.twinkle_data/` 下其它文件。
