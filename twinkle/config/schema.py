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


class AgentConfig(_StrictModel):
    max_steps: int = 1000


class ContextCompressionConfig(_StrictModel):
    token_threshold: int = 60000
    keep_recent_pairs: int = 6
    summary_prompt: str = (
        "你是对话上下文压缩器。把给定历史对话压成一段摘要，保留关键事实、用户偏好、"
        "已做决策、工具调用结果，丢弃寒暄与冗余。用中文。"
    )


class SkillsConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/skills
    mode: SkillMode = "all"
    enabled: list[str] = []  # [] = all skills open


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


class MemoryConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/memory
    embed_model: str = "text-embedding-3-small"
    query: MemoryQueryConfig = MemoryQueryConfig()
    hybrid: MemoryHybridConfig = MemoryHybridConfig()
    chunking: MemoryChunkingConfig = MemoryChunkingConfig()
    cleanup: MemoryCleanupConfig = MemoryCleanupConfig()


class PermissionsConfig(_StrictModel):
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
