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
SKILLS_SKILLNET_API_URL = settings.skills.skillnet_api_url
SKILLS_SKILLHUB_API_URL = settings.skills.skillhub_api_url
SKILLS_GITHUB_TOKEN = settings.skills.github_token
SKILLS_REMOTE_TIMEOUT = settings.skills.remote_timeout
SKILLS_REMOTE_MAX_RETRIES = settings.skills.remote_max_retries

# --- memory (Phase 5a) ---
MEMORY_DIR = settings.memory.dir
MEMORY_EMBED_MODEL = settings.memory.embed_model
MEMORY_QUERY_MAX_RESULTS = settings.memory.query.max_results
MEMORY_HYBRID_VECTOR_WEIGHT = settings.memory.hybrid.vector_weight
MEMORY_HYBRID_TEXT_WEIGHT = settings.memory.hybrid.text_weight
MEMORY_HYBRID_CANDIDATE_MULTIPLIER = settings.memory.hybrid.candidate_multiplier
MEMORY_CHUNKING_TOKENS = settings.memory.chunking.tokens
MEMORY_CHUNKING_OVERLAP = settings.memory.chunking.overlap
MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE = settings.memory.cleanup.max_chunks_per_file
MEMORY_AUTO_INJECT_ENABLED = settings.memory.auto_inject.enabled
MEMORY_AUTO_INJECT_MAX_CHARS = settings.memory.auto_inject.max_chars
MEMORY_FLUSH_ENABLED = settings.memory.flush.enabled
MEMORY_DREAMING_ENABLED = settings.memory.dreaming.enabled
MEMORY_DREAMING_INTERVAL_SECONDS = settings.memory.dreaming.interval_seconds
MEMORY_DREAMING_START_DELAY_SECONDS = settings.memory.dreaming.start_delay_seconds
MEMORY_DREAMING_TOP_K = settings.memory.dreaming.top_k

# --- LLM (OpenAI-compatible) ---
LLM_BASE_URL = settings.llm.base_url
LLM_API_KEY = settings.llm.api_key
LLM_MODEL = settings.llm.model
LLM_TIMEOUT = settings.llm.timeout

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

# --- subagent (Phase 8) ---
SUBAGENT_MAX_STEPS = settings.subagent.max_steps
SUBAGENT_HARD_TIMEOUT = settings.subagent.hard_timeout
SUBAGENT_SOFT_TIMEOUT = settings.subagent.soft_timeout
SUBAGENT_ABORT_TIMEOUT = settings.subagent.abort_timeout
SUBAGENT_CHILD_PERMISSIONS = settings.subagent.child_permissions
SUBAGENT_MAX_RESULT_CHARS = settings.subagent.max_result_chars
SUBAGENT_LIST_SESSIONS_FILTER = settings.subagent.list_sessions_filter

# --- overflow recovery (Phase 9) ---
OVERFLOW_MAX_RECOVERY_ATTEMPTS = settings.overflow_recovery.max_recovery_attempts
OVERFLOW_THRESHOLD_RATIO = settings.overflow_recovery.threshold_ratio
OVERFLOW_AGGRESSIVE_KEEP_RECENT = settings.overflow_recovery.aggressive_keep_recent
OVERFLOW_CONTEXT_WINDOW_LIMIT = settings.overflow_recovery.context_window_limit_tokens

# --- repeat tool detection (Phase 9) ---
REPEAT_TOOL_HISTORY_SIZE = settings.repeat_tool_detection.history_size
REPEAT_TOOL_REPEAT_WARN = settings.repeat_tool_detection.repeat_warn
REPEAT_TOOL_PINGPONG_WARN = settings.repeat_tool_detection.pingpong_warn
REPEAT_TOOL_LOOP_BLOCK = settings.repeat_tool_detection.loop_block
REPEAT_TOOL_GLOBAL_STOP = settings.repeat_tool_detection.global_stop
REPEAT_TOOL_REMEDIATION_MAX_PER_MINUTE = settings.repeat_tool_detection.remediation_max_per_minute

# --- skill evolution (Phase 10) ---
EVOLUTION_ENABLED = settings.evolution.enabled
EVOLUTION_TRIGGER = settings.evolution.trigger
EVOLUTION_AUTO_SAVE = settings.evolution.auto_save
EVOLUTION_MAX_TEXT_RECORDS = settings.evolution.max_text_records
EVOLUTION_MAX_SCRIPT_RECORDS = settings.evolution.max_script_records
EVOLUTION_SCORING_W_E = settings.evolution.scoring.w_effectiveness
EVOLUTION_SCORING_W_U = settings.evolution.scoring.w_utilization
EVOLUTION_SCORING_W_F = settings.evolution.scoring.w_freshness
EVOLUTION_FRESHNESS_HALF_LIFE = settings.evolution.scoring.freshness_half_life_days
EVOLUTION_STALE_VERSION_PENALTY = settings.evolution.scoring.stale_version_penalty
EVOLUTION_DISTILL_MIN_SCORE = settings.evolution.distill.min_score
EVOLUTION_SIGNAL_FAILURE = settings.evolution.signals.execution_failure
EVOLUTION_SIGNAL_SCRIPT = settings.evolution.signals.script_artifact
EVOLUTION_SIGNAL_USER_INTENT = settings.evolution.signals.user_intent

# --- team (Phase 18) ---
TEAM_ENABLED = settings.team.enabled
