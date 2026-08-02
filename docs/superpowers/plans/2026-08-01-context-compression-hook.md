# 上下文压缩抽成 Hook 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把内联在 `agent_loop.py:231-237` 的 `compress_messages` 调用移成一个 `before_model_call` 的 `AgentHook`，行为零变化。

**Architecture:** 新建 `ContextCompressionHook`（priority 95，先于 SkillHook 90/MemoryHook 80 跑，看到原始 session 消息），复用现有 `context_compression.compress_messages` 算法不动，通过 `ctx.inputs.messages = 压缩后 list` 赋新 list（不 in-place）。在 `build_agent_loop` 里 auto-wire（仿 `SubagentContextHook`）。`_merge_system_messages` 已按 `[prior context summary]` 前缀归类，hook 顺序不影响最终合并。

**Tech Stack:** Python asyncio，Twinkle 自有 `AgentHook`/`HookManager`，pytest（`asyncio.run()` + 无 pytest-asyncio）。

> **仓库约定（务必遵守）：** 提交只在**本地** commit，**不要 push**（用户明确说才 push）。提交到 main 分支即可（照 `bf0f8fb` docs 模式）。每个 Task 结束 commit 一次。

---

## File Structure

| 文件 | 动作 | 责任 |
|---|---|---|
| `twinkle/agentserver/hooks/builtin/context_compression_hook.py` | 新建 | `ContextCompressionHook` 类：`before_model_call` 时压缩 `ctx.inputs.messages` 并赋回 |
| `twinkle/agentserver/hooks/builtin/__init__.py` | 改 | 导出 `ContextCompressionHook` |
| `tests/test_context_compression_hook.py` | 新建 | hook 单元测试（过阈值/未过阈值/LLM 失败降级/config 回退） |
| `tests/test_agent_loop_compress.py` | 改 | 改成注册 hook（不再 monkeypatch `agent_loop.CONTEXT_*`） |
| `twinkle/agentserver/server.py` | 改 | `build_agent_loop` auto-wire `ContextCompressionHook(llm=llm)` |
| `twinkle/agentserver/agent_loop.py` | 改 | 删内联 `compress_messages` 调用 + 孤儿 import |

**不动：** `twinkle/agentserver/context_compression.py`（算法）、`twinkle/resources/config.yaml` + `twinkle/config/__init__.py`（config 常量，`CONTEXT_*` 仍定义于此，hook 经 `_get_*` 读）、`tests/test_context_compression.py`（模块测试）、`tests/test_config_context.py` + `tests/test_config_constants.py`（config 测试）。

---

## Task 1: 创建 ContextCompressionHook + 导出 + 单元测试

**Files:**
- Create: `tests/test_context_compression_hook.py`
- Create: `twinkle/agentserver/hooks/builtin/context_compression_hook.py`
- Modify: `twinkle/agentserver/hooks/builtin/__init__.py`

- [ ] **Step 1: 写失败的单测**

创建 `tests/test_context_compression_hook.py`：

```python
import asyncio

from twinkle.agentserver.context_compression import estimate_tokens
from twinkle.agentserver.hooks.base import ModelCallInputs
from twinkle.agentserver.hooks.builtin import ContextCompressionHook
from twinkle.agentserver.llm_client import TextDelta


class _SummaryLLM:
    """Fake LLM for the summary call inside compress_messages. Yields fixed text."""
    def __init__(self, summary="摘要内容"):
        self._summary = summary

    async def stream(self, messages, tools):
        yield TextDelta(self._summary)


class _RaisingLLM:
    """stream() raises — exercises compress_messages' degrade-to-head+tail path."""
    async def stream(self, messages, tools):
        raise RuntimeError("boom")


class _Ctx:
    """Minimal ctx stub: hook only touches ctx.inputs.messages."""
    def __init__(self, messages):
        self.inputs = ModelCallInputs(messages=messages, tools=[])


def _big_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    return msgs


def test_compresses_over_threshold_and_assigns_back():
    hook = ContextCompressionHook(
        llm=_SummaryLLM(), token_threshold=1, keep_recent_pairs=2, summary_prompt="p")
    big = _big_messages()
    ctx = _Ctx(big)

    asyncio.run(hook.before_model_call(ctx))

    contents = [m.get("content", "") for m in ctx.inputs.messages]
    assert any("[prior context summary]" in c for c in contents)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    # 原输入 list 未被 in-place 修改
    assert not any("[prior context summary]" in m.get("content", "") for m in big)


def test_noop_under_threshold():
    hook = ContextCompressionHook(
        llm=_SummaryLLM(), token_threshold=60_000, keep_recent_pairs=6, summary_prompt="p")
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    ctx = _Ctx(small)

    asyncio.run(hook.before_model_call(ctx))

    assert not any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
    assert ctx.inputs.messages == small
    assert ctx.inputs.messages is not small  # no-op 返回副本,非同一对象


def test_degrades_to_head_tail_on_llm_failure():
    hook = ContextCompressionHook(
        llm=_RaisingLLM(), token_threshold=1, keep_recent_pairs=2, summary_prompt="p")
    ctx = _Ctx(_big_messages())

    asyncio.run(hook.before_model_call(ctx))  # 不得抛

    assert not any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
    assert ctx.inputs.messages[0]["role"] == "system"  # head 保留


def test_uses_config_defaults_when_no_override(monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 1)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 2)
    monkeypatch.setattr(twinkle.config, "CONTEXT_SUMMARY_PROMPT", "p")
    hook = ContextCompressionHook(llm=_SummaryLLM())  # 不传 → 从 config 读
    ctx = _Ctx(_big_messages())

    asyncio.run(hook.before_model_call(ctx))

    assert any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_compression_hook.py -v`
Expected: FAIL — `ImportError: cannot import name 'ContextCompressionHook' from 'twinkle.agentserver.hooks.builtin'`（hook 还没导出）。

- [ ] **Step 3: 实现 hook + 导出**

创建 `twinkle/agentserver/hooks/builtin/context_compression_hook.py`：

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

改 `twinkle/agentserver/hooks/builtin/__init__.py`，加一行 import（alphabetical 首位）+ `__all__` 首位：

```python
from twinkle.agentserver.hooks.builtin.context_compression_hook import ContextCompressionHook
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.agentserver.hooks.builtin.retry_hook import RetryHook
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.hooks.builtin.subagent_context_hook import SubagentContextHook

__all__ = ["ContextCompressionHook", "LoggingHook", "MemoryHook", "PermissionHook", "RetryHook", "SkillHook", "SubagentContextHook"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_compression_hook.py -v`
Expected: PASS（4 个用例全绿）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_context_compression_hook.py twinkle/agentserver/hooks/builtin/context_compression_hook.py twinkle/agentserver/hooks/builtin/__init__.py
git commit -m "feat(hooks): add ContextCompressionHook (before_model_call)"
```

> 此时 hook 已存在并被单测覆盖，但**未接入** agent_loop（内联压缩仍在跑）+ **未 auto-wire**。既有测试套件不受影响。

---

## Task 2: 改集成测试用 hook（红→绿）

**Files:**
- Modify: `tests/test_agent_loop_compress.py`

> 当前这个文件 monkeypatch `agent_loop.CONTEXT_TOKEN_THRESHOLD` 等（这些常量 Task 3 会从 agent_loop 移除，monkeypatch 会失败）。本 Task 改成直接注册 hook。红→绿：先去掉 monkeypatch 且**不**注册 hook → 无压缩 → 红；再加注册 → 绿。

- [ ] **Step 1: 改 `test_run_stream_compresses_before_llm` —— 去掉 monkeypatch、暂不注册 hook**

把 `tests/test_agent_loop_compress.py` 的 `test_run_stream_compresses_before_llm` 改成（删掉 3 行 `monkeypatch.setattr`，先**不**加 `register_hook`）：

```python
def test_run_stream_compresses_before_llm():
    # 先去掉 monkeypatch、不注册 hook —— 期望失败（无压缩）。
    big = [{"role": "system", "content": "s"}]
    big += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    store = _Store(big)
    real_llm = _LLM()
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())

    env = E2AEnvelope(
        request_id="r1", session_id="s1", method="chat.send", params={"query": "hi"}
    )
    frames = []

    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)

    asyncio.run(collect())
    assert real_llm.seen is not None
    assert estimate_tokens(real_llm.seen) < estimate_tokens(big)
    assert real_llm.seen[0]["role"] == "system"
```

（`test_run_stream_no_compress_under_threshold` 暂不动。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_agent_loop_compress.py::test_run_stream_compresses_before_llm -v`
Expected: FAIL — `assert estimate_tokens(real_llm.seen) < estimate_tokens(big)`：内联压缩此时用默认 `CONTEXT_TOKEN_THRESHOLD=60000`，而测试历史 ~1380 token 低于 60000，内联 no-op，无压缩，`seen` 等于 `big`。

- [ ] **Step 3: 加 hook 注册 → 绿**

在 `test_run_stream_compresses_before_llm` 里 `loop = agent_loop.AgentLoop(...)` 之后加一行注册：

```python
def test_run_stream_compresses_before_llm():
    big = [{"role": "system", "content": "s"}]
    big += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    store = _Store(big)
    real_llm = _LLM()
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())
    loop.register_hook(ContextCompressionHook(
        llm=real_llm, token_threshold=1, keep_recent_pairs=2, summary_prompt="p"))

    env = E2AEnvelope(
        request_id="r1", session_id="s1", method="chat.send", params={"query": "hi"}
    )
    frames = []

    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)

    asyncio.run(collect())
    assert real_llm.seen is not None
    assert estimate_tokens(real_llm.seen) < estimate_tokens(big)
    assert real_llm.seen[0]["role"] == "system"  # head 保留
```

并在文件顶部 import 区加：

```python
from twinkle.agentserver.hooks.builtin import ContextCompressionHook
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_agent_loop_compress.py::test_run_stream_compresses_before_llm -v`
Expected: PASS — hook（threshold=1）压缩了历史，`seen` 比 `big` 小。

- [ ] **Step 5: 改 `test_run_stream_no_compress_under_threshold`**

去掉 3 行 monkeypatch，注册 hook（高阈值）：

```python
def test_run_stream_no_compress_under_threshold():
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    store = _Store(small)
    real_llm = _LLM()
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())
    loop.register_hook(ContextCompressionHook(
        llm=real_llm, token_threshold=60_000, keep_recent_pairs=6, summary_prompt="p"))

    env = E2AEnvelope(
        request_id="r2", session_id="s2", method="chat.send", params={"query": "yo"}
    )
    frames = []

    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)

    asyncio.run(collect())
    assert real_llm.seen is not None
    assert not any("[prior context summary]" in m.get("content", "") for m in real_llm.seen)
    assert frames and frames[-1].response_kind == "e2a.complete"
```

- [ ] **Step 6: 跑整个文件确认通过**

Run: `python -m pytest tests/test_agent_loop_compress.py -v`
Expected: PASS（2 个用例全绿）。

- [ ] **Step 7: Commit**

```bash
git add tests/test_agent_loop_compress.py
git commit -m "test(compression): integration test uses ContextCompressionHook"
```

---

## Task 3: 接入生产 —— auto-wire + 移除内联

**Files:**
- Modify: `twinkle/agentserver/server.py`（`build_agent_loop`）
- Modify: `twinkle/agentserver/agent_loop.py`

> 先 auto-wire（加新路径）再移除内联（删旧路径），保证工作树里不会出现"两条路径都没"的窗口。

- [ ] **Step 1: auto-wire 进 `build_agent_loop`**

改 `twinkle/agentserver/server.py` 的 `build_agent_loop`。把 lazy import 行和 auto-wire 列表各加 `ContextCompressionHook`：

old:
```python
    from twinkle.agentserver.tools.builtin.subagent import create_subagent_executor
    from twinkle.agentserver.hooks.builtin import SubagentContextHook
    from twinkle.config import settings
    executor = create_subagent_executor(
        llm=llm, store=store, parent_tools=tools, config=settings.subagent
    )
    for hook in list(hooks or []) + [SubagentContextHook(executor)]:
        loop.register_hook(hook)
    return loop
```

new:
```python
    from twinkle.agentserver.tools.builtin.subagent import create_subagent_executor
    from twinkle.agentserver.hooks.builtin import SubagentContextHook, ContextCompressionHook
    from twinkle.config import settings
    executor = create_subagent_executor(
        llm=llm, store=store, parent_tools=tools, config=settings.subagent
    )
    # ContextCompressionHook auto-wire:dep 是 llm,在此构造(同 SubagentContextHook/executor)。
    for hook in list(hooks or []) + [SubagentContextHook(executor), ContextCompressionHook(llm=llm)]:
        loop.register_hook(hook)
    return loop
```

- [ ] **Step 2: 移除内联压缩 + 孤儿 import（agent_loop.py）**

改 `twinkle/agentserver/agent_loop.py` 两处。

**处 1 —— import 块**（去掉 `compress_messages` import 和三个 `CONTEXT_*`）：

old:
```python
from twinkle.agentserver.context_compression import compress_messages
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    CONTEXT_KEEP_RECENT_PAIRS,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_TOKEN_THRESHOLD,
    MEMORY_DIR,
    SKILLS_DIR,
    WORKSPACE_DIR,
)
```

new:
```python
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    MEMORY_DIR,
    SKILLS_DIR,
    WORKSPACE_DIR,
)
```

**处 2 —— 内联调用块**（删掉 `# -- Context compression -- #` 段）：

old:
```python
            msgs = self._session_store.get_messages(session_id)

            # -- Context compression (before hook trigger) -- #
            msgs = await compress_messages(
                msgs,
                self._llm,
                token_threshold=CONTEXT_TOKEN_THRESHOLD,
                keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS,
                summary_system_prompt=CONTEXT_SUMMARY_PROMPT,
            )

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())
```

new:
```python
            msgs = self._session_store.get_messages(session_id)

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())
```

- [ ] **Step 3: 跑全套确认无回归**

Run: `python -m pytest tests/ -v`
Expected: PASS（全绿）。

> 关键时序自检：改后 `get_messages` → `ctx.inputs=msgs`（原始）→ `BEFORE_MODEL_CALL[Compression95→Skill90→Memory80→Logging10]` → `merge` → `stream`。Compression 先于 Skill/Memory，看到原始 msgs；`_merge_system_messages` 按 `[prior context summary]` 前缀归类，合并顺序不变。

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/server.py twinkle/agentserver/agent_loop.py
git commit -m "refactor(compression): move inline compress into auto-wired hook"
```

---

## 验收

- [ ] `python -m pytest tests/test_context_compression.py tests/test_context_compression_hook.py tests/test_agent_loop_compress.py -v` 全绿。
- [ ] `python -m pytest tests/ -v` 全绿（无回归）。
- [ ] `git log --oneline -3` 显示三个本 Task 的 commit（未 push）。
- [ ] 超长对话（>60000 估算 token）跑下来，喂 LLM 的 messages 含 `[prior context summary]` system 段且 token 下降；短对话不压缩。
