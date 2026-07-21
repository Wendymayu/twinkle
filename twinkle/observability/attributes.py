"""Span/metric attribute key constants.

Aligned with OpenTelemetry GenAI semantic conventions (gen_ai.*) plus
twinkle-specific dimensions (twinkle.*). Centralized so instrumentors
never hardcode string keys.
"""

# --- span names ---
SPAN_AGENT_INVOKE = "twinkle.agent.invoke"
SPAN_GEN_AI_CHAT = "gen_ai.chat"
SPAN_GEN_AI_TOOL = "gen_ai.tool"

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

# --- metric names ---
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_TOOL_COUNT = "gen_ai.tool.count"
METRIC_LLM_DURATION = "gen_ai.client.operation.duration"
METRIC_TOOL_DURATION = "gen_ai.tool.duration"
METRIC_AGENT_DURATION = "twinkle.agent.duration"

# --- misc ---
TOOL_ERROR_PREFIX = "[tool error]"
