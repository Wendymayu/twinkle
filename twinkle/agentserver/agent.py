"""ReActAgent — a ReAct-pattern agent: think -> (tool -> result)* -> answer.

run() is an async generator yielding E2AResponse frames so the ws send
boundary stays in server.py (agent never touches the socket).

Twinkle is stream-only; unary has been removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import sys
import time
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    TODO_EVENTS,
    flush_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.permission_context import set_permission_channel
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY, ApprovalPendingRecord
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder
from twinkle.e2a.models import E2AResponse
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    MEMORY_DIR,
    SKILLS_DIR,
    WORKSPACE_DIR,
)

log = logging.getLogger("twinkle.agentserver")


# ---------------------------------------------------------------------------
# AgentRequest — pure business input, no transport-layer concepts
# ---------------------------------------------------------------------------

@dataclass
class AgentRequest:
    """One agent run's business inputs. No E2A / WebSocket concepts.

    server.py is responsible for constructing this from the transport envelope.
    """

    session_id: str
    request_id: str
    query: str
    channel: str = "web"
    mode: str = ""  # "" = normal, "team" = team collaboration


class _Inbox(Protocol):
    """信箱协议:member 信箱有 drain()→list[str]。MessageBox 实现。

    agent.py 不 import team 包(避免循环);leader 传 None,member 传 MessageBox。
    """
    def drain(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Build the base system prompt injected once per session at position 0.

    Includes agent identity, runtime environment, workspace overview, and
    tool usage guidance. Dynamic values (platform, date, workspace paths)
    are resolved at injection time so the prompt stays current.
    """
    os_type = sys.platform
    workspace = WORKSPACE_DIR
    memory_dir = MEMORY_DIR
    skills_dir = SKILLS_DIR

    # Windows-specific mkdir warning
    mkdir_warning = ""
    if os_type.startswith("win"):
        mkdir_warning = (
            "\n⚠️ Windows `mkdir` 不支持 `-p`！创建嵌套目录请用 "
            "PowerShell `New-Item -ItemType Directory -Path \"parent/child\" -Force` "
            "或 cmd 分步 `mkdir parent && mkdir parent\\child`。"
        )

    prompt = f"""# 身份与行为原则

对外交流时，不要主动提及内部框架名、目录名或运行细节。

- **直接进入正题** — 少说"好的""很乐意"，直接开始做事。
- **先想再做** — 接到任务后先理清思路，想清楚步骤再执行。
- **办事严谨** — 做事牢靠是获得信任的关键。
- **尽量不拒绝** — 尽量满足合理请求，仅在涉及违法、有害或超出能力时才拒绝并说明原因。
- **简洁输出** — 不要重复表达相同的意思，每个想法只说一次。

# 运行环境

**必须严格使用与当前平台匹配的命令语法**，切勿混用其他平台命令。常见差异：

| 操作 | Windows | Linux/macOS |
|------|---------|-------------|
| 创建目录 | `mkdir folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` | `cat file.txt` |
| 列出文件 | `dir` | `ls -la` |
| 删除目录 | `rmdir folder` | `rm -rf folder` |{mkdir_warning}

# 工作区

以下目录仅供执行任务时内部参考，不要主动向用户展示内部路径。

| 路径 | 用途 |
|------|------|
| `{workspace}` | 工作区根目录，文件操作默认收敛于此 |
| `{memory_dir}` | 长期记忆存储 |
| `{skills_dir}` | 技能库 |

# 工具使用指南

## Todo（任务规划）

你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed", result=...) 标记完成，调 todo_list 查看进度，调 todo_get 查看单任务详情。
- 简单单步请求：直接回答或调工具，不要使用 todo。

"""

    return prompt


def build_agent_runtime_prompt() -> str:
    """Build a lean runtime-only prompt for team members.

    Members get team identity from build_member_system_prompt(); this
    supplies only the execution environment they share with every agent:
    platform, date, command syntax, and tool usage guidance.

    Omits the user-facing identity/behavior section and the global
    workspace paths — members see team workspace via their team section.
    """
    os_type = sys.platform

    mkdir_warning = ""
    if os_type.startswith("win"):
        mkdir_warning = (
            "\n⚠️ Windows `mkdir` 不支持 `-p`！创建嵌套目录请用 "
            "PowerShell `New-Item -ItemType Directory -Path \"parent/child\" -Force` "
            "或 cmd 分步 `mkdir parent && mkdir parent\\child`。"
        )

    return f"""# 运行环境

**必须严格使用与当前平台匹配的命令语法**，切勿混用其他平台命令。常见差异：

| 操作 | Windows | Linux/macOS |
|------|---------|-------------|
| 创建目录 | `mkdir folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` | `cat file.txt` |
| 列出文件 | `dir` | `ls -la` |
| 删除目录 | `rmdir folder` | `rm -rf folder` |{mkdir_warning}

# 工具使用指南

## Todo（任务规划）

你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed", result=...) 标记完成，调 todo_list 查看进度，调 todo_get 查看单任务详情。
- 简单单步请求：直接回答或调工具，不要使用 todo。

"""


# ── Team leader prompt (injected per-request when mode=team) ──────

def build_leader_system_prompt() -> str:
    """Build the leader's system prompt for team mode.

    Replaces build_system_prompt() when mode=team. The leader is a
    coordinator — identity and workflow are team-specific, not the
    generic user-facing rules in the base prompt.

    Aligned with jiuwenswarm's TeamRail sections:
      P:11  team_role     — leader_policy (who you are)
      P:13  team_workflow — leader_workflow (how you work)
      then   runtime + tool guidance
    """
    os_type = sys.platform

    mkdir_warning = ""
    if os_type.startswith("win"):
        mkdir_warning = (
            "\n⚠️ Windows `mkdir` 不支持 `-p`！创建嵌套目录请用 "
            "PowerShell `New-Item -ItemType Directory -Path \"parent/child\" -Force` "
            "或 cmd 分步 `mkdir parent && mkdir parent\\child`。"
        )

    return f"""# 团队角色

你是 TeamLeader，负责规划、委派和整合。你的价值在于**定义"做什么"和"为什么做"**，而非亲自执行。团队成员是独立 agent，看不到你的对话历史，委派时需提供充分上下文。

## 核心职责
1. **目标拆解**: 分析用户需求，拆解为可委派的子任务。用 todo 工具规划，用 delegate_to_member 执行
2. **组建团队**: 通过 persona 参数为每个成员定义角色和专长（如"金融分析师，专长美股财报"），让成员获得匹配能力
3. **质量把关**: 审查成员返回的结果，必要时追加委派或要求修正
4. **整合交付**: 将所有成员产出整合为连贯的最终结果

## 决策原则
- **你只协调，不执行**: 把实质性工作（命令执行、文件写入、数据分析）委派给成员。你只用只读工具来了解上下文
- **并行优先**: 无依赖的子任务并发委派给多个成员
- **信任成员**: 成员是领域专家，Leader 定义目标、成员决定方法
- **委派后不催促**: 成员需要时间执行，等待结果返回后再审查

## 工作流程
1. 分析需求 → 用 create_task 拆解子任务入队(可设 blocked_by 依赖)
2. 委派成员 → delegate_to_member(member_name="成员名", persona="角色", objective="认领并执行 queue 中你能做的 task") 启动成员
3. 监控进度 → list_tasks 查队列状态,get_task 看单个 task 详情(含 help_reason 求助标记)
4. 调整方向 → send_member(member_name, message) 向运行中成员发 steer(非阻塞,只调方向不派任务)
5. 审查结果 → 成员返回后检查质量,不满足则追加委派或 cancel_task 重派
6. 整合输出 → 汇总所有结果,向用户交付最终答案

# 运行环境

**必须严格使用与当前平台匹配的命令语法**，切勿混用其他平台命令。常见差异：

| 操作 | Windows | Linux/macOS |
|------|---------|-------------|
| 创建目录 | `mkdir folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` | `cat file.txt` |
| 列出文件 | `dir` | `ls -la` |
| 删除目录 | `rmdir folder` | `rm -rf folder` |{mkdir_warning}

# 工具使用指南

## 核心工具

delegate_to_member(member_name, persona, objective, prompt) 启动一个团队成员执行任务。第一次委派某 member_name 会创建该成员。
- **member_name**: 成员名(简短英文,如 researcher),稳定可读,用于 task owner/消息寻址
- **persona**: 成员角色描述,越具体成员能力越匹配
- **objective**: 任务目标,一句话说清要产出什么。主路径用"认领并执行 queue 中你能做的 task"
- **prompt**: 可选,补充上下文

create_task(subject, blocked_by) 创建共享任务入队;成员通过 claim_task 认领、complete_task 完成。
send_member(member_name, message) 向运行中成员发 steer(非阻塞,调整方向用,不派任务——任务走 create_task)。
list_tasks / get_task 查队列与详情(含 help_reason 求助标记,成员遇困时会标)。

## 你可以直接使用的工具

**协调类**: delegate_to_member、create_task、cancel_task、list_tasks、get_task、send_member — team 编排
**规划类**: todo_create、todo_update、todo_list、todo_get — 个人规划追踪
**只读类**: read_file、list_files、glob、web_search、web_fetch — 了解上下文
**查询类**: memory_search、read_memory、list_skill、read_skill、cron_list_jobs

## Todo（任务规划）

你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed", result=...) 标记完成，调 todo_list 查看进度，调 todo_get 查看单任务详情。
- 简单单步请求：直接委派或调工具，不要使用 todo。"""


# ── Member system prompt (aligned with jiuwenswarm sections) ────

def build_member_system_prompt(*, persona: str, workspace: str,
                               member_name: str = "") -> str:
    """Build a member's system prompt with team identity front and center.

    Aligned with jiuwenswarm's section model:
      P:11  team_role     — role policy + member identity
      P:15  team_persona  — persona description
      P:30  team_info     — workspace path

    Followed by a lean runtime prompt (platform, date, tool usage) —
    NOT the full user-facing build_system_prompt(). Members don't need
    user-facing identity/behavior rules or global workspace paths.
    """
    name_line = f"（成员名: `{member_name}`）" if member_name else ""
    return f"""# 团队角色

你是 Teammate{name_line}，{persona}

作为团队成员：
- Leader 定义"做什么"，你来决定"怎么做"
- 聚焦任务目标，自主搜索、执行、产出
- 从 queue 认领 task（claim_task）、完成后调 complete_task 回报结果；遇困难用 complete_task(help_reason=...) 求助 Leader
- 任务完成后给出清晰总结，让 Leader 能直接整合

# 当前人设

{persona}

# 团队共享工作区

路径: `{workspace}`
所有文件读写操作在此目录内进行。

---

{build_agent_runtime_prompt()}"""


# ── base_sections 工厂(loop 每步注入 builder;member/subagent 构造时带 persona) ──

def normal_base_sections() -> list[PromptSection]:
    """Normal-mode base sections for the generic agent path."""
    return [PromptSection("system_prompt", build_system_prompt(), priority=10)]


def leader_base_sections() -> list[PromptSection]:
    """Team-leader base sections (mode=team)."""
    return [PromptSection("system_prompt", build_leader_system_prompt(), priority=10)]


def member_base_sections(*, persona: str, workspace: str,
                         member_name: str = "") -> list[PromptSection]:
    """Team-member base sections — persona baked at construction time."""
    return [PromptSection("system_prompt",
                           build_member_system_prompt(persona=persona, workspace=workspace,
                                                      member_name=member_name),
                           priority=10)]


# ── Leader tool whitelist for team mode ──────────────────────────
# In team mode the leader is a COORDINATOR: plan, delegate, review.
# Execution tools (command_exec, write_file, edit_file) are reserved
# for members so delegation is not optional — the leader MUST delegate
# substantive work. This is the architectural difference from subagent
# mode: the leader cannot do the work itself.

_TEAM_LEADER_TOOL_WHITELIST: frozenset[str] = frozenset({
    # Coordination
    "delegate_to_member",
    "create_task", "cancel_task", "list_tasks", "get_task",   # NEW: team task 编排
    "send_member",                                            # NEW: leader→member steer
    # Planning & tracking
    "todo_create", "todo_update", "todo_list", "todo_get",
    # Read-only inspection
    "read_file", "list_files", "glob",
    "web_search", "web_fetch",
    "memory_search", "read_memory",
    # Skills (read-only)
    "list_skill", "read_skill",
    # Cron (read-only management)
    "cron_list_jobs",
})


_MAX_HOOK_RETRIES = 3


# ---------------------------------------------------------------------------
# ReActAgent
# ---------------------------------------------------------------------------

class ReActAgent:
    """A ReAct-pattern agent: LLM think → tool calls → results → re-decide.

    Hooks are injected at construction time via *hooks*.  ``run()`` is the
    single public entry point — it processes one user message through the
    ReAct loop and yields E2AResponse frames.
    """

    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        *,
        hooks: tuple[AgentHook, ...] = (),
        max_steps: int | None = None,
        inbox: _Inbox | None = None,
        base_sections: list[PromptSection] | None = None,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        for h in hooks:
            self._hook_manager.register_hook(h)
        self._max_steps = max_steps if max_steps is not None else MAX_STEPS
        self._inbox = inbox
        self._base_sections = base_sections  # None → normal/leader by mode; list → member/subagent

    @property
    def session_store(self) -> SessionStore:
        """The SessionStore this agent reads/writes conversation history from."""
        return self._session_store

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook (kept for test injection — prefer constructor)."""
        self._hook_manager.register_hook(hook_instance)

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook."""
        self._hook_manager.unregister_hook(hook_instance)

    # -- Public entry point -------------------------------------------------

    async def run(self, request: AgentRequest) -> AsyncIterator[E2AResponse]:
        """Process one user message through the ReAct loop.

        Triggers BEFORE_INVOKE / AFTER_INVOKE hooks, delegates to the
        internal ReAct loop, writes interrupt snapshots on failure.
        """
        session_id = request.session_id
        request_id = request.request_id

        ctx = HookContext(
            agent=self,
            event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query=request.query, mode=request.mode),
            session_id=session_id,
            request_id=request_id,
            extra={},
        )

        await self._hook_manager.execute(HookEvent.BEFORE_INVOKE, ctx)

        completed_normally = False
        try:
            async for frame in self._run_react_loop(ctx, request):
                yield frame
            completed_normally = True
        except HookInterrupt:
            completed_normally = True
            yield E2AResponse(
                request_id=request_id,
                sequence=0,
                is_final=True,
                status="failed",
                response_kind="e2a.error",
                body={"error": "execution interrupted"},
            )
        except Exception as exc:
            ctx.exception = exc
            await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            if not completed_normally:
                try:
                    snapshot = await self._build_interrupt_snapshot(ctx, session_id)
                    await self._session_store.append(
                        session_id,
                        {"role": "assistant", "content": snapshot},
                        request_id=request_id,
                    )
                except asyncio.CancelledError:
                    log.warning("interrupt snapshot cancelled for session %s", session_id)
                except Exception:
                    log.exception("failed to write interrupt snapshot for session %s", session_id)
            APPROVAL_REGISTRY.clear_all_pending(session_id)
            await self._hook_manager.execute(HookEvent.AFTER_INVOKE, ctx)

    # -- ReAct loop ---------------------------------------------------------

    async def _run_react_loop(
        self,
        ctx: HookContext,
        request: AgentRequest,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop with hook trigger points.

        Model calls use manual self._hook_manager.execute() (async generator
        incompatible with @hook).  Tool calls use @hook-decorated _tool_call.
        """
        session_id = request.session_id
        request_id = request.request_id

        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
        set_permission_channel(request.channel)
        await self._fill_missing_tool_results(session_id, request_id)

        is_team_mode = request.mode == "team"

        await self._session_store.append(
            session_id,
            {"role": "user", "content": request.query},
            request_id=request_id,
        )

        seq = 0
        full_text = ""
        # 一次冻结 tool schemas:invoke 内不变;team 过滤只依赖 request.mode(before_invoke 时已知)。
        # 对齐 jiuwenswarm:tools 跨步稳定 → system prefix 字节稳定 → provider 自动 prefix cache 命中。
        tool_schemas = self._tool_manager.schemas()
        if is_team_mode:
            tool_schemas = [t for t in tool_schemas
                           if t["function"]["name"] in _TEAM_LEADER_TOOL_WHITELIST]
        for _step in range(self._max_steps):
            msgs = self._session_store.get_messages(session_id)
            if self._inbox is not None:
                new_messages = self._inbox.drain()
                if new_messages:
                    msgs = list(msgs) + [{"role": "user", "content": m} for m in new_messages]

            # -- BEFORE_MODEL_CALL -- #
            # 每步新建 builder + 注 base sections(normal/leader by mode,或构造时注入的 member/subagent)
            builder = SystemPromptBuilder()
            if self._base_sections is not None:
                base = self._base_sections
            elif is_team_mode:
                base = leader_base_sections()
            else:
                base = normal_base_sections()
            for sec in base:
                builder.add_section(sec)
            # per-invoke 冻结段(before_invoke hooks 如 SkillHook/MemoryHook 注入,跨步稳定)
            for sec in ctx.extra.get("frozen_sections", []):
                builder.add_section(sec)
            ctx.builder = builder

            ctx.inputs = ModelCallInputs(messages=msgs, tools=tool_schemas)
            await self._hook_manager.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

            # -- 注 builder.build() 为首条 system + env 尾部 UserMessage -- #
            ctx.inputs.messages = (
                [{"role": "system", "content": ctx.builder.build()}]
                + ctx.inputs.messages
            )
            env_entries = ctx.extra.pop("environment_context", None)
            if env_entries:
                env_text = "\n\n".join(e["content"] for e in env_entries)
                ctx.inputs.messages.append(
                    {"role": "user",
                     "content": f"<environment_context>\n{env_text}\n</environment_context>"})

            # Check force_finish
            force_finish = ctx.consume_force_finish_request()
            if force_finish is not None:
                yield E2AResponse(
                    request_id=request_id,
                    sequence=seq,
                    is_final=True,
                    status="succeeded",
                    response_kind="e2a.complete",
                    body={"result": {"content": str(force_finish.result or "")}},
                )
                return

            # -- LLM stream with retry loop -- #
            should_reask = False
            for retry_attempt in range(_MAX_HOOK_RETRIES + 1):
                ctx.retry_attempt = retry_attempt
                ctx.exception = None
                try:
                    async for stream_event in self._llm.stream(messages=ctx.inputs.messages, tools=ctx.inputs.tools):
                        if isinstance(stream_event, TextDelta):
                            full_text += stream_event.content
                            yield E2AResponse(
                                request_id=request_id,
                                sequence=seq,
                                is_final=False,
                                status="in_progress",
                                response_kind="e2a.chunk",
                                body={"result": {"content": stream_event.content}},
                            )
                            seq += 1
                        elif isinstance(stream_event, Finish):
                            await self._session_store.append(
                                session_id,
                                stream_event.assistant_message,
                                request_id=request_id,
                                event_type="chat.final",
                            )
                            tool_calls = stream_event.assistant_message.get("tool_calls")
                            if stream_event.finish_reason == "tool_calls" and tool_calls:
                                if len(tool_calls) > 1:
                                    # --- Parallel path ---
                                    try:
                                        parallel_results, parallel_todos = await self._try_parallel_tool_calls(
                                            tool_calls, session_id, request_id,
                                        )
                                        for snap in parallel_todos:
                                            yield E2AResponse(
                                                request_id=request_id,
                                                sequence=seq, is_final=False,
                                                status="in_progress",
                                                response_kind="e2a.todo_update",
                                                body=snap,
                                            )
                                            seq += 1
                                        for tool_call_id, result in parallel_results:
                                            await self._session_store.append(
                                                session_id,
                                                {"role": "tool", "tool_call_id": tool_call_id, "content": result},
                                                request_id=request_id,
                                                event_type="chat.tool_result",
                                            )
                                        await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                        should_reask = True
                                        break
                                    except HookInterrupt:
                                        log.info("parallel tool calls fell back to sequential (HookInterrupt)")
                                if len(tool_calls) <= 1 or should_reask is False:
                                    # --- Sequential path ---
                                    for tool_call in tool_calls:
                                        name = tool_call["function"]["name"]
                                        try:
                                            args = json.loads(tool_call["function"]["arguments"] or "{}")
                                        except Exception:
                                            args = {}
                                        ctx.inputs = ToolCallInputs(
                                            name=name, args=args, tool_call_id=tool_call["id"]
                                        )
                                        try:
                                            result = await self._tool_call(ctx)
                                        except HookInterrupt as hook_interrupt:
                                            if "approval_id" not in hook_interrupt.data:
                                                yield E2AResponse(
                                                    request_id=request_id, sequence=seq, is_final=True,
                                                    status="failed", response_kind="e2a.error",
                                                    body={"error": "tool execution interrupted"})
                                                return
                                            # ASK: register Future + yield e2a.ask + suspend
                                            approval_id = hook_interrupt.data["approval_id"]
                                            future = APPROVAL_REGISTRY.register(approval_id)
                                            APPROVAL_REGISTRY.save_pending(session_id, ApprovalPendingRecord(
                                                approval_id=approval_id,
                                                tool=hook_interrupt.data["tool"],
                                                args=hook_interrupt.data["args"],
                                                tool_call_id=tool_call["id"],
                                                reason=hook_interrupt.data["reason"],
                                                request_id=request_id,
                                                session_id=session_id,
                                                created_at=time.time(),
                                            ))
                                            yield E2AResponse(
                                                request_id=request_id, sequence=seq, is_final=False,
                                                status="in_progress", response_kind="e2a.ask",
                                                body={"approval_id": approval_id, "tool": hook_interrupt.data["tool"],
                                                      "args": hook_interrupt.data["args"], "tool_call_id": tool_call["id"],
                                                      "reason": hook_interrupt.data["reason"]})
                                            seq += 1
                                            decision = await future  # SUSPEND
                                            if decision in ("allow", "allow_always"):
                                                ctx.extra["_approval_decision"] = decision
                                                ctx.extra.setdefault("_approved_tool_call_ids", set()).add(tool_call["id"])
                                                try:
                                                    result = await self._tool_call(ctx)
                                                except HookInterrupt:
                                                    raise
                                                except Exception as exc:
                                                    result = f"[tool error] {type(exc).__name__}: {exc}"
                                            else:
                                                result = (f"[tool denied by user: {hook_interrupt.data['tool']}] "
                                                          f"{hook_interrupt.data.get('reason', '')}")
                                        except Exception as exc:
                                            result = f"[tool error] {type(exc).__name__}: {exc}"
                                        for snap in flush_todo_events():
                                            yield E2AResponse(
                                                request_id=request_id,
                                                sequence=seq,
                                                is_final=False,
                                                status="in_progress",
                                                response_kind="e2a.todo_update",
                                                body=snap,
                                            )
                                            seq += 1
                                        await self._session_store.append(
                                            session_id,
                                            {"role": "tool", "tool_call_id": tool_call["id"], "content": result},
                                            request_id=request_id,
                                            event_type="chat.tool_result",
                                        )
                                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                should_reask = True
                                break
                            # Final answer
                            yield E2AResponse(
                                request_id=request_id,
                                sequence=seq,
                                is_final=True,
                                status="succeeded",
                                response_kind="e2a.complete",
                                body={"result": {"content": full_text}},
                            )
                            await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    if should_reask:
                        break
                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break
                except asyncio.CancelledError:
                    raise
                except HookInterrupt:
                    raise
                except Exception as exc:
                    ctx.exception = exc
                    await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    force_finish = ctx.consume_force_finish_request()
                    if force_finish is not None:
                        yield E2AResponse(
                            request_id=request_id,
                            sequence=seq,
                            is_final=True,
                            status="succeeded",
                            response_kind="e2a.complete",
                            body={"result": {"content": str(force_finish.result or "")}},
                        )
                        return
                    retry_request = ctx.consume_retry_request()
                    if retry_request is not None and retry_attempt < _MAX_HOOK_RETRIES:
                        if retry_request.delay > 0:
                            await asyncio.sleep(retry_request.delay)
                        log.info("hook requested LLM retry, attempt %d/%d",
                                 retry_attempt + 1, _MAX_HOOK_RETRIES)
                        continue
                    raise
            if should_reask:
                continue

        # exceeded max_steps
        yield E2AResponse(
            request_id=request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={self._max_steps}"},
        )

    # -- Parallel tool execution --------------------------------------------

    async def _try_parallel_tool_calls(
        self,
        tool_calls: list[dict],
        session_id: str,
        request_id: str,
    ) -> tuple[list[tuple[str, str]], list[dict]]:
        """Execute multiple tool calls concurrently via asyncio.gather.

        Each tool call gets its own HookContext (isolated inputs + extra) and
        its own TODO_EVENTS buffer so concurrent calls don't race on shared
        state.  Results are returned in the same order as *tcs*.
        """
        results_per_tool: list[tuple[str, str] | None] = [None] * len(tool_calls)
        todos_per_tool: list[list[dict]] = [[] for _ in range(len(tool_calls))]

        async def _execute_one_tool(idx: int, tool_call: dict) -> None:
            name = tool_call["function"]["name"]
            try:
                args = json.loads(tool_call["function"]["arguments"] or "{}")
            except Exception:
                args = {}

            todo_buffer: list[dict] = []
            TODO_EVENTS.set(todo_buffer)

            tool_call_context = HookContext(
                agent=self,
                event=HookEvent.BEFORE_TOOL_CALL,
                inputs=ToolCallInputs(name=name, args=args, tool_call_id=tool_call["id"]),
                session_id=session_id,
                request_id=request_id,
                extra={},
            )
            try:
                result = await self._tool_call(tool_call_context)
            except HookInterrupt:
                raise
            except Exception as exc:
                result = f"[tool error] {type(exc).__name__}: {exc}"

            results_per_tool[idx] = (tool_call["id"], result)
            todos_per_tool[idx] = list(todo_buffer)

        raw_results = await asyncio.gather(
            *[asyncio.create_task(_execute_one_tool(i, tool_call)) for i, tool_call in enumerate(tool_calls)],
            return_exceptions=True,
        )

        for result_item in raw_results:
            if isinstance(result_item, HookInterrupt):
                raise result_item

        results: list[tuple[str, str]] = []
        for result_item in results_per_tool:
            if result_item is not None:
                results.append(result_item)
        all_todos = [snap for todo_list in todos_per_tool for snap in todo_list]
        return results, all_todos

    # -- Interrupt snapshot -------------------------------------------------

    async def _build_interrupt_snapshot(self, ctx: HookContext, session_id: str) -> str:
        """Build an interrupt snapshot message from session history + TodoStore."""
        parts = ["[SYSTEM] 任务中断。"]

        if ctx.exception:
            exc_type = type(ctx.exception).__name__
            exc_msg = str(ctx.exception)
            if len(exc_msg) > 100:
                exc_msg = exc_msg[:100] + "..."
            parts.append(f"中断原因：{exc_type}: {exc_msg}")
        else:
            parts.append("中断原因：请求被取消")

        msgs = self._session_store.get_messages(session_id)
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                tools = [tc.get("function", {}).get("name", "unknown") for tc in m.get("tool_calls", [])]
                parts.append(f"中断前正在执行：{', '.join(tools)}")
                break

        try:
            from twinkle.agentserver.todo import get_todo_store
            todos = get_todo_store()
            tasks = await todos.list(session_id)
            if tasks:
                done = sum(1 for t in tasks if t.status == "completed")
                parts.append(f"任务进度：{done}/{len(tasks)} 已完成")
        except Exception:
            pass

        return " ".join(parts)

    # -- Orphan tool-result fill --------------------------------------------

    async def _fill_missing_tool_results(self, session_id: str, request_id: str) -> None:
        """Inject synthetic tool_result for orphan tool_calls from a crash."""
        msgs = self._session_store.get_messages(session_id)
        if not msgs:
            return
        last_assistant = None
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_assistant = m
                break
        if last_assistant is None:
            return

        pending = APPROVAL_REGISTRY.get_pending(session_id)
        pending_map = {p["tool_call_id"]: p for p in pending if p.get("tool_call_id")}

        for tc in last_assistant["tool_calls"]:
            tc_id = tc.get("id")
            if tc_id and not any(m.get("role") == "tool" and m.get("tool_call_id") == tc_id
                                for m in msgs):
                tool_name = tc.get("function", {}).get("name", "unknown")
                tool_args = tc.get("function", {}).get("arguments", "")
                parts = [f"[interrupted: {tool_name} was interrupted, result unknown."]
                if tool_args:
                    args_preview = tool_args[:200] + ("..." if len(tool_args) > 200 else "")
                    parts.append(f"Args: {args_preview}.")
                approval = pending_map.get(tc_id)
                if approval:
                    parts.append(f"Approval was pending (reason: {approval.get('reason', 'unknown')}).")
                parts.append("]")
                content = " ".join(parts)
                await self._session_store.append(
                    session_id,
                    {"role": "tool", "tool_call_id": tc_id, "content": content},
                    request_id=request_id)

    # -- @hook-decorated tool call ------------------------------------------

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _tool_call(self, ctx: HookContext) -> str:
        """Tool execution wrapped with @hook lifecycle."""
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        return await self._tool_manager.execute(inputs.name, inputs.args)
