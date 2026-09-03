"""运行时 configuration —— 从 resources/config.yaml 加载。

YAML（twinkle/resources/config.yaml）是面向用户的真相源：sections + 注释 +
Literal 取值域，secrets/deploy 变量用 ${ENV:-default}，可调项用字面量。
loader（config.loader）读取 + 解析 + 处理它；schema（config.schema）校验它
（错误的 tier/mode -> 启动报错）。本包的 __init__ 把已校验的 `settings`
摊平为代码库其余部分已在导入的同一批模块级常量
（`from twinkle.config import X`），故消费方无需改动。

镜像 jiuwenswarm/resources/config.yaml。observability 仍读自己的 OTEL_* env
（observability/config.py）——此处不并入（v1）。Workspace 引导
（ensure_workspace_dir）位于 twinkle/workspace.py。
"""
from .loader import load_config
from .schema import TwinkleConfig

settings: TwinkleConfig = load_config()

# --- agentserver / gateway ---
AGENTSERVER_HOST = settings.agentserver.host
AGENTSERVER_PORT = settings.agentserver.port
GATEWAY_HOST = settings.gateway.host
GATEWAY_PORT = settings.gateway.port

# --- workspace + 派生目录（sandbox + 持久化根）---
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
MEMORY_INDEX_DEBOUNCE_SECONDS = settings.memory.index.debounce_seconds
MEMORY_WATCH_INTERVAL_SECONDS = settings.memory.index.watch_interval_seconds
MEMORY_AUTO_INJECT_ENABLED = settings.memory.auto_inject.enabled
MEMORY_AUTO_INJECT_MAX_CHARS_USER = settings.memory.auto_inject.max_chars_user
MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY = settings.memory.auto_inject.max_chars_memory
MEMORY_FLUSH_ENABLED = settings.memory.flush.enabled
MEMORY_DREAMING_ENABLED = settings.memory.dreaming.enabled
MEMORY_DREAMING_INTERVAL_SECONDS = settings.memory.dreaming.interval_seconds
MEMORY_DREAMING_START_DELAY_SECONDS = settings.memory.dreaming.start_delay_seconds
MEMORY_DREAMING_MIN_DISTINCT_FILES = settings.memory.dreaming.min_distinct_files
MEMORY_DREAMING_MAX_MEMORY_CHARS = settings.memory.dreaming.max_memory_chars
MEMORY_DREAMING_MAX_DELETE_FRACTION = settings.memory.dreaming.max_delete_fraction
MEMORY_DREAMING_MAX_INFECTIOUS_FRACTION = settings.memory.dreaming.max_infectious_fraction

# --- LLM（OpenAI 兼容）---
LLM_BASE_URL = settings.llm.base_url
LLM_API_KEY = settings.llm.api_key
LLM_MODEL = settings.llm.model
LLM_TIMEOUT = settings.llm.timeout

# --- agent loop ---
AGENT_MAX_STEPS = settings.agent.max_steps

# --- context compression (Phase 3) ---
CONTEXT_TOKEN_THRESHOLD = settings.context_compression.token_threshold
CONTEXT_TRIGGER_RATIO = settings.context_compression.trigger_ratio  # A/B 共用窗口比例
CONTEXT_KEEP_RECENT_PAIRS = settings.context_compression.keep_recent_pairs
CONTEXT_SUMMARY_PROMPT = settings.context_compression.summary_prompt
CONTEXT_SUMMARY_PROMPT_MODE = settings.context_compression.summary_prompt_mode
MICRO_COMPACT_TRIGGER_THRESHOLD = settings.context_compression.micro_compact.trigger_threshold
MICRO_COMPACT_KEEP_RECENT_PER_TOOL = settings.context_compression.micro_compact.keep_recent_per_tool
MICRO_COMPACT_COMPACTABLE_TOOL_NAMES = list(settings.context_compression.micro_compact.compactable_tool_names)
MICRO_COMPACT_CLEARED_MARKER = settings.context_compression.micro_compact.cleared_marker
TOOL_RESULT_BUDGET_TOKENS_THRESHOLD = settings.context_compression.tool_result_budget.tokens_threshold
TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD = settings.context_compression.tool_result_budget.large_message_threshold
TOOL_RESULT_BUDGET_TRIM_SIZE = settings.context_compression.tool_result_budget.trim_size
TOOL_RESULT_BUDGET_PROTECT_LATEST = settings.context_compression.tool_result_budget.protect_latest

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
SUBAGENT_SOFT_TIMEOUT = settings.subagent.soft_timeout
SUBAGENT_ABORT_TIMEOUT = settings.subagent.abort_timeout
SUBAGENT_MAX_RESULT_CHARS = settings.subagent.max_result_chars
SUBAGENT_LIST_SESSIONS_FILTER = settings.subagent.list_sessions_filter

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
