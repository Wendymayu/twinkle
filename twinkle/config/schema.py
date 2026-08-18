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

from pydantic import BaseModel, ConfigDict, model_validator

SkillMode = Literal["all", "auto_list"]
PermissionTier = Literal["allow", "require-approval", "deny"]


class _StrictModel(BaseModel):
    """Base: reject unknown keys so a YAML typo (e.g. `permission:` vs `permissions:`,
    or `enbaled:`) fails loudly at startup instead of silently disabling a subsystem."""
    model_config = ConfigDict(extra="forbid")


class AgentserverConfig(_StrictModel):
    host: str = "127.0.0.1"
    port: int = 18000


class GatewayConfig(_StrictModel):
    host: str = "127.0.0.1"
    port: int = 19000


class WorkspaceConfig(_StrictModel):
    dir: str = ""  # "" -> ~/.twinkle


class LoggingConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/logs


class SessionsConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/sessions


class TodosConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/todos


class LLMConfig(_StrictModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: float = 120.0  # per-chunk read timeout (s); hung model -> APITimeoutError


class AgentConfig(_StrictModel):
    max_steps: int = 1000


class MicroCompactConfig(_StrictModel):
    trigger_threshold: int = 5           # 可清条数(总数-keep) > trigger 才清
    keep_recent_per_tool: int = 3       # 每工具留最近 N 条原文
    compactable_tool_names: list[str] = [
        "read_file", "glob", "command_exec", "web_fetch", "web_search"]
    cleared_marker: str = "[Old tool result content cleared]"


class ToolResultBudgetConfig(_StrictModel):
    tokens_threshold: int = 9000        # 所有 tool 结果总量超此才触发
    large_message_threshold: int = 3000  # 单条估算 token(char//3) 超此才 eligible
    trim_size: int = 3000               # 预览保留字符数
    protect_latest: int = 1             # 最新 N 条 tool result 永不 offload


class ContextCompressionConfig(_StrictModel):
    token_threshold: int = 60000
    keep_recent_pairs: int = 6
    summary_prompt: str = (
        "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、"
        "已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
    )
    summary_prompt_mode: Literal["structured", "free"] = "structured"
    micro_compact: MicroCompactConfig = MicroCompactConfig()
    tool_result_budget: ToolResultBudgetConfig = ToolResultBudgetConfig()


class SkillsConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/skills
    mode: SkillMode = "all"
    enabled: list[str] = []  # [] = all skills open
    skillnet_api_url: str = "http://api-skillnet.openkg.cn"  # SkillNet 公开搜索服务(关键词搜索)
    skillhub_api_url: str = "https://api.skillhub.cn"  # SkillHub 公开列表/下载 API(关键词搜索 + zip 下载)
    github_token: str = ""  # "" = anonymous (60/hour) — GitHub 下载用
    remote_timeout: float = 60.0
    remote_max_retries: int = 3


class MemoryQueryConfig(_StrictModel):
    max_results: int = 10


class MemoryHybridConfig(_StrictModel):
    vector_weight: float = 0.7
    text_weight: float = 0.3
    candidate_multiplier: float = 2.0


class MemoryChunkingConfig(_StrictModel):
    tokens: int = 256
    overlap: int = 32


class MemoryCleanupConfig(_StrictModel):
    max_chunks_per_file: int = 200


class MemoryIndexConfig(_StrictModel):
    debounce_seconds: float = 2.0  # 写后去抖窗口:连续写塌成一次重索引(对齐 jiuwenswarm watchDebounceMs)


class MemoryAutoInjectConfig(_StrictModel):
    enabled: bool = True     # 被动召回开关（默认开=before_invoke 注 USER.md+MEMORY.md 进 prefix；关=只策略 prompt）
    max_chars_user: int = 4000     # USER.md 注入上限（画像小而稳；对齐 openclaw USER_BOOTSTRAP_MAX_CHARS）
    max_chars_memory: int = 12000 # MEMORY.md 注入上限（累积会膨胀；大预算）。超限各自 head+tail 截断（保首尾丢中间）


class MemoryFlushConfig(_StrictModel):
    enabled: bool = False    # 兜底开关（opt-in；默认关=维持 5a）
    # prompt 硬编码在 memory_flush_hook._FLUSH_PROMPT：带 JSON 输出契约，进 config 会被用户改坏→静默失效


class MemoryDreamingConfig(_StrictModel):
    enabled: bool = True            # 后台整理开关（默认开：盘上 MEMORY.md 周期 compact 兜底容量；无 LLM 仍 no-op）
    interval_seconds: int = 3600   # 整理周期秒
    start_delay_seconds: int = 300  # 启动后首跑延迟秒
    top_k: int = 5                  # 聚类相似召回数
    min_distinct_files: int = 2     # 晋升门：同一事实须出现在 ≥N 个不同 daily 文件才搬进 MEMORY.md
    max_memory_chars: int = 10000   # MEMORY.md 容量预算，超限 compact 丢最老提升行
    max_delete_fraction: float = 0.25  # 整合步单次删除行数上限比例（安全阀，防 LLM 误删）
    # prompt 同 flush：硬编码进 dreaming.py（JSON 契约，不进 config）；见 docs/design/dreaming-redesign.md §9


class MemoryConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/memory
    embed_model: str = "text-embedding-3-small"
    query: MemoryQueryConfig = MemoryQueryConfig()
    hybrid: MemoryHybridConfig = MemoryHybridConfig()
    chunking: MemoryChunkingConfig = MemoryChunkingConfig()
    cleanup: MemoryCleanupConfig = MemoryCleanupConfig()
    index: MemoryIndexConfig = MemoryIndexConfig()
    auto_inject: MemoryAutoInjectConfig = MemoryAutoInjectConfig()
    flush: MemoryFlushConfig = MemoryFlushConfig()
    dreaming: MemoryDreamingConfig = MemoryDreamingConfig()


class PermissionsConfig(_StrictModel):
    enabled: bool = False
    enabled_channels: list[str] = ["web"]
    global_default: PermissionTier = "allow"
    tools: dict[str, PermissionTier] = {
        "command_exec": "require-approval",
        "web_fetch": "allow",
        "web_search": "allow",
        "todo_create": "allow",
        "todo_update": "allow",
        "todo_list": "allow",
        "todo_get": "allow",
        "memory_search": "allow",
        "write_memory": "allow",
        "read_memory": "allow",
        "edit_memory": "allow",
    }
    rules: list[dict] = []  # jiuwenswarm rules[] shape; v1 unvalidated internals
    approval_overrides: dict = {}
    overrides_file: str = ""  # "" -> <workspace>/.twinkle_data/permission_overrides.json
    audit_file: str = ""  # "" -> <logging.dir>/audit/permission_audit.jsonl


class SubagentConfig(_StrictModel):
    max_steps: int = 50                 # child ReAct cap (tighter than agent.max_steps=1000)
    hard_timeout: float = 300.0         # absolute cap (asyncio.wait_for on the whole child run)
    soft_timeout: float = 120.0         # no-streaming-activity reset
    abort_timeout: float = 30.0          # cancel-a-stuck-child window
    child_permissions: bool = False      # v1 MUST be false (true needs streaming -> startup reject)
    max_result_chars: int = 8000         # truncate child final to protect parent context
    list_sessions_filter: bool = True    # hide __sub_ sessions from session.list

    @model_validator(mode="after")
    def _reject_child_permissions_v1(self) -> "SubagentConfig":
        if self.child_permissions:
            raise ValueError(
                "subagent.child_permissions=true requires streaming-forward (not in v1); "
                "the child loop must not run PermissionHook or it deadlocks. Leave it false."
            )
        return self


class OverflowRecoveryConfig(_StrictModel):
    max_recovery_attempts: int = 3          # consecutive overflow recovery max attempts
    threshold_ratio: float = 0.85           # target ratio of model window after recovery
    aggressive_keep_recent: int = 3         # keep_recent_pairs reduced to this on overflow
    context_window_limit_tokens: int = 0    # 0 = parse from 413 error; >0 = manual override


class EvolutionScoringConfig(_StrictModel):
    w_effectiveness: float = 0.5
    w_utilization: float = 0.3
    w_freshness: float = 0.2
    freshness_half_life_days: int = 90
    stale_version_penalty: float = 0.7


class EvolutionDistillConfig(_StrictModel):
    min_score: float = 0.4


class EvolutionSignalsConfig(_StrictModel):
    execution_failure: bool = True
    script_artifact: bool = True
    user_intent: bool = False


class EvolutionConfig(_StrictModel):
    enabled: bool = False
    trigger: Literal["after_invoke", "after_tool_call", "after_model_call", "none"] = "after_invoke"
    auto_save: bool = False
    max_text_records: int = 2
    max_script_records: int = 1
    scoring: EvolutionScoringConfig = EvolutionScoringConfig()
    distill: EvolutionDistillConfig = EvolutionDistillConfig()
    signals: EvolutionSignalsConfig = EvolutionSignalsConfig()


class RepeatToolDetectionConfig(_StrictModel):
    history_size: int = 30                  # sliding window size
    repeat_warn: int = 10                   # LOW threshold
    pingpong_warn: int = 10                 # MEDIUM threshold
    loop_block: int = 20                    # HIGH threshold
    global_stop: int = 30                   # CRITICAL threshold
    remediation_max_per_minute: int = 5     # remediation injection rate limit


class WorkflowConfig(_StrictModel):
    execution_timeout: float = 300.0
    max_fallback_count: int = 3
    enable_fallback: bool = True


class TeamConfig(_StrictModel):
    enabled: bool = False


class TwinkleConfig(_StrictModel):
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
    memory: MemoryConfig = MemoryConfig()
    permissions: PermissionsConfig = PermissionsConfig()
    subagent: SubagentConfig = SubagentConfig()
    overflow_recovery: OverflowRecoveryConfig = OverflowRecoveryConfig()
    repeat_tool_detection: RepeatToolDetectionConfig = RepeatToolDetectionConfig()
    workflow: WorkflowConfig = WorkflowConfig()
    evolution: EvolutionConfig = EvolutionConfig()
    team: TeamConfig = TeamConfig()

    @model_validator(mode="after")
    def _derive_paths(self) -> "TwinkleConfig":
        # workspace first — everything else hangs off it.
        ws = self.workspace.dir or str(Path.home() / ".twinkle")
        ws = os.path.expanduser(ws)
        self.workspace.dir = ws
        # explicit user paths get ~ expanded too; empty ones derive from workspace.
        if not self.logging.dir:
            self.logging.dir = str(Path(ws) / "logs")
        else:
            self.logging.dir = os.path.expanduser(self.logging.dir)
        if not self.sessions.dir:
            self.sessions.dir = str(Path(ws) / ".twinkle_data" / "sessions")
        else:
            self.sessions.dir = os.path.expanduser(self.sessions.dir)
        if not self.todos.dir:
            self.todos.dir = str(Path(ws) / ".twinkle_data" / "todos")
        else:
            self.todos.dir = os.path.expanduser(self.todos.dir)
        if not self.skills.dir:
            self.skills.dir = str(Path(ws) / "skills")
        else:
            self.skills.dir = os.path.expanduser(self.skills.dir)
        if not self.memory.dir:
            self.memory.dir = str(Path(ws) / ".twinkle_data" / "memory")
        else:
            self.memory.dir = os.path.expanduser(self.memory.dir)
        if not self.permissions.overrides_file:
            self.permissions.overrides_file = str(
                Path(ws) / ".twinkle_data" / "permission_overrides.json")
        else:
            self.permissions.overrides_file = os.path.expanduser(
                self.permissions.overrides_file)
        if not self.permissions.audit_file:
            self.permissions.audit_file = str(
                Path(self.logging.dir) / "audit" / "permission_audit.jsonl")
        else:
            self.permissions.audit_file = os.path.expanduser(
                self.permissions.audit_file)
        return self
