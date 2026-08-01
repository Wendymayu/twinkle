# 上下文压缩抽成 Hook 设计（轻量重构）

> 日期: 2026-08-01
> 关联: Phase 3 上下文压缩（`docs/superpowers/specs/2026-07-23-phase3-context-compression-design.md`）、Hook 设计（`docs/design/hook-design.md:467-478`）
> 状态: 已批准，待实现

## 1. 背景与动机

Phase 3 的上下文压缩当前实现为：

- 算法逻辑在独立模块 `twinkle/agentserver/context_compression.py`（`compress_messages` 等，全文 115 行）；
- **调用点硬编码内联在 `twinkle/agentserver/agent_loop.py:231-237`**，每个 ReAct 步骤在 `BEFORE_MODEL_CALL` 之前主动调用，估算 token 超 `token_threshold` 才压缩。

问题：压缩是"喂模型前整备 messages"的跨切面关注点，却和 ReAct 循环机制混在主循环里。而 Twinkle 的 Hook 系统中 `SkillHook`/`MemoryHook` 已经在 `before_model_call` 用 `ctx.inputs.messages = 新list` 模式改消息（注入 skill/memory 的 system 消息）——压缩本质是同一类活，理应同构。`docs/design/hook-design.md:467-478` 早已给出 `ContextCompressionHook` 示例，`agent_loop.py:268-271` 注释也已为这个迁移预留对接点（"Use ctx.inputs.messages so that a context-compression hook ... takes effect on retry"）。

本设计把内联调用移成一个 `before_model_call` Hook，**行为零变化**，不引入新抽象。

## 2. 参考实现对照（jiuwenswarm）

jiuwenswarm 的压缩**没有**放进它那套通用 config-hook（`BEFORE_MODEL_CALL`/`PreToolUse` 等 17 事件，用户配外部命令/prompt——这才是 Twinkle `AgentHook` 的直接对应物）。它给压缩单独做了一套 `ContextProcessor` 机制：`trigger_*`（廉价判定）+ `on_*`（变换）双钩子、Rail 注册、在 context_engine 的 `get_context_window`/`add_messages` 触发；还做了多级分层压缩（卸载→摘要→激进→截断）、offload+reload、配置化阈值、自动+手动双触发、压缩可观测+持久化、protected 清单、结构化 9 段摘要 prompt。

**关键判断**：抽象意义上 ContextProcessor 也是 hook（在生命周期点注册的、能改 pipeline 的回调）。但对**单压缩器**而言，一个带内部阈值判定的 `before_model_call` AgentHook 功能上等价于一个 ContextProcessor——`trigger` 退化成 hook 内 `if tokens > threshold`，`on` 就是赋回 `ctx.inputs.messages`。trigger/on 拆分、链式串联只在**多级压缩**时才兑现。因此 Twinkle 走自己的 `AgentHook` 通用总线，是 jiuwenswarm ContextProcessor 的等价精简形态——精神对齐、规模合适。多级链等丰富能力留待后续优化（见 §7）。

## 3. 目标与非目标

**目标**

- 把 `agent_loop.py:231-237` 内联 `compress_messages` 调用移成 `before_model_call` 的 `AgentHook`。
- 行为零变化：压缩时机 / 算法 / 阈值 / 输出 / 不写回 SessionStore 全部不变。
- 主循环少一段跨切面关注点，与 SkillHook/MemoryHook 同构、可发现、顺序用 priority 显式表达。

**非目标（YAGNI，留待后续）**

- 不预拆 trigger/on 双方法。
- 不做 processor 链 / 多级分层压缩 / offload+reload / 压缩可观测事件 / protected 清单 / 结构化摘要 prompt。
- 不改压缩语义（保持主动每步压缩；被动 `on_model_exception` 压缩是 `hook-design.md` 设想的方案 B，不做）。

## 4. 方案

### 4.1 新文件 `twinkle/agentserver/hooks/builtin/context_compression_hook.py`

仿 `SkillHook`（`hooks/builtin/skill_hook.py`）模式：

```python
"""ContextCompressionHook — before_model_call 压缩历史。

每步主动压缩（与原内联调用等价）：估算 token 超 threshold 时，把 middle
LLM 摘要成一条 system 消息，保留 head(system)+tail(最近 N 对,tool 配对闭合)。
压缩结果不写回 SessionStore,只改 ctx.inputs.messages(赋新 list,不 in-place)。
复用 context_compression.compress_messages,算法逻辑不变,只换调用方。

阈值传 None 时从 config 读(生产),测试可直传(仿 SkillHook.mode)。
"""
from __future__ import annotations

from twinkle.agentserver.context_compression import compress_messages
from twinkle.agentserver.hooks.base import AgentHook, HookContext


class ContextCompressionHook(AgentHook):
    priority = 95  # 功能层;高于 SkillHook(90)/MemoryHook(80),确保先跑、看原始 session 消息

    def __init__(self, llm, *, token_threshold=None, keep_recent_pairs=None, summary_prompt=None):
        self._llm = llm
        self._token_threshold = token_threshold
        self._keep_recent_pairs = keep_recent_pairs
        self._summary_prompt = summary_prompt

    async def before_model_call(self, ctx: HookContext) -> None:
        compressed = await compress_messages(
            ctx.inputs.messages, self._llm,
            token_threshold=self._token_threshold or _get_token_threshold(),
            keep_recent_pairs=self._keep_recent_pairs or _get_keep_recent_pairs(),
            summary_system_prompt=self._summary_prompt or _get_summary_prompt(),
        )
        # 赋新 list(不 in-place mutate——msgs 可能是 store 内部 list)
        ctx.inputs.messages = compressed


def _get_token_threshold():
    from twinkle.config import CONTEXT_TOKEN_THRESHOLD
    return CONTEXT_TOKEN_THRESHOLD


def _get_keep_recent_pairs():
    from twinkle.config import CONTEXT_KEEP_RECENT_PAIRS
    return CONTEXT_KEEP_RECENT_PAIRS


def _get_summary_prompt():
    from twinkle.config import CONTEXT_SUMMARY_PROMPT
    return CONTEXT_SUMMARY_PROMPT
```

### 4.2 导出 `hooks/builtin/__init__.py`

照 `SubagentContextHook` 加一行：

```python
from twinkle.agentserver.hooks.builtin.context_compression_hook import ContextCompressionHook
# __all__ 追加 "ContextCompressionHook"
```

### 4.3 注册 `server.py` `build_agent_loop`

auto-wire，仿 `SubagentContextHook`（其 dep executor 在 build_agent_loop 构造；compression 的 dep llm 也在）：

```python
from twinkle.agentserver.hooks.builtin import ContextCompressionHook  # lazy import,同 SubagentContextHook
...
for hook in list(hooks or []) + [SubagentContextHook(executor), ContextCompressionHook(llm=llm)]:
    loop.register_hook(hook)
```

`main()` 的 hooks 列表 `[PermissionHook(engine), SkillHook(), MemoryHook(), LoggingHook(), RetryHook()]` 不变（compression 不再 caller-passed）。

### 4.4 `agent_loop.py` 清理

- 删内联调用（230-237 行 `# -- Context compression (before hook trigger) -- #` 段 + `compress_messages(...)`）。
- 删孤儿 import：`from twinkle.agentserver.context_compression import compress_messages`（:40）；config 三个常量 `CONTEXT_KEEP_RECENT_PAIRS` / `CONTEXT_SUMMARY_PROMPT` / `CONTEXT_TOKEN_THRESHOLD`（:43-45）。
- import 块（:41-46）其余不变——保留 `AGENT_MAX_STEPS as MAX_STEPS`，仅删去上述三个 `CONTEXT_*` 行。
- 保留 `ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())`（:240）——`msgs` 现在是原始 session 消息（`session_store.get_messages`，:228），压缩交给 hook。

## 5. 行为零变化保证

时序对照：

```
当前: get_messages(228) → 内联compress(231) → ctx.inputs=msgs(240) → BEFORE_MODEL_CALL[Skill90/Memory80/Logging10](241) → merge(247) → stream(ctx.inputs.messages)(271)
改后: get_messages(228) →                ctx.inputs=msgs(240) → BEFORE_MODEL_CALL[Compression95→Skill90→Memory80→Logging10](241) → merge(247) → stream(271)
```

- **Compression 先于 Skill/Memory**：看到原始 msgs（identity system + 历史），与当前内联调用看到的一致。若 Compression 落到 Skill/Memory 之后，会把 skill/memory 注入的 system 消息误当 middle 摘要——故 priority 必须 >90，取 95。
- **merge 正确归类**：`_merge_system_messages`（`agent_loop.py:440-508`）按 content 前缀分类——`[prior context summary]` → summary 段，`## 可用技能` → skill 段，`## 长期记忆` → memory 段。无论 hook 顺序都正确归类、按 identity→skill→memory→summary→other 排序合并，最终 merged system 顺序不变。
- **LoggingHook(10) 最后跑**：看到的仍是"压缩后+skill+memory"消息，日志行为不变。
- **HookManager fail-soft**：Compression 抛异常只 log 不影响其他 hook；而 `compress_messages` 内部已 catch 摘要失败降级 head+tail（`context_compression.py:108-112`），不会抛。
- **不写回 SessionStore**：`ctx.inputs.messages` 改的是喂 LLM 的副本，`history.json` 仍无损。

## 6. 测试

### 6.1 改 `tests/test_agent_loop_compress.py`

当前两用例 monkeypatch `agent_loop.CONTEXT_*`（这些常量已从 agent_loop 移除，monkeypatch 会失败）。改成构造 hook 并注册：

```python
from twinkle.agentserver.hooks.builtin import ContextCompressionHook

loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())
loop.register_hook(ContextCompressionHook(
    llm=real_llm, token_threshold=1, keep_recent_pairs=2, summary_prompt="p"))
```

- `test_run_stream_compresses_before_llm`：threshold=1 触发压缩，断言 `estimate_tokens(real_llm.seen) < estimate_tokens(big)` 且 `seen[0]["role"]=="system"` 不变。
- `test_run_stream_no_compress_under_threshold`：threshold=60_000 + 小历史，断言无 `[prior context summary]` 消息、`frames[-1].response_kind=="e2a.complete"` 不变。

### 6.2 新增 `tests/test_context_compression_hook.py`（直接单测 hook）

- 过阈值：`ctx.inputs.messages` 被压缩、赋回新 list、原 list 未被 in-place mutate。
- 未过阈值：no-op，`ctx.inputs.messages` 是副本（与输入 list 相等但非同一对象）。
- 摘要 LLM 失败：降级 head+tail（无 summary 段），不抛。
- 配置回退：`__init__` 不传阈值时，从 config 读默认值。

### 6.3 不变

- `tests/test_context_compression.py`（测 `compress_messages` 模块本身）不动。

## 7. 后续（朝 jiuwenswarm 多级链优化，非本次）

- trigger/on 双钩子拆分 + processor 链（多级分层：卸载→摘要→激进→截断，保真度逐级降）。
- offload+按需 reload（不丢只藏，模型可 `reloader_tool` 拉回）。
- 压缩策略配置化（阈值/保留数/目标 token/prompt 全可配，支持不同模型窗口）。
- 压缩可观测 + 持久化（压缩前后 token 数、省了多少、哪个 processor 触发、摘要写 history.jsonl）。
- protected 清单（关键 system/工具结果不被压缩）。
- 被动 `on_model_exception` 压缩（token 溢出才压 + `request_retry`）。

## 8. 验收标准

- `python -m pytest tests/test_context_compression.py tests/test_context_compression_hook.py tests/test_agent_loop_compress.py -v` 全绿。
- `python -m pytest tests/ -v` 全绿（无回归）。
- 超长对话压测：超过 60000 估算 token 时，喂 LLM 的 messages 含 `[prior context summary]` system 段且 token 数下降；短对话不压缩。
