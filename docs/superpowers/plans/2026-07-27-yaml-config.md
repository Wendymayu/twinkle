# Twinkle YAML Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `twinkle/config.py`'s env-var + `.env` loader with a YAML config file + `${ENV:-default}` interpolation + pydantic validation, mirroring `jiuwenswarm/resources/config.yaml`, while keeping the `from twinkle.config import X` constant API unchanged.

**Architecture:** New `twinkle/resources/config.yaml` (committed, `${ENV:-default}` for secrets/deploy vars; literals for tunables) → `config_loader.py` reads + resolves env + `yaml.safe_load` → `config_schema.py` pydantic models with `Literal` 取值域 + derived paths → `config.py` exports the same flat constants consumers already import. `ensure_workspace_dir`/`_seed_example_skills` move to a new `workspace.py`. Permissions deny patterns stay in `builtin_rules.py`; observability stays on its own `OTEL_*` env.

**Tech Stack:** Python ≥3.11, PyYAML (new dep), pydantic ≥2.11 (existing), pytest (existing, no pytest-asyncio — `asyncio.run()` + `free_port`).

**Spec:** `docs/superpowers/specs/2026-07-27-yaml-config-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `twinkle/resources/config.yaml` | Default config: sections + comments + 取值域 + `${ENV:-default}` | Create |
| `twinkle/resources/__init__.py` | Make `twinkle.resources` a package so the YAML ships in builds | Create (empty) |
| `twinkle/config_schema.py` | Pydantic models: `Literal` 取值域, defaults, derived-path validator | Create |
| `twinkle/config_loader.py` | `_load_env_file` + `_resolve_env_vars` + `load_config(path=None)` | Create |
| `twinkle/config.py` | `settings = load_config()` → export flat constants (same names) | Rewrite |
| `twinkle/workspace.py` | `ensure_workspace_dir`/`_seed_example_skills` (moved from config) | Create |
| `twinkle/agentserver/__main__.py`, `twinkle/agentserver/server.py:146` | Import `ensure_workspace_dir` from `twinkle.workspace` | Modify |
| `pyproject.toml` | Add `pyyaml`, package-data for `twinkle.resources` | Modify |
| `.env.example` | Slim to secrets + surviving env vars | Modify |
| `tests/test_config_schema.py` | Schema validation + derived paths | Create |
| `tests/test_config_loader.py` | `${ENV:-default}` resolution + `load_config` | Create |
| `tests/test_config_constants.py` | `config.py` exports correct constants + env-driven paths | Create |
| `tests/test_permissions_config.py` | Rewrite for YAML (drop `TWINKLE_PERMISSIONS` env tests) | Rewrite |
| `tests/test_permissions_e2e.py:28-32` | Enable permissions via constant monkeypatch, not env | Modify |
| `docs/architecture.md` §9.2, `CLAUDE.md` config table | Sync env-var doc | Modify |

---

## Task 1: Add PyYAML dependency + package the YAML resource

**Files:**
- Modify: `pyproject.toml`
- Create: `twinkle/resources/__init__.py`

- [ ] **Step 1: Add pyyaml + package-data to pyproject.toml**

Edit `pyproject.toml` — add `pyyaml>=6` to `dependencies` (after `httpx>=0.27`), and add a `[tool.setuptools.package-data]` table after the `[tool.setuptools.packages.find]` block:

```toml
[project]
name = "twinkle"
version = "0.0.1"
description = "Twinkle — personal AI assistant (learning reimplementation of jiuwenclaw core)"
requires-python = ">=3.11"
dependencies = [
    "websockets>=14",
    "pydantic>=2.11",
    "openai>=1.50",
    "httpx>=0.27",
    "pyyaml>=6",
]

[project.optional-dependencies]
dev = ["pytest>=8"]
obs = [
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
]

[tool.setuptools.packages.find]
include = ["twinkle*"]

[tool.setuptools.package-data]
"twinkle.resources" = ["*.yaml"]
```

- [ ] **Step 2: Make twinkle.resources a package (so find discovers it + package-data applies)**

Create `twinkle/resources/__init__.py` (empty file, just a docstring is fine):

```python
"""Bundled resources: config.yaml + example skills. Packaged as data files."""
```

- [ ] **Step 3: Reinstall editable so the new dep + resource are picked up**

Run: `.venv/Scripts/python.exe -m pip install -e ".[dev]"`
Expected: installs `pyyaml`; finishes without error.

- [ ] **Step 4: Verify pyyaml imports**

Run: `.venv/Scripts/python.exe -c "import yaml; print(yaml.__version__)"`
Expected: prints a version like `6.x`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml twinkle/resources/__init__.py
git commit -m "deps: add pyyaml + package twinkle.resources for config.yaml"
```

---

## Task 2: Create the config.yaml resource

**Files:**
- Create: `twinkle/resources/config.yaml`

- [ ] **Step 1: Write the YAML**

Create `twinkle/resources/config.yaml`:

```yaml
# Twinkle 运行时配置。${ENV:-default} 仅用于机密 + 部署相关变量(路径/端点/端口);
# 可调参数(max_steps/压缩阈值/skill mode/permissions 策略)直接写值——改它就编辑本文件。
# 本文件可安全 commit:机密走 .env / 环境变量,不落盘于此。
agentserver:
  host: ${TWINKLE_AGENTSERVER_HOST:-127.0.0.1}
  port: ${TWINKLE_AGENTSERVER_PORT:-18000}
gateway:
  host: ${TWINKLE_GATEWAY_HOST:-127.0.0.1}
  port: ${TWINKLE_GATEWAY_PORT:-19000}
workspace:
  dir: ${TWINKLE_WORKSPACE_DIR:-}        # 空 → ~/.twinkle;sandbox 根,command_exec/file_tools 收敛其下
logging:
  dir: ${TWINKLE_LOG_DIR:-}              # 空 → <workspace>/logs
sessions:
  dir: ${TWINKLE_SESSIONS_DIR:-}         # 空 → <workspace>/.twinkle_data/sessions
todos:
  dir: ${TWINKLE_TODOS_DIR:-}            # 空 → <workspace>/.twinkle_data/todos
llm:
  base_url: ${TWINKLE_LLM_BASE_URL:-https://api.openai.com/v1}
  model: ${TWINKLE_LLM_MODEL:-gpt-4o-mini}
  api_key: ${TWINKLE_LLM_API_KEY:-}       # 机密:放 .env,别 commit
agent:
  max_steps: 1000                         # ReAct 最大步数,超限 yield e2a.error(防不收敛硬上限,非目标)
context_compression:
  token_threshold: 60000                  # 估算 token(char//3,不精确)超此即压缩历史
  keep_recent_pairs: 6                    # 保留最近 N 个 user/assistant 对
  summary_prompt: "你是对话上下文压缩器。把给定历史对话压成一段摘要,保留关键事实、用户偏好、已做决策、工具调用结果,丢弃寒暄与冗余。用中文。"
skills:
  dir: ${TWINKLE_SKILLS_DIR:-}            # 空 → <workspace>/skills
  mode: all                               # all = 每步注入 skill 清单;auto_list = 模型按需调 list_skill 拉
  enabled: []                             # 列表;空 = 全开
permissions:
  enabled: false                          # false = 系统关(全 ALLOW,无审计/无 ASK;command_exec 仍走 builtin_rules)
  enabled_channels: [web]
  global_default: allow                   # allow | require-approval | deny
  tools:
    command_exec: require-approval         # allow | require-approval | deny(require-approval 引擎归一为 ASK)
    web_fetch: allow
    web_search: allow
    todo_create: allow
    todo_complete: allow
    todo_list: allow
  rules: []                               # 用户规则(同 jiuwenswarm rules[] 形状;v1 可空)
  approval_overrides: {}
  overrides_file: ${TWINKLE_PERMISSION_OVERRIDES_FILE:-}   # 空 → <workspace>/.twinkle_data/permission_overrides.json
  audit_file: ${TWINKLE_PERMISSION_AUDIT_FILE:-}          # 空 → <workspace>/logs/audit/permission_audit.jsonl
```

- [ ] **Step 2: Verify it parses + resolves**

Run: `.venv/Scripts/python.exe -c "import yaml; print(yaml.safe_load(open('twinkle/resources/config.yaml',encoding='utf-8'))['permissions']['tools']['command_exec'])"`
Expected: prints `require-approval`.

- [ ] **Step 3: Commit**

```bash
git add twinkle/resources/config.yaml
git commit -m "config: add resources/config.yaml (YAML + \${ENV:-default} interpolation)"
```

---

## Task 3: config_schema.py — pydantic models with 取值域 + derived paths (TDD)

**Files:**
- Create: `twinkle/config_schema.py`
- Test: `tests/test_config_schema.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_schema.py`:

```python
import os
import posixpath
from pathlib import Path

import pytest
from pydantic import ValidationError

from twinkle.config_schema import (
    TwinkleConfig, PermissionsConfig, SkillsConfig, PermissionTier, SkillMode,
)


def test_defaults_match_packaged_yaml():
    c = TwinkleConfig()
    assert c.agentserver.host == "127.0.0.1"
    assert c.agentserver.port == 18000
    assert c.gateway.port == 19000
    assert c.llm.base_url == "https://api.openai.com/v1"
    assert c.llm.model == "gpt-4o-mini"
    assert c.agent.max_steps == 1000
    assert c.context_compression.token_threshold == 60000
    assert c.context_compression.keep_recent_pairs == 6
    assert c.context_compression.summary_prompt.startswith("你是对话上下文压缩器")
    assert c.skills.mode == "all"
    assert c.skills.enabled == []
    assert c.permissions.enabled is False
    assert c.permissions.enabled_channels == ["web"]
    assert c.permissions.global_default == "allow"
    assert c.permissions.tools["command_exec"] == "require-approval"
    assert c.permissions.rules == []


def test_bad_permission_tier_raises():
    with pytest.raises(ValidationError):
        PermissionsConfig(enabled=True, enabled_channels=["web"],
                           global_default="BOGUS", tools={"x": "allow"})


def test_bad_tool_tier_raises():
    with pytest.raises(ValidationError):
        PermissionsConfig(enabled=True, enabled_channels=["web"],
                           global_default="allow", tools={"x": "BOGUS"})


def test_bad_skill_mode_raises():
    with pytest.raises(ValidationError):
        SkillsConfig(mode="nope")


def test_derived_paths_under_default_workspace():
    c = TwinkleConfig()  # workspace.dir empty -> ~/.twinkle
    home = str(Path.home())
    assert c.workspace.dir == posixpath.join(home, ".twinkle") or c.workspace.dir.endswith(".twinkle")
    assert c.sessions.dir.replace("\\", "/").endswith(".twinkle_data/sessions")
    assert c.todos.dir.replace("\\", "/").endswith(".twinkle_data/todos")
    assert c.logging.dir.replace("\\", "/").endswith("logs")
    assert c.skills.dir.replace("\\", "/").endswith("skills")
    assert c.permissions.overrides_file.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    assert c.permissions.audit_file.replace("\\", "/").endswith(
        "logs/audit/permission_audit.jsonl")


def test_derived_paths_under_custom_workspace():
    c = TwinkleConfig(workspace={"dir": "/tmp/twinkle-test"})
    assert c.sessions.dir.replace("\\", "/") == "/tmp/twinkle-test/.twinkle_data/sessions"
    assert c.todos.dir.replace("\\", "/") == "/tmp/twinkle-test/.twinkle_data/todos"
    assert c.logging.dir.replace("\\", "/") == "/tmp/twinkle-test/logs"
    assert c.skills.dir.replace("\\", "/") == "/tmp/twinkle-test/skills"
    assert c.permissions.audit_file.replace("\\", "/") == "/tmp/twinkle-test/logs/audit/permission_audit.jsonl"


def test_explicit_dirs_not_overwritten():
    c = TwinkleConfig(workspace={"dir": "/tmp/ws"},
                      sessions={"dir": "/tmp/sess"},
                      logging={"dir": "/tmp/logs"})
    assert c.sessions.dir == "/tmp/sess"
    assert c.logging.dir == "/tmp/logs"
    # permissions.audit_file derives from logging.dir
    assert c.permissions.audit_file.replace("\\", "/") == "/tmp/logs/audit/permission_audit.jsonl"


def test_workspace_tilde_expanded():
    c = TwinkleConfig(workspace={"dir": "~/twinkle-xyz"})
    assert "~" not in c.workspace.dir
    assert c.workspace.dir.replace("\\", "/").endswith("twinkle-xyz")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.config_schema'`.

- [ ] **Step 3: Write config_schema.py**

Create `twinkle/config_schema.py`:

```python
"""Typed config schema — pydantic models with Literal 取值域 + derived paths.

Loaded by config_loader from twinkle/resources/config.yaml. Field defaults mirror
the packaged config.yaml so the model is self-documenting and TwinkleConfig() with
no args produces the valid shipped defaults. The YAML is the user-facing source of
truth; this model validates it (bad tier / bad mode -> startup ValidationError).

Mirrors jiuwenswarm/resources/config.yaml field shapes (permissions.tools/rules,
skill_mode, telemetry omitted here — observability keeps its own OTEL_* env, v1).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

SkillMode = Literal["all", "auto_list"]
PermissionTier = Literal["allow", "require-approval", "deny"]


class AgentserverConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18000


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 19000


class WorkspaceConfig(BaseModel):
    dir: str = ""  # "" -> ~/.twinkle


class LoggingConfig(BaseModel):
    dir: str = ""  # "" -> <workspace>/logs


class SessionsConfig(BaseModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/sessions


class TodosConfig(BaseModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/todos


class LLMConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""


class AgentConfig(BaseModel):
    max_steps: int = 1000


class ContextCompressionConfig(BaseModel):
    token_threshold: int = 60000
    keep_recent_pairs: int = 6
    summary_prompt: str = (
        "你是对话上下文压缩器。把给定历史对话压成一段摘要,保留关键事实、用户偏好、"
        "已做决策、工具调用结果,丢弃寒暄与冗余。用中文。"
    )


class SkillsConfig(BaseModel):
    dir: str = ""  # "" -> <workspace>/skills
    mode: SkillMode = "all"
    enabled: list[str] = []  # [] = all skills open


class PermissionsConfig(BaseModel):
    enabled: bool = False
    enabled_channels: list[str] = ["web"]
    global_default: PermissionTier = "allow"
    tools: dict[str, PermissionTier] = {
        "command_exec": "require-approval",
        "web_fetch": "allow",
        "web_search": "allow",
        "todo_create": "allow",
        "todo_complete": "allow",
        "todo_list": "allow",
    }
    rules: list[dict] = []  # jiuwenswarm rules[] shape; v1 unvalidated internals
    approval_overrides: dict = {}
    overrides_file: str = ""  # "" -> <workspace>/.twinkle_data/permission_overrides.json
    audit_file: str = ""  # "" -> <logging.dir>/audit/permission_audit.jsonl


class TwinkleConfig(BaseModel):
    agentserver: AgentserverConfig = AgentserverConfig()
    gateway: GatewayConfig = GatewayConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()
    logging: LoggingConfig = LoggingConfig()
    sessions: SessionsConfig = SessionsConfig()
    todos: TodosConfig = TodosConfig()
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
    context_compression: ContextCompressionConfig = ContextCompressionConfig()
    skills: SkillsConfig = SkillsConfig()
    permissions: PermissionsConfig = PermissionsConfig()

    @model_validator(mode="after")
    def _derive_paths(self) -> "TwinkleConfig":
        # workspace first — everything else hangs off it.
        ws = self.workspace.dir or str(Path.home() / ".twinkle")
        ws = os.path.expanduser(ws)
        self.workspace.dir = ws
        if not self.logging.dir:
            self.logging.dir = str(Path(ws) / "logs")
        if not self.sessions.dir:
            self.sessions.dir = str(Path(ws) / ".twinkle_data" / "sessions")
        if not self.todos.dir:
            self.todos.dir = str(Path(ws) / ".twinkle_data" / "todos")
        if not self.skills.dir:
            self.skills.dir = str(Path(ws) / "skills")
        if not self.permissions.overrides_file:
            self.permissions.overrides_file = str(
                Path(ws) / ".twinkle_data" / "permission_overrides.json")
        if not self.permissions.audit_file:
            self.permissions.audit_file = str(
                Path(self.logging.dir) / "audit" / "permission_audit.jsonl")
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_schema.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/config_schema.py tests/test_config_schema.py
git commit -m "config: add pydantic config_schema with Literal 取值域 + derived paths"
```

---

## Task 4: config_loader.py — `${ENV:-default}` resolver (TDD)

**Files:**
- Create: `twinkle/config_loader.py`
- Test: `tests/test_config_loader.py`

- [ ] **Step 1: Write the failing tests (resolver portion)**

Create `tests/test_config_loader.py`:

```python
from twinkle.config_loader import _resolve_env_vars


def test_env_value_wins_over_default(monkeypatch):
    monkeypatch.setenv("MY_VAR", "from-env")
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: from-env"


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: fallback"


def test_empty_env_falls_to_default(monkeypatch):
    # mirrors current `os.getenv(X) or default` (empty string is falsy)
    monkeypatch.setenv("MY_VAR", "")
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: fallback"


def test_no_default_unset_yields_empty(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("v: '${MY_VAR}'") == "v: ''"


def test_plain_text_unchanged(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("just text: 18000") == "just text: 18000"


def test_multiple_occurrences(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.delenv("B", raising=False)
    assert _resolve_env_vars("${A:-0}/${B:-0}") == "1/0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_loader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.config_loader'`.

- [ ] **Step 3: Write config_loader.py (resolver + _load_env_file + load_config)**

Create `twinkle/config_loader.py`:

```python
"""Config loader: read YAML -> resolve ${ENV:-default} -> pydantic-validate.

${VAR:-default} semantics mirror jiuwenswarm/resources/config.yaml: a non-empty
real env var wins; an empty/unset env var falls back to the default; ${VAR} with
no default and no env yields empty string (mirrors the old `os.getenv(X) or default`
where empty is falsy).

_load_env_file() populates os.environ from a .env at the repo root FIRST (real env
still wins via setdefault), so ${TWINKLE_LLM_API_KEY} resolves from .env.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from twinkle.config_schema import TwinkleConfig

# Config YAML ships as a package data file next to this module.
CONFIG_YAML_PATH = Path(__file__).resolve().parent / "resources" / "config.yaml"

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _load_env_file() -> None:
    """Populate os.environ from a .env at the repo root.

    Real env wins (setdefault), so .env is a convenience default, not an override.
    Mirrors the original twinkle/config.py parser verbatim.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _resolve_env_vars(text: str) -> str:
    """Replace ${VAR:-default} / ${VAR} using os.environ (empty env -> default)."""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        val = os.environ.get(name)
        if val:  # non-empty real env wins; empty falls through
            return val
        return default if default is not None else ""

    return _ENV_RE.sub(_replace, text)


def load_config(config_path: str | Path | None = None) -> TwinkleConfig:
    """Read the YAML at config_path (default: packaged resources/config.yaml),
    resolve ${ENV:-default}, parse, and validate into TwinkleConfig. Raises on
    missing file or invalid config (bad tier/mode -> pydantic ValidationError).
    """
    _load_env_file()  # so ${TWINKLE_LLM_API_KEY} etc. resolve from .env
    path = Path(config_path) if config_path else CONFIG_YAML_PATH
    text = _resolve_env_vars(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(text) or {}
    return TwinkleConfig(**data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_loader.py -v`
Expected: all 6 resolver tests PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/config_loader.py tests/test_config_loader.py
git commit -m "config: add config_loader (\${ENV:-default} resolver + load_config)"
```

---

## Task 5: config_loader.load_config end-to-end (TDD)

**Files:**
- Test: `tests/test_config_loader.py` (append)

- [ ] **Step 1: Append load_config tests**

Append to `tests/test_config_loader.py`:

```python
from twinkle.config_loader import load_config


def test_loads_packaged_defaults(monkeypatch):
    # hermetic: clear surviving env so packaged defaults apply
    for k in ("TWINKLE_AGENTSERVER_PORT", "TWINKLE_LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c = load_config()  # reads packaged resources/config.yaml
    assert c.agentserver.port == 18000
    assert c.permissions.enabled is False
    assert c.permissions.tools["command_exec"] == "require-approval"
    assert c.skills.mode == "all"


def test_env_override_in_yaml(monkeypatch):
    monkeypatch.setenv("TWINKLE_AGENTSERVER_PORT", "12345")
    c = load_config()  # ${TWINKLE_AGENTSERVER_PORT:-18000} -> 12345
    assert c.agentserver.port == 12345


def test_empty_env_uses_yaml_default(monkeypatch):
    monkeypatch.setenv("TWINKLE_AGENTSERVER_PORT", "")
    c = load_config()
    assert c.agentserver.port == 18000


def test_custom_path_overrides_packaged(tmp_path):
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "agentserver:\n  host: 0.0.0.0\n  port: 9999\n"
        "permissions:\n  enabled: true\n  tools:\n    echo: deny\n",
        encoding="utf-8",
    )
    c = load_config(custom)  # other sections come from schema defaults
    assert c.agentserver.port == 9999
    assert c.permissions.enabled is True
    assert c.permissions.tools["echo"] == "deny"
    # schema defaults fill the gaps
    assert c.skills.mode == "all"


def test_bad_tier_in_yaml_raises(monkeypatch, tmp_path):
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  tools:\n    command_exec: BOGUS\n", encoding="utf-8")
    import pytest
    with pytest.raises(Exception):  # pydantic ValidationError
        load_config(custom)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_loader.py -v`
Expected: all tests (resolver + load_config) PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config_loader.py
git commit -m "test: cover load_config packaged defaults + env override + custom path"
```

---

## Task 6: Rewrite config.py to load via the loader + export flat constants (TDD)

**Files:**
- Modify: `twinkle/config.py` (full rewrite)
- Test: `tests/test_config_constants.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_constants.py`:

```python
import importlib


def test_constants_match_packaged_defaults(monkeypatch):
    for k in ("TWINKLE_WORKSPACE_DIR", "TWINKLE_LLM_API_KEY",
              "TWINKLE_AGENT_MAX_STEPS"):
        monkeypatch.delenv(k, raising=False)
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.AGENTSERVER_PORT == 18000
    assert cfg.GATEWAY_PORT == 19000
    assert cfg.LLM_MODEL == "gpt-4o-mini"
    assert cfg.AGENT_MAX_STEPS == 1000
    assert cfg.SKILL_MODE == "all"
    assert cfg.ENABLED_SKILLS == []
    assert cfg.CONTEXT_TOKEN_THRESHOLD == 60000
    assert cfg.CONTEXT_KEEP_RECENT_PAIRS == 6
    assert cfg.CONTEXT_SUMMARY_PROMPT.startswith("你是对话上下文压缩器")
    assert cfg.PERMISSIONS_ENABLED is False
    assert cfg.PERMISSIONS_ENABLED_CHANNELS == {"web"}
    assert cfg.PERMISSIONS_GLOBAL_DEFAULT == "allow"
    assert cfg.PERMISSIONS_TOOLS["command_exec"] == "require-approval"
    assert cfg.PERMISSIONS_RULES == []
    assert isinstance(cfg.PERMISSIONS, dict)


def test_workspace_env_derives_paths(monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", "/tmp/twinkle-const-test")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.WORKSPACE_DIR.replace("\\", "/") == "/tmp/twinkle-const-test"
    assert cfg.SESSIONS_DIR.replace("\\", "/").endswith(
        ".twinkle_data/sessions")
    assert cfg.PERMISSION_OVERRIDES_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    importlib.reload(cfg)  # restore for downstream tests
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_constants.py -v`
Expected: FAIL (old config.py still uses env getters; `test_constants_match_packaged_defaults` may pass by coincidence, but `test_workspace_env_derives_paths` may also pass — the real signal is that the suite still works. To force a failure before rewrite, confirm the import path: the tests reference constants that exist. If they pass already, proceed — Task 6's rewrite must keep them green. If you want a red signal, temporarily assert `cfg.AGENT_MAX_STEPS == 999` — no, don't. Skip the red step here: this task is a refactor preserving behavior; the guard is that ALL tests stay green after rewrite.)

- [ ] **Step 3: Rewrite config.py**

Replace the entire contents of `twinkle/config.py` with:

```python
"""Runtime configuration — loaded from resources/config.yaml.

The YAML (twinkle/resources/config.yaml) is the user-facing source of truth:
sections + comments + Literal 取值域, with ${ENV:-default} for secrets/deploy
vars and literals for tunables. config_loader reads + resolves + parses it;
config_schema validates it (bad tier/mode -> startup error). This module
flattens the validated `settings` into the same module-level constants the rest
of the codebase already imports (from twinkle.config import X), so consumers
don't change.

Mirrors jiuwenswarm/resources/config.yaml. observability still reads its own
OTEL_* env (observability/config.py) — not folded here (v1). Workspace
bootstrap (ensure_workspace_dir) lives in twinkle/workspace.py now.
"""
from twinkle.config_loader import load_config
from twinkle.config_schema import TwinkleConfig

settings: TwinkleConfig = load_config()

# --- agentserver / gateway ---
AGENTSERVER_HOST = settings.agentserver.host
AGENTSERVER_PORT = settings.agentserver.port
GATEWAY_HOST = settings.gateway.host
GATEWAY_PORT = settings.gateway.port

# --- workspace + derived dirs (sandbox + persistence roots) ---
WORKSPACE_DIR = settings.workspace.dir
LOG_DIR = settings.logging.dir
SESSIONS_DIR = settings.sessions.dir
TODOS_DIR = settings.todos.dir

# --- skills (Phase 7) ---
SKILLS_DIR = settings.skills.dir
SKILL_MODE = settings.skills.mode
ENABLED_SKILLS = list(settings.skills.enabled)

# --- LLM (OpenAI-compatible) ---
LLM_BASE_URL = settings.llm.base_url
LLM_API_KEY = settings.llm.api_key
LLM_MODEL = settings.llm.model

# --- agent loop ---
AGENT_MAX_STEPS = settings.agent.max_steps

# --- context compression (Phase 3) ---
CONTEXT_TOKEN_THRESHOLD = settings.context_compression.token_threshold
CONTEXT_KEEP_RECENT_PAIRS = settings.context_compression.keep_recent_pairs
CONTEXT_SUMMARY_PROMPT = settings.context_compression.summary_prompt

# --- permissions (Phase 4) ---
PERMISSIONS = settings.permissions.model_dump()
PERMISSIONS_ENABLED = settings.permissions.enabled
PERMISSIONS_ENABLED_CHANNELS = set(settings.permissions.enabled_channels)
PERMISSIONS_GLOBAL_DEFAULT = settings.permissions.global_default
PERMISSIONS_TOOLS = dict(settings.permissions.tools)
PERMISSIONS_RULES = list(settings.permissions.rules)
PERMISSION_OVERRIDES_FILE = settings.permissions.overrides_file
PERMISSION_AUDIT_FILE = settings.permissions.audit_file
```

- [ ] **Step 4: Run the new tests + the existing config tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config_constants.py tests/test_config_context.py tests/test_config_schema.py tests/test_config_loader.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite to catch regressions (expect permission-config failures — fixed in Task 8/9)**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: `tests/test_permissions_config.py` failures (they set the now-removed `TWINKLE_PERMISSIONS` env) + `tests/test_permissions_e2e.py` failure (same). Everything else PASS. Do NOT commit yet — Tasks 8 & 9 fix those.

- [ ] **Step 6: Commit**

```bash
git add twinkle/config.py tests/test_config_constants.py
git commit -m "config: load from YAML via config_loader, export flat constants (behavior-preserving)"
```

---

## Task 7: Move workspace bootstrap to workspace.py + update import sites

**Files:**
- Create: `twinkle/workspace.py`
- Modify: `twinkle/agentserver/__main__.py:5`
- Modify: `twinkle/agentserver/server.py:146`

- [ ] **Step 1: Create workspace.py**

Create `twinkle/workspace.py`:

```python
"""Workspace bootstrap — ensure_workspace_dir + seed example skills.

Moved out of config.py so config stays pure constants; this is the only runtime
side-effect module (called at server startup, never at import — keeps tests that
monkeypatch WORKSPACE_DIR side-effect-free on the host).
"""
import os
import shutil
from pathlib import Path

from twinkle.config import SKILLS_DIR, WORKSPACE_DIR


def ensure_workspace_dir() -> str:
    """Create WORKSPACE_DIR + SKILLS_DIR if missing (idempotent), seed example
    skills on first start. Call at server startup. Not called at import time."""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    os.makedirs(SKILLS_DIR, exist_ok=True)
    _seed_example_skills(SKILLS_DIR)
    return WORKSPACE_DIR


def _seed_example_skills(skills_dir: str) -> None:
    """First-start: copy bundled example skills (twinkle/resources/skills/*) to
    <WORKSPACE>/skills. Skip if target exists (preserve user edits)."""
    src = Path(__file__).resolve().parent / "resources" / "skills"
    if not src.is_dir():
        return
    for skill_dir in src.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        dst = Path(skills_dir) / skill_dir.name
        if dst.exists():
            continue  # 用户已有(可能改过),不覆盖
        shutil.copytree(skill_dir, dst)
```

- [ ] **Step 2: Update the two import sites**

In `twinkle/agentserver/__main__.py`, change line 5:

```python
from twinkle.workspace import ensure_workspace_dir
```

In `twinkle/agentserver/server.py`, change line 146:

```python
    from twinkle.workspace import ensure_workspace_dir
```

- [ ] **Step 3: Verify ensure_workspace_dir is no longer in config.py**

Run: `.venv/Scripts/python.exe -c "import twinkle.config as c; assert not hasattr(c, 'ensure_workspace_dir'); import twinkle.workspace as w; assert callable(w.ensure_workspace_dir); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Run the suite (minus the known-broken permission tests) to verify nothing else broke**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_permissions_config.py --ignore=tests/test_permissions_e2e.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/workspace.py twinkle/agentserver/__main__.py twinkle/agentserver/server.py
git commit -m "config: move ensure_workspace_dir to workspace.py, update import sites"
```

---

## Task 8: Rewrite tests/test_permissions_config.py for YAML config

**Files:**
- Rewrite: `tests/test_permissions_config.py`

The old file tested `TWINKLE_PERMISSIONS` env-var parsing (JSON/bare-bool/invalid fallback) — that env var is gone. Replace with: defaults from packaged YAML, path derivation, schema validation, and enable-via-custom-YAML.

- [ ] **Step 1: Rewrite the test file**

Replace the entire contents of `tests/test_permissions_config.py` with:

```python
"""Permission config now loads from resources/config.yaml (no TWINKLE_PERMISSIONS env).

v1 dropped the single JSON env var; enabling/overrides are done by editing the
YAML (or pointing load_config at a custom YAML in tests)."""
import importlib

import pytest


def test_defaults_disabled(monkeypatch):
    monkeypatch.delenv("TWINKLE_PERMISSIONS", raising=False)  # no-op now; kept hermetic
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is False
    assert cfg.PERMISSIONS_ENABLED_CHANNELS == {"web"}
    assert cfg.PERMISSIONS_TOOLS.get("command_exec") == "require-approval"
    assert cfg.PERMISSIONS_GLOBAL_DEFAULT == "allow"
    assert cfg.PERMISSIONS_RULES == []


def test_override_paths_under_workspace(monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", "/tmp/twinkle-test")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSION_OVERRIDES_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    assert cfg.PERMISSION_AUDIT_FILE.replace("\\", "/").endswith(
        "logs/audit/permission_audit.jsonl")
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    importlib.reload(cfg)


def test_bad_tier_in_config_raises(tmp_path):
    from twinkle.config_loader import load_config
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  global_default: BOGUS\n", encoding="utf-8")
    with pytest.raises(Exception):  # pydantic ValidationError
        load_config(custom)


def test_enable_and_tool_override_via_yaml(tmp_path):
    from twinkle.config_loader import load_config
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  enabled: true\n  tools:\n    echo: deny\n",
        encoding="utf-8")
    c = load_config(custom)
    assert c.permissions.enabled is True
    assert c.permissions.tools["echo"] == "deny"
    # default tools still present (schema default fills command_exec etc.)
    assert c.permissions.tools["command_exec"] == "require-approval"
```

- [ ] **Step 2: Run the rewritten test file**

Run: `.venv/Scripts/python.exe -m pytest tests/test_permissions_config.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_permissions_config.py
git commit -m "test: rewrite permissions_config for YAML loader (drop TWINKLE_PERMISSIONS env tests)"
```

---

## Task 9: Fix tests/test_permissions_e2e.py enable mechanism

**Files:**
- Modify: `tests/test_permissions_e2e.py:28-32`

The e2e test enabled permissions via `TWINKLE_PERMISSIONS` env (removed). Switch to monkeypatching the config constants (matches how `test_file_tools` monkeypatches `WORKSPACE_DIR`), since `permission_engine()` reads them fresh at call time.

- [ ] **Step 1: Replace the enable block**

In `tests/test_permissions_e2e.py`, replace lines 28-32:

```python
def test_full_approval_flow_through_gateway_and_agentserver(free_port, tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", '{"enabled": true, "tools": {"echo": "require-approval"}}')
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import importlib, twinkle.config as cfg
    importlib.reload(cfg)
```

with:

```python
def test_full_approval_flow_through_gateway_and_agentserver(free_port, tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import importlib, twinkle.config as cfg
    importlib.reload(cfg)
    # Enable permissions + register the echo tool tier via the config constants
    # that permission_engine() reads fresh at call time (mirrors test_file_tools
    # monkeypatching WORKSPACE_DIR). TWINKLE_PERMISSIONS env was removed in v1.
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    monkeypatch.setattr(cfg, "PERMISSIONS_TOOLS",
                        {**cfg.PERMISSIONS_TOOLS, "echo": "require-approval"})
```

- [ ] **Step 2: Run the e2e test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_permissions_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_permissions_e2e.py
git commit -m "test: enable permissions via constant monkeypatch in e2e (TWINKLE_PERMISSIONS env removed)"
```

---

## Task 10: Docs — .env.example, CLAUDE.md config table, architecture.md §9.2

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md` (Configuration section table)
- Modify: `docs/architecture.md` §9.2

- [ ] **Step 1: Slim .env.example to surviving env vars + a pointer to config.yaml**

Replace `.env.example` with:

```bash
# Copy this file to .env and fill in real values. .env is gitignored (safe for secrets).
# Real environment variables still override values here.
#
# Most config now lives in twinkle/resources/config.yaml (edit that file for
# tunables: max_steps, context_compression, skills.mode, permissions policy).
# Only secrets + deploy-variable paths/endpoints/ports stay as env vars here,
# resolved into the YAML via ${ENV:-default}.

TWINKLE_LLM_BASE_URL=https://api.openai.com/v1
TWINKLE_LLM_API_KEY=
TWINKLE_LLM_MODEL=gpt-4o-mini

# Optional: override ports / workspace (defaults shown)
# TWINKLE_AGENTSERVER_PORT=18000
# TWINKLE_GATEWAY_PORT=19000
# TWINKLE_WORKSPACE_DIR=~/.twinkle

# --- observability (OTel, agentserver-only; default off = zero-cost no-op) ---
# OTEL_ENABLED=true
# OTEL_TRACES_EXPORTER=otlp
# OTEL_METRICS_EXPORTER=otlp
# OTEL_EXPORTER_OTLP_PROTOCOL=grpc
# OTEL_EXPORTER_OTLP_ENDPOINT=http://101.37.215.110:4317
# OTEL_SERVICE_NAME=twinkle-agentserver
```

- [ ] **Step 2: Update CLAUDE.md Configuration table**

In `CLAUDE.md`, find the Configuration table (the `| Variable | Default | Notes |` block). Add a lead-in line after the intro paragraph: "Most tunable config now lives in `twinkle/resources/config.yaml` (YAML + `${ENV:-default}` interpolation + pydantic validation, mirroring `jiuwenswarm/resources/config.yaml`). Only secrets + deploy-variable paths/endpoints/ports remain env vars." Then in the table, **remove** the rows for `TWINKLE_AGENT_MAX_STEPS`, `TWINKLE_CONTEXT_*` (3), `TWINKLE_SKILL_MODE`, `TWINKLE_ENABLED_SKILLS`, and rewrite the `TWINKLE_PERMISSIONS` row to note it is removed (replaced by `permissions:` block in config.yaml). Keep rows for `TWINKLE_AGENTSERVER_*`, `TWINKLE_GATEWAY_*`, `TWINKLE_WORKSPACE_DIR`, `TWINKLE_LOG_DIR`, `TWINKLE_SESSIONS_DIR`, `TWINKLE_TODOS_DIR`, `TWINKLE_LLM_*`, `TWINKLE_SKILLS_DIR`, `TWINKLE_PERMISSION_OVERRIDES_FILE`, `TWINKLE_PERMISSION_AUDIT_FILE`. Add one new row:

```
| `TWINKLE_CONFIG_FILE` | (packaged) | Not implemented in v1 — config read from `twinkle/resources/config.yaml` only. Future: user-override path. |
```

(Strike that — v1 has no `TWINKLE_CONFIG_FILE` env; do NOT add the row. Instead add a note line: "v1 reads only the packaged `resources/config.yaml`; a user-override path is future work.")

- [ ] **Step 3: Update architecture.md §9.2**

In `docs/architecture.md`, §9.2 "环境变量":
- Add a paragraph after the intro: "v1 起,可调参数(`max_steps`/`context_compression`/`skills.mode`/`permissions` 策略)改为编辑 `twinkle/resources/config.yaml`(YAML + `${ENV:-default}` 插值 + pydantic 校验,镜像 `jiuwenswarm/resources/config.yaml`)。env 变量仅保留机密 + 部署相关变量(端口/路径/端点)。"
- In the env var table, remove rows for `TWINKLE_AGENT_MAX_STEPS` and note the moved ones. Add a `config.yaml` reference row pointing to the file.
- In the **Permissions 配置** block, replace the `TWINKLE_PERMISSIONS` JSON description with: "`permissions:` 块在 `config.yaml`(`enabled`/`enabled_channels`/`global_default`/`tools`/`rules`/`approval_overrides`/`overrides_file`/`audit_file`)。`enabled=false` = 系统关(全 ALLOW,无审计/无 ASK;command_exec 仍走 builtin_rules)。tier 取值域:`allow | require-approval | deny`(`require-approval` 引擎归一为 ASK)。"

- [ ] **Step 4: Verify docs render sanity (no broken table)**

Run: `.venv/Scripts/python.exe -c "print(open('CLAUDE.md',encoding='utf-8').read()[:50])"`
Expected: prints the first line of CLAUDE.md without error.

- [ ] **Step 5: Commit**

```bash
git add .env.example CLAUDE.md docs/architecture.md
git commit -m "docs: config moved to config.yaml; slim .env.example, sync CLAUDE.md + architecture §9.2"
```

---

## Task 11: Full suite + final cleanup

**Files:** none (verification + commit)

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: ALL tests PASS (including the rewritten `test_permissions_config.py` and fixed `test_permissions_e2e.py`).

- [ ] **Step 2: Smoke-test the loader from a clean shell (no .env)**

Run: `.venv/Scripts/python.exe -c "from twinkle.config import LLM_API_KEY, AGENTSERVER_PORT, PERMISSIONS_ENABLED; print(LLM_API_KEY, AGENTSERVER_PORT, PERMISSIONS_ENABLED)"`
Expected: prints ` 18000 False` (empty API key, default port, permissions off).

- [ ] **Step 3: Verify no stale references to removed env vars in the codebase**

Run: `.venv/Scripts/python.exe -c "import subprocess; print(subprocess.run(['grep','-rn','TWINKLE_PERMISSIONS','twinkle/'],capture_output=True,text=True).stdout or 'none in twinkle/')"`
Expected: prints `none in twinkle/` (no code references the removed env var; tests may still mention it in comments — acceptable).

- [ ] **Step 4: Final commit (if any cleanup remains)**

If Step 3 surfaced stale code references, fix and commit:

```bash
git add -A
git commit -m "config: final cleanup after YAML migration"
```

If nothing to clean, skip — the work is complete.

---

## Self-Review (run after writing, before handoff)

- **Spec coverage:** §3 file layout → Tasks 1-7; §4 config.yaml → Task 2; §4.1 env 存废 → Tasks 2,6,10; §5 schema → Task 3; §6 loader → Tasks 4-6; §7 secrets → Tasks 2,10; §8 out-of-scope (deny patterns, observability, user-override file) → explicitly untouched; §9 test impact → Tasks 6,8,9. ✓
- **Placeholder scan:** no TBD/TODO; every code step has full code. ✓
- **Type consistency:** `PermissionTier`/`SkillMode` defined in Task 3, used in Task 2 YAML + Task 8 tests; `load_config(config_path=None)` signature consistent across Tasks 4,5,6,8; `TwinkleConfig` fields match YAML keys. ✓
- **Known behavior change (flagged in spec §4.1):** `TWINKLE_PERMISSIONS`/`TWINKLE_AGENT_MAX_STEPS`/`TWINKLE_CONTEXT_*`/`TWINKLE_SKILL_MODE`/`TWINKLE_ENABLED_SKILLS` env vars no longer honored — tests rewritten in Tasks 8 & 9. ✓
