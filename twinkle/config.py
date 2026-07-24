"""Runtime configuration, read from environment with sensible defaults.

Mirrors jiuwenclaw's two-process port layout:
- AgentServer (ws server, heavy execution core): 18000
- Gateway (ws client to agentserver + browser ws server): 19000

Config sources (first match wins): real environment variables, then a
``.env`` file at the repo root (gitignored — for local secrets like the
LLM API key). No python-dotenv dependency; a minimal hand-rolled parser.
"""
import os
from pathlib import Path


def _load_env_file() -> None:
    """Populate os.environ from a .env file at the repo root.

    Real environment variables always win (os.environ.setdefault), so
    ``.env`` is a convenience default, not an override.
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


_load_env_file()

AGENTSERVER_HOST = os.getenv("TWINKLE_AGENTSERVER_HOST", "127.0.0.1")
AGENTSERVER_PORT = int(os.getenv("TWINKLE_AGENTSERVER_PORT", "18000"))

GATEWAY_HOST = os.getenv("TWINKLE_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("TWINKLE_GATEWAY_PORT", "19000"))

# --- Workspace (sandbox root for command_exec / file_tools) ---
# Defaults to ~/.twinkle (the user home) so generated files land under the
# user's directory, not the repo / code dir (mirrors jiuwenswarm's
# ~/.jiuwenswarm). Override with TWINKLE_WORKSPACE_DIR (~/... expanded) to
# point elsewhere. command_exec / file_tools confine file ops under this root
# so the agent cannot escape the workspace.
WORKSPACE_DIR = os.path.expanduser(
    os.getenv("TWINKLE_WORKSPACE_DIR") or str(Path.home() / ".twinkle")
)

# --- Sessions persistence (disk-backed session store) ---
# Per-session dir layout: <SESSIONS_DIR>/<session_id>/{metadata.json,history.json}.
# Defaults to <WORKSPACE_DIR>/.twinkle_data/sessions (which lives under ~/.twinkle
# unless TWINKLE_WORKSPACE_DIR is overridden). Gitignored. If you want strict
# isolation from the command_exec / file_tools sandbox, point this outside WORKSPACE_DIR.
SESSIONS_DIR = os.getenv("TWINKLE_SESSIONS_DIR") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "sessions"
)


def ensure_workspace_dir() -> str:
    """Create WORKSPACE_DIR if missing (idempotent). Call at server startup
    so read/list/glob work on a fresh ~/.twinkle without a "not found" error.
    Not called at import time to keep tests (which monkeypatch WORKSPACE_DIR)
    side-effect-free on the host.
    """
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    return WORKSPACE_DIR

# --- LLM (OpenAI-compatible) ---
# Point at any OpenAI-compatible endpoint by overriding these env vars
# (or by setting them in .env at the repo root).
LLM_BASE_URL = os.getenv("TWINKLE_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("TWINKLE_LLM_API_KEY", "")
LLM_MODEL = os.getenv("TWINKLE_LLM_MODEL", "gpt-4o-mini")

# --- agent loop ---
# Max ReAct steps before the loop gives up (yields e2a.error). Default 1000 —
# supports long agentic runs (Claude Code-scale traces run thousands of spans).
# This is a runaway backstop, not a target: a non-converging loop burns up to
# this many LLM calls before being caught.
AGENT_MAX_STEPS = int(os.getenv("TWINKLE_AGENT_MAX_STEPS", "1000"))

# --- context compression (Phase 3) ---
# When estimated tokens of the session messages exceed this threshold, the
# agent loop compresses prior history (sliding window + LLM summary) before
# feeding the LLM. Estimate is char-based (//3) — imprecise for glm but fine
# for a learning project. See context_compression.py.
CONTEXT_TOKEN_THRESHOLD = int(os.getenv("TWINKLE_CONTEXT_TOKEN_THRESHOLD", "60000"))
CONTEXT_KEEP_RECENT_PAIRS = int(os.getenv("TWINKLE_CONTEXT_KEEP_RECENT_PAIRS", "6"))
CONTEXT_SUMMARY_PROMPT = os.getenv(
    "TWINKLE_CONTEXT_SUMMARY_PROMPT",
    "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、已做决策、工具调用结果，丢弃寒暄与冗余。用中文。",
)

# --- permissions (Phase 4) ---
# Single JSON env var (mirrors OTEL opt-in): enabled=false = system off
# (all ALLOW, no audit, no ASK; command_exec still uses its own blocklist).
import json as _json

_PERMISSIONS_DEFAULT = {
    "enabled": False,
    "enabled_channels": ["web"],
    "global_default": "allow",
    "tools": {
        "command_exec": "require-approval",
        "web_fetch": "allow",
        "web_search": "allow",
        "todo_create": "allow",
        "todo_complete": "allow",
        "todo_list": "allow",
    },
    "rules": [],
    "approval_overrides": {},
}


def _load_permissions() -> dict:
    raw = os.getenv("TWINKLE_PERMISSIONS")
    if not raw:
        return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                for k, v in _PERMISSIONS_DEFAULT.items()}
    try:
        user = _json.loads(raw)
    except _json.JSONDecodeError:
        # invalid JSON -> fall back to defaults (engine will log nothing; safe)
        return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                for k, v in _PERMISSIONS_DEFAULT.items()}
    merged = dict(_PERMISSIONS_DEFAULT)
    merged.update(user)
    return merged


PERMISSIONS = _load_permissions()
PERMISSIONS_ENABLED = bool(PERMISSIONS.get("enabled", False))
PERMISSIONS_ENABLED_CHANNELS = set(PERMISSIONS.get("enabled_channels", ["web"]))
PERMISSIONS_GLOBAL_DEFAULT = PERMISSIONS.get("global_default", "allow")
PERMISSIONS_TOOLS = PERMISSIONS.get("tools", {})
PERMISSIONS_RULES = PERMISSIONS.get("rules", [])
PERMISSION_OVERRIDES_FILE = os.getenv("TWINKLE_PERMISSION_OVERRIDES_FILE") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "permission_overrides.json"
)
PERMISSION_AUDIT_FILE = os.getenv("TWINKLE_PERMISSION_AUDIT_FILE") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "permission_audit.jsonl"
)
