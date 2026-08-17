"""Span/metric attribute key constants.

Aligned with OpenTelemetry GenAI semantic conventions (gen_ai.*) plus
twinkle-specific dimensions (twinkle.*). Centralized so instrumentors
never hardcode string keys.
"""

# --- span names ---
SPAN_AGENT_INVOKE = "twinkle.agent.invoke"
SPAN_GEN_AI_CHAT = "gen_ai.chat"
SPAN_GEN_AI_TOOL = "gen_ai.tool"
SPAN_COMPRESSION = "twinkle.compression"
SPAN_SKILL_EVOLUTION = "twinkle.skill.evolution"
SPAN_MEMORY_FLUSH = "twinkle.memory.flush"

# --- gen_ai.* (OTel GenAI semconv) ---
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reason"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_STREAMING_FIRST_TOKEN_MS = "gen_ai.streaming.first_token_ms"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_ERROR = "gen_ai.tool.error"
GEN_AI_TOOL_ARGUMENTS = "gen_ai.tool.arguments"
GEN_AI_TOOL_RESULT = "gen_ai.tool.result"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"

# --- twinkle.* (custom) ---
TWINKLE_REQUEST_ID = "twinkle.request.id"
TWINKLE_SESSION_ID = "twinkle.session.id"
TWINKLE_AGENT_ITERATIONS = "twinkle.agent.iterations"
TWINKLE_AGENT_STATUS = "twinkle.agent.status"
# --- compression ---
TWINKLE_COMPRESSION_TOKENS_BEFORE = "twinkle.compression.tokens_before"
TWINKLE_COMPRESSION_TOKENS_AFTER = "twinkle.compression.tokens_after"
TWINKLE_COMPRESSION_COMPRESSED = "twinkle.compression.compressed"
TWINKLE_COMPRESSION_HAS_SUMMARY = "twinkle.compression.has_summary"
TWINKLE_COMPRESSION_STRATEGY = "twinkle.compression.strategy"
# --- memory flush ---
TWINKLE_MEMORY_FLUSH_NEW_WRITES = "twinkle.memory.flush.new_writes"
TWINKLE_MEMORY_FLUSH_ERRORS = "twinkle.memory.flush.errors"
# --- skill / evolution ---
TWINKLE_SKILL_NAME = "twinkle.skill.name"
TWINKLE_EVOLUTION_STATUS = "twinkle.evolution.status"
TWINKLE_EVOLUTION_MESSAGE = "twinkle.evolution.message"

# --- metric names ---
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_TOOL_COUNT = "gen_ai.tool.count"
METRIC_LLM_DURATION = "gen_ai.client.operation.duration"
METRIC_TOOL_DURATION = "gen_ai.tool.duration"
METRIC_AGENT_DURATION = "twinkle.agent.duration"

# --- misc ---
TOOL_ERROR_PREFIX = "[tool error]"
