"""Runtime configuration — loaded from resources/config.yaml.

The YAML (twinkle/resources/config.yaml) is the user-facing source of truth:
sections + comments + Literal 取值域, with ${ENV:-default} for secrets/deploy
vars and literals for tunables. The loader (config.loader) reads + resolves +
parses it; the schema (config.schema) validates it (bad tier/mode -> startup
error). This package's __init__ flattens the validated `settings` into the same
module-level constants the rest of the codebase already imports
(`from twinkle.config import X`), so consumers don't change.

Mirrors jiuwenswarm/resources/config.yaml. observability still reads its own
OTEL_* env (observability/config.py) — not folded here (v1). Workspace
bootstrap (ensure_workspace_dir) lives in twinkle/workspace.py.
"""
from .loader import load_config
from .schema import TwinkleConfig

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
