# 上下文组装对齐 jiuwenswarm + KV cache 友好 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Twinkle 的上下文组装改成 jiuwenswarm 式的 section 覆写（同名覆写不堆叠）+ env 移尾部 UserMessage，使 system 前缀字节稳定、provider 端 prefix cache 可命中；删除脆弱的字符串前缀 merge。

**Architecture:** 新增 `SystemPromptBuilder`（dict-by-name section + priority join）。`ReActAgent` 每步新建 builder、注入 base sections（normal/leader/member/subagent 四套，member/subagent 在构造时注入带 persona 的 base_sections），经 `ctx.builder` 共享给 hook；`SkillHook`/`MemoryHook` 改用 `ctx.builder.add_section`（不再 prepend system msg）；新 `RuntimeEnvHook`(p99) 把 today/os 放 `ctx.extra["environment_context"]`，loop 发 LLM 前拼成尾部 `<environment_context>` UserMessage。session_store 不再存 system prompt（删 4 处 pre-seed），`_merge_system_messages` 整体删除。

**Tech Stack:** Python 3.12, asyncio, dataclasses, pytest（`asyncio.run()` 风格，无 pytest-asyncio）。

---

## 关键设计决策（写代码前必读）

1. **为什么 member/subagent 也必须改**：`SkillHook`/`MemoryHook` 是全局 hook（normal/leader/member/subagent 都注册），改 `add_section` 后若 `ctx.builder is None` 会 AttributeError。所以 builder 必须在**所有**路径都存在。member/subagent 的 identity 带 persona（构造时已知，不在通用 loop 里），所以必须**构造时注入 base_sections**，并删除它们的 session_store pre-seed（否则双 system）。这是最小正确设计，不是镀金。

2. **single-section-per-mode（偏离 spec §2 的 4-section 表）**：每个 mode 的 base prompt 包成**一个** `PromptSection("system_prompt", <build_*_system_prompt() 去env>, priority=10)`，不拆 identity/runtime_guidance/workspace/tools_guidance 四子段。理由(YAGNI)：无 hook 覆写单个子段；builder.build() 输出与拆分版字节等价；spec §2 的拆分无功能收益。skill/memory 仍是独立 section（hook 加）。

3. **env 移尾部、builder 注入顺序**：每步 loop：`builder = new` + base sections → `ctx.builder = builder` → `ctx.inputs.messages = msgs`(纯历史,无 system) → `execute(BEFORE_MODEL_CALL)`（hook 改 builder/messages）→ **替换旧 `_merge_system_messages` 调用点**：`ctx.inputs.messages = [{"role":"system","content":ctx.builder.build()}] + ctx.inputs.messages` + `ctx.extra.pop("environment_context")` 拼尾部 UserMessage。压缩(p95)在 hook 阶段对纯历史操作（head 空），产出 `[summary]+tail`，builder.build() 随后 prepend 其前 → `[builder.build(), summary, tail, env]`。

4. **不改动压缩子系统**：`split_messages_head_middle_tail` 已处理无 system head（`head=[]`），`compress_messages` 不动。压缩 hook 不碰 `ctx.builder`，builder 不碰压缩 summary（summary 是独立 system msg，不进 builder）。

5. **守 [[observability-via-instrumentor-not-inline]]**：本计划不加任何 OTel span 到 hook/业务代码。**守 [[json-contract-prompts-not-in-config]]**：MemoryHook 策略 prompt 仍是硬编码模块常量（自由文本，非 JSON 契约），不进 config。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `twinkle/agentserver/prompts.py` | `PromptSection` dataclass + `SystemPromptBuilder`（dict 覆写 + priority join） | 新建 |
| `twinkle/agentserver/hooks/base.py` | `HookContext` 加 `builder` 字段 | 改 |
| `twinkle/agentserver/hooks/builtin/runtime_env_hook.py` | `RuntimeEnvHook`(p99) 注 env 到 `ctx.extra` | 新建 |
| `twinkle/agentserver/hooks/builtin/__init__.py` | 导出 `RuntimeEnvHook` | 改 |
| `twinkle/agentserver/agent.py` | 去 env 的 3 个 build_* prompt + 3 个 base_sections 工厂 + `ReActAgent.__init__` 加 base_sections + loop builder 注入/删 session_store system/删 merge | 改 |
| `twinkle/agentserver/hooks/builtin/skill_hook.py` | `_prepend_system_message` → `ctx.builder.add_section` | 改 |
| `twinkle/agentserver/hooks/builtin/memory_hook.py` | 去 today + `_prepend` → `add_section` x2 | 改 |
| `twinkle/agentserver/server.py` | `create_agent` 加 `RuntimeEnvHook()` | 改 |
| `twinkle/agentserver/team/manager.py` | `_build_member` 删 pre-seed + 注 base_sections + 加 RuntimeEnvHook | 改 |
| `twinkle/agentserver/tools/builtin/subagent/executor.py` | 删 pre-seed + 注 base_sections + 加 RuntimeEnvHook + 删 `_system_prompt` | 改 |
| `tests/test_prompts.py` | SystemPromptBuilder 覆写/排序 | 新建 |
| `tests/test_runtime_env_hook.py` | env 注入/pop 防累积 | 新建 |
| `tests/test_skill_hook.py` | 改断言到 `ctx.builder.build()` | 改 |
| `tests/test_memory_hook.py` | 改断言到 builder sections + `_ctx` 设 builder | 改 |
| `tests/test_team.py` | 改 `test_member_prompt_has_runtime_environment`(去 当前平台/当前日期) | 改 |

---

## Task 1: SystemPromptBuilder 模块

**Files:**
- Create: `twinkle/agentserver/prompts.py`
- Test: `tests/test_prompts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts.py
"""SystemPromptBuilder — dict-by-name section 覆写(同名不堆叠) + priority 排序 join。"""
from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder


def test_add_section_overwrites_same_name_not_stack():
    """同名 section 后者覆写前者(不堆叠)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("skills", "v1", priority=90))
    b.add_section(PromptSection("skills", "v2", priority=90))
    assert b.build() == "v2"


def test_build_sorts_by_priority():
    """build() 按 priority 升序 join(小在前)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("memory", "MEM", priority=80))
    b.add_section(PromptSection("skills", "SKILL", priority=90))
    b.add_section(PromptSection("identity", "ID", priority=10))
    assert b.build() == "ID\n\nMEM\n\nSKILL"


def test_remove_section():
    b = SystemPromptBuilder()
    b.add_section(PromptSection("skills", "S", priority=90))
    b.remove_section("skills")
    assert b.build() == ""
    # remove 不存在的 name 不报错
    b.remove_section("nope")


def test_build_empty_returns_empty_string():
    assert SystemPromptBuilder().build() == ""


def test_build_is_idempotent_and_deterministic():
    """稳定 section 内容不变 → build() 输出不变(prefix cache 友好的前提)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("identity", "STABLE", priority=10))
    b.add_section(PromptSection("skills", "ALSO_STABLE", priority=90))
    assert b.build() == b.build()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prompts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.prompts'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/prompts.py
"""SystemPromptBuilder — dict-by-name section 覆写 + priority 排序 join。

对齐 jiuwenswarm core/single_agent/prompts/builder.py 核心(砍多语言)：
- add_section = _sections[name] = section（同名覆写,不堆叠）
- build() = 按 priority 升序 "\n\n".join(content)

每步 per-request 新建实例。build() 每次全量重建、幂等、确定性。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PromptSection:
    name: str
    content: str
    priority: int


class SystemPromptBuilder:
    """dict-by-name section + priority 排序 + 同名覆写(不堆叠)。"""

    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def add_section(self, section: PromptSection) -> None:
        self._sections[section.name] = section  # 同名覆写

    def remove_section(self, name: str) -> None:
        self._sections.pop(name, None)

    def build(self) -> str:
        return "\n\n".join(
            s.content for s in
            sorted(self._sections.values(), key=lambda x: x.priority)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prompts.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/prompts.py tests/test_prompts.py
git commit -m "feat(prompts): add SystemPromptBuilder (dict section overwrite + priority join)"
```

---

## Task 2: HookContext.builder 字段

**Files:**
- Modify: `twinkle/agentserver/hooks/base.py:175-190`
- Test: `tests/test_hook_context_builder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hook_context_builder.py
"""HookContext.builder 字段——loop 每步赋 builder,hook 从 ctx.builder.add_section。"""
from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder


def _ctx():
    return HookContext(
        agent=None, event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q"), session_id="s", request_id="r",
    )


def test_builder_defaults_none():
    assert _ctx().builder is None


def test_builder_settable_and_usable():
    ctx = _ctx()
    ctx.builder = SystemPromptBuilder()
    ctx.builder.add_section(PromptSection("skills", "S", priority=90))
    assert ctx.builder.build() == "S"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_context_builder.py -v`
Expected: FAIL — `AttributeError: 'HookContext' object has no attribute 'builder'` (or assignment fails)

- [ ] **Step 3: Write minimal implementation**

在 `twinkle/agentserver/hooks/base.py` 的 `HookContext` dataclass 中，`extra` 字段后加 `builder` 字段。`base.py` 顶部已 import `Any`（`agent: Any` 在用），直接用。

```python
# twinkle/agentserver/hooks/base.py —— HookContext dataclass
# 在 extra: dict = field(default_factory=dict) 之后、exception 之前插入:
    extra: dict = field(default_factory=dict)
    builder: Any = None  # SystemPromptBuilder | None — loop 每步赋,hook 读
    exception: Exception | None = None
    retry_attempt: int = 0
```

（精确改：把 `extra: dict = field(default_factory=dict)` 行后紧跟的 `exception: Exception | None = None` 之间插入 `builder: Any = None` 一行。）

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook_context_builder.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/hooks/base.py tests/test_hook_context_builder.py
git commit -m "feat(hooks): add builder field to HookContext for section-based prompt assembly"
```

---

## Task 3: RuntimeEnvHook

**Files:**
- Create: `twinkle/agentserver/hooks/builtin/runtime_env_hook.py`
- Test: `tests/test_runtime_env_hook.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runtime_env_hook.py
"""RuntimeEnvHook — before_model_call 把 today/os 放 ctx.extra['environment_context']。

priority 99 最先跑;不进 system prompt(用 ctx.extra 不用 ctx.builder);
loop 端 pop() 消费防多轮累积。
"""
import asyncio
import platform
import datetime

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook


def _ctx():
    return HookContext(
        agent=None, event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[]),
        session_id="s", request_id="r",
    )


def test_env_goes_to_extra_not_messages():
    ctx = _ctx()
    asyncio.run(RuntimeEnvHook().before_model_call(ctx))
    # messages 不变(env 不进 system)
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
    # env 在 extra
    env = ctx.extra.get("environment_context")
    assert env and len(env) == 1
    content = env[0]["content"]
    assert platform.system() or True  # smoke
    assert datetime.date.today().isoformat() in content
    assert ctx.extra["environment_context"][0]["source"] == "runtime_env"


def test_priority_is_99():
    assert RuntimeEnvHook.priority == 99


def test_loop_pop_prevents_accumulation_across_steps():
    """模拟 loop 两步:每步 RuntimeEnvHook append,loop 端 pop,不累积。"""
    ctx = _ctx()
    hook = RuntimeEnvHook()
    for _ in range(2):
        asyncio.run(hook.before_model_call(ctx))
        # loop 端消费
        env = ctx.extra.pop("environment_context", None)
        assert env and len(env) == 1
    # 第二步 pop 后 extra 无残留
    assert "environment_context" not in ctx.extra
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runtime_env_hook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.hooks.builtin.runtime_env_hook'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/hooks/builtin/runtime_env_hook.py
"""RuntimeEnvHook — before_model_call 把易变 env(today/os)放 ctx.extra['environment_context']。

env 不进 system prompt(用 ctx.extra 不用 ctx.builder)——system 前缀字节稳定,
provider 端 prefix cache 不被每步/每日变动的 today 破坏。loop 端 pop() 拼尾部
<environment_context> UserMessage。UserMessage 不 SystemMessage:多数 provider 把额外
SystemMessage 合并进 system 参数破坏前缀 cache 稳定性(jiuwenswarm 明示理由)。
"""
from __future__ import annotations

import datetime
import sys

from twinkle.agentserver.hooks.base import AgentHook, HookContext


class RuntimeEnvHook(AgentHook):
    priority = 99  # before_model_call 最先跑(高于 ContextCompression 95 / Skill 90 / Memory 80)

    async def before_model_call(self, ctx: HookContext) -> None:
        content = (
            f"当前平台：`{sys.platform}`\n"
            f"当前日期：`{datetime.date.today().isoformat()}`"
        )
        ctx.extra.setdefault("environment_context", []).append(
            {"content": content, "source": "runtime_env"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runtime_env_hook.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Register in `hooks/builtin/__init__.py`**

在 `twinkle/agentserver/hooks/builtin/__init__.py` 顶部 import 区加一行，`__all__` 列表加 `"RuntimeEnvHook"`：

```python
# 顶部 import 区(按字母序插在 RepeatToolCallDetectorHook 之前):
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook
```

```python
# __all__ 列表加 "RuntimeEnvHook"(放 RepeatToolCallDetectorHook 前):
__all__ = [
    "ContextCompressionHook", "ContextOverflowRecoveryHook",
    "LoggingHook", "MemoryFlushHook", "MemoryHook", "PermissionHook",
    "RepeatToolCallDetectorHook", "RetryHook", "RuntimeEnvHook",
    "SkillEvolutionHook", "SkillHook", "SubagentContextHook", "TeamContextHook",
]
```

- [ ] **Step 6: Verify import works**

Run: `python -c "from twinkle.agentserver.hooks.builtin import RuntimeEnvHook; print(RuntimeEnvHook.priority)"`
Expected: prints `99`

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/runtime_env_hook.py twinkle/agentserver/hooks/builtin/__init__.py tests/test_runtime_env_hook.py
git commit -m "feat(hooks): add RuntimeEnvHook (env-at-tail, today/os to ctx.extra)"
```

---

## Task 4: 从 build_* prompts 去 env + base_sections 工厂

> 三件事：(a) `build_system_prompt`/`build_agent_runtime_prompt`/`build_leader_system_prompt` 的 `# 运行环境` 块删掉 `当前平台`/`当前日期` 两行；(b) 加 3 个 `*_base_sections` 工厂函数返回 `list[PromptSection]`；(c) 改 `test_team.py` 的 env 断言。

**Files:**
- Modify: `twinkle/agentserver/agent.py:83-326`
- Modify: `tests/test_team.py:370-376`

- [ ] **Step 1: Write the failing test for env-strip**

```python
# tests/test_prompt_env_strip.py
"""build_*_system_prompt 去掉 today/os(env 移尾部 RuntimeEnvHook)。运行环境块保留(命令表+mkdir warning)。"""
from twinkle.agentserver.agent import (
    build_system_prompt, build_agent_runtime_prompt, build_leader_system_prompt,
)


def test_base_prompt_no_env_values():
    p = build_system_prompt()
    assert "当前平台" not in p
    assert "当前日期" not in p
    assert "运行环境" in p  # 块头保留
    assert "身份与行为原则" in p  # identity 保留


def test_runtime_prompt_no_env_values():
    p = build_agent_runtime_prompt()
    assert "当前平台" not in p
    assert "当前日期" not in p
    assert "运行环境" in p


def test_leader_prompt_no_env_values():
    p = build_leader_system_prompt()
    assert "当前平台" not in p
    assert "当前日期" not in p
    assert "运行环境" in p
    assert "TeamLeader" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prompt_env_strip.py -v`
Expected: FAIL — `当前平台`/`当前日期` still in prompts (assertions fail)

- [ ] **Step 3: Strip env from `build_system_prompt()`**

在 `twinkle/agentserver/agent.py` 的 `build_system_prompt()`（约 L83-149），`# 运行环境` 块下删除这两行：

```text
当前平台：`{os_type}`
当前日期：`{today_date}`
```

改后该块应为（保留块头 + 命令表 + mkdir_warning）：

```python
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
```

函数体内 `os_type`/`today_date` 局部变量：`today_date` 不再被引用 → 删 `today_date = date.today().isoformat()` 行；`os_type` 仍被 `mkdir_warning` 用 → 保留。`date` import 若无其它引用可留（`build_agent_runtime_prompt`/leader 仍用，先不删 import）。

- [ ] **Step 4: Strip env from `build_agent_runtime_prompt()`**

同文件 `build_agent_runtime_prompt()`（约 L152-195），`# 运行环境` 块删同样两行 `当前平台`/`当前日期`。改后该块：

```python
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
```

`today_date` 局部变量不再用 → 删 `today_date = date.today().isoformat()` 行。`os_type` 仍被 mkdir_warning 用 → 保留。

- [ ] **Step 5: Strip env from `build_leader_system_prompt()`**

同文件 `build_leader_system_prompt()`（约 L200-286），`# 运行环境` 块（约 L247-259）删两行 `当前平台`/`当前日期`。改后该块：

```python
# 运行环境

**必须严格使用与当前平台匹配的命令语法**，切勿混用其他平台命令。常见差异：

| 操作 | Windows | Linux/macOS |
|------|---------|-------------|
| 创建目录 | `mkdir folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` | `cat file.txt` |
| 列出文件 | `dir` | `ls -la` |
| 删除目录 | `rmdir folder` | `rm -rf folder` |{mkdir_warning}
```

`today_date` 局部变量不再用 → 删 `today_date = date.today().isoformat()` 行。`os_type` 保留（mkdir_warning 用）。

- [ ] **Step 6: Run env-strip test to verify it passes**

Run: `python -m pytest tests/test_prompt_env_strip.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Fix `test_team.py` env assertion**

`tests/test_team.py` 的 `test_member_prompt_has_runtime_environment`（L370-376）当前断言 `当前平台`/`当前日期` in prompt——这两行已移走。改为只断言块头保留：

```python
def test_member_prompt_has_runtime_environment():
    """Member prompt includes runtime environment block (platform/date moved to env-tail)."""
    from twinkle.agentserver.agent import build_member_system_prompt
    prompt = build_member_system_prompt(persona="tester", workspace="/tmp/ws")
    assert "运行环境" in prompt
    # 当前平台/当前日期 已移到尾部 <environment_context>(RuntimeEnvHook),不在 prompt 里
    assert "当前平台" not in prompt
    assert "当前日期" not in prompt
```

- [ ] **Step 8: Run team tests to verify no other breakage**

Run: `python -m pytest tests/test_team.py -v`
Expected: PASS (all member/leader/base prompt structure tests green; `test_member_prompt_has_runtime_environment` updated)

- [ ] **Step 9: Write failing test for base_sections factories**

```python
# tests/test_base_sections.py
"""normal/leader/member base_sections 工厂——返回 list[PromptSection],priority 10,内容=对应 build_* prompt。"""
from twinkle.agentserver.agent import (
    normal_base_sections, leader_base_sections, member_base_sections,
    build_system_prompt, build_leader_system_prompt, build_member_system_prompt,
)
from twinkle.agentserver.prompts import PromptSection


def test_normal_base_sections():
    secs = normal_base_sections()
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert secs[0].content == build_system_prompt()


def test_leader_base_sections():
    secs = leader_base_sections()
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert secs[0].content == build_leader_system_prompt()


def test_member_base_sections_bakes_persona():
    secs = member_base_sections(persona="数据分析师", workspace="/shared", member_name="analyst")
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert "数据分析师" in secs[0].content
    assert "/shared" in secs[0].content
    assert secs[0].content == build_member_system_prompt(
        persona="数据分析师", workspace="/shared", member_name="analyst")
```

- [ ] **Step 10: Run to verify it fails**

Run: `python -m pytest tests/test_base_sections.py -v`
Expected: FAIL — `ImportError: cannot import name 'normal_base_sections'`

- [ ] **Step 11: Implement base_sections factories**

在 `twinkle/agentserver/agent.py`，`build_member_system_prompt` 函数定义之后（约 L327 前），`_TEAM_LEADER_TOOL_WHITELIST` 之前，加 3 个工厂。需先在文件顶部 import `PromptSection`：

```python
# twinkle/agentserver/agent.py 顶部 import 区(在 from twinkle.e2a.models import E2AResponse 附近)加:
from twinkle.agentserver.prompts import PromptSection
```

```python
# twinkle/agentserver/agent.py —— 在 build_member_system_prompt 之后加:

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
```

- [ ] **Step 12: Run to verify it passes**

Run: `python -m pytest tests/test_base_sections.py -v`
Expected: PASS (3 passed)

- [ ] **Step 13: Commit**

```bash
git add twinkle/agentserver/agent.py tests/test_prompt_env_strip.py tests/test_base_sections.py tests/test_team.py
git commit -m "feat(agent): strip env from build_* prompts + add normal/leader/member base_sections factories"
```

---

## Task 5: ReActAgent 接 builder + 删 session_store system + 删 merge

> 核心改动：`__init__` 加 `base_sections` 参数；loop 每步建 builder + 注 base sections + 赋 `ctx.builder`；删 session_store system append(484-490)；用 `builder.build()` prepend + env 消费 替换 `_merge_system_messages` 调用(516)；删 `_merge_system_messages` 定义(848-902)。

**Files:**
- Modify: `twinkle/agentserver/agent.py:369-386, 460-540, 846-902`
- Test: `tests/test_agent_loop_context_assembly.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_agent_loop_context_assembly.py
"""loop 上下文组装:builder.build() 作首条 system,env 在尾部 UserMessage,无 merge,session_store 不存 system。

用 fake LLM 捕获实际发给模型的 messages,断言结构。
"""
import asyncio

from twinkle.agentserver.agent import ReActAgent, AgentRequest, normal_base_sections
from twinkle.agentserver.hooks.base import HookEvent
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook
from twinkle.agentserver.prompts import SystemPromptBuilder
from twinkle.agentserver.sessions import SessionStore


class _FinishLLM:
    """LLM stub:第一次 stream 直接 Finish,捕获 messages。"""
    def __init__(self):
        self.captured = None

    async def stream(self, *, messages, tools=None):
        self.captured = messages
        from twinkle.agentserver.llm_client import Finish
        yield Finish(reason="stop", content="done")


def _make_agent(store, *, base_sections=None, hooks=()):
    return ReActAgent(
        _FinishLLM(), store, _tools_stub(),
        hooks=tuple(hooks),
        base_sections=base_sections,
        max_steps=2,
    )


def _tools_stub():
    class _TM:
        def schemas(self): return []
    return _TM()


def test_first_message_is_builder_build_and_env_at_tail(tmp_path):
    store = SessionStore(str(tmp_path / "db.json"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(),
                       hooks=[RuntimeEnvHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass
    asyncio.run(_run())

    msgs = agent._llm.captured
    assert msgs[0]["role"] == "system"
    # builder.build() = normal base prompt(含 身份与行为原则)
    assert "身份与行为原则" in msgs[0]["content"]
    # env 在尾部 UserMessage,不在 system 前缀
    assert msgs[-1]["role"] == "user"
    assert "<environment_context>" in msgs[-1]["content"]
    assert "当前日期" in msgs[-1]["content"]
    assert "当前日期" not in msgs[0]["content"]


def test_session_store_does_not_persist_system(tmp_path):
    """session_store 只存 user/assistant,不存 system。"""
    store = SessionStore(str(tmp_path / "db.json"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(),
                       hooks=[RuntimeEnvHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass
    asyncio.run(_run())

    persisted = asyncio.run(store.get_messages("s1"))
    assert persisted[0]["role"] == "user"  # 无 system 头
    assert not any(m["role"] == "system" for m in persisted)


def test_merge_system_messages_deleted():
    """_merge_system_messages 已从 ReActAgent 删除。"""
    assert not hasattr(ReActAgent, "_merge_system_messages")
```

> **注**：`SessionStore` 的确切构造签名以 `twinkle/agentserver/sessions.py` 为准；若签名不同（如 `SessionStore(path)` 无参 db 名），按现有 `tests/test_agent_loop.py` 的 fixture 风格调整。`_tools_stub` 返回空 schemas 即可。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_loop_context_assembly.py -v`
Expected: FAIL — `base_sections` kwarg 不存在 / `_merge_system_messages` 还在 / env 不在尾部

- [ ] **Step 3: Add `base_sections` param to `__init__`**

`twinkle/agentserver/agent.py` `ReActAgent.__init__`（L369-386）：

```python
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
```

- [ ] **Step 4: Delete session_store system append in `_run_react_loop`**

删 `twinkle/agentserver/agent.py:480-490` 的 system append 块（保留 L492-496 的 user query append）：

```python
        is_team_mode = request.mode == "team"

        # (已删:session_store 不再存 system prompt。builder 每步重建注入 messages[0]。)
        await self._session_store.append(
            session_id,
            {"role": "user", "content": request.query},
            request_id=request_id,
        )
```

- [ ] **Step 5: Replace `_merge_system_messages` call with builder.build() prepend + env consumption**

`twinkle/agentserver/agent.py` 的 step 循环（约 L500-516）。在 `ctx.inputs = ModelCallInputs(...)`（L512）之后、`execute(BEFORE_MODEL_CALL)`（L513）**之前**插入 builder 注入；把 L516 的 `ctx.inputs.messages = self._merge_system_messages(...)` 替换为 builder.build() prepend + env 消费：

```python
        seq = 0
        full_text = ""
        for _step in range(self._max_steps):
            msgs = self._session_store.get_messages(session_id)
            if self._inbox is not None:
                new_messages = self._inbox.drain()
                if new_messages:
                    msgs = list(msgs) + [{"role": "user", "content": m} for m in new_messages]

            # -- BEFORE_MODEL_CALL -- #
            tool_schemas = self._tool_manager.schemas()
            if is_team_mode:
                tool_schemas = [t for t in tool_schemas
                               if t["function"]["name"] in _TEAM_LEADER_TOOL_WHITELIST]

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
```

> `SystemPromptBuilder` 已在 Task 1 的 prompts.py；需在 agent.py 顶部 import：`from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder`（Task 4 已 import PromptSection，此处扩成 `from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder`）。

- [ ] **Step 6: Delete `_merge_system_messages` definition**

删 `twinkle/agentserver/agent.py:846-902` 整个 `# -- System message merge --` 注释 + `_merge_system_messages` 静态方法。删后 `# -- @hook-decorated tool call --`（原 L904）直接接上一方法。

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_loop_context_assembly.py -v`
Expected: PASS (3 passed)

- [ ] **Step 8: Run existing agent_loop tests to catch breakage**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: 大多 PASS；若有断言"session_store 存 system"或"merged system"的用例失败，按新语义修正（builder 每步注入、session 不存 system）。修测试时保持断言意图（system 在发给 LLM 的 messages[0]），不削弱。

> 若 `test_agent_loop.py` 用了 `_merge_system_messages` 或断言 session 内 system 头，改为断言"发给 LLM 的 messages[0] 是 system 且含身份段"。记录于 commit message。

- [ ] **Step 9: Commit**

```bash
git add twinkle/agentserver/agent.py tests/test_agent_loop_context_assembly.py tests/test_agent_loop.py
git commit -m "feat(agent): builder-based system prompt + env-at-tail; delete _merge_system_messages and session_store system seed"
```

---

## Task 6: SkillHook → add_section

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/skill_hook.py`
- Modify: `tests/test_skill_hook.py`

- [ ] **Step 1: Rewrite the failing tests**

替换 `tests/test_skill_hook.py` 中依赖 `ctx.inputs.messages[0]` 的断言为 `ctx.builder.build()`。`_ctx` helper 设 `builder`：

```python
# tests/test_skill_hook.py
import asyncio
from pathlib import Path
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.prompts import SystemPromptBuilder
from twinkle.agentserver.skills import _set_skill_manager, SkillManager


def _make_skill(dir_: Path, name: str, desc: str) -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def _ctx(messages=None) -> HookContext:
    ctx = HookContext(
        agent=None, event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=messages or [], tools=[]),
        session_id="s", request_id="r",
    )
    ctx.builder = SystemPromptBuilder()
    return ctx


@pytest.fixture
def isolated_skills(tmp_path):
    _make_skill(tmp_path / "a", "a", "desc a")
    _make_skill(tmp_path / "b", "b", "desc b")
    _set_skill_manager(SkillManager(str(tmp_path)))
    yield tmp_path
    _set_skill_manager(None)


def test_all_mode_adds_skills_section(isolated_skills):
    hook = SkillHook(mode="all")
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(hook.before_model_call(ctx))
    built = ctx.builder.build()
    assert "## 可用技能" in built
    assert "a: desc a" in built
    # messages 不动(skill 进 builder,不 prepend system)
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]


def test_auto_list_mode_adds_note_section(isolated_skills):
    hook = SkillHook(mode="auto_list")
    ctx = _ctx([])
    asyncio.run(hook.before_model_call(ctx))
    assert "list_skill" in ctx.builder.build()


def test_no_skills_is_noop(tmp_path):
    _set_skill_manager(SkillManager(str(tmp_path)))  # 空目录
    try:
        ctx = _ctx([{"role": "user", "content": "hi"}])
        asyncio.run(SkillHook(mode="all").before_model_call(ctx))
        assert ctx.builder.build() == ""  # 无 section
        assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
    finally:
        _set_skill_manager(None)


def test_skills_section_overwrites_not_stacks(isolated_skills):
    """同名 section 二次 add 覆写,不堆叠(对齐 jiuwenswarm)。"""
    hook = SkillHook(mode="all")
    ctx = _ctx([])
    asyncio.run(hook.before_model_call(ctx))
    asyncio.run(hook.before_model_call(ctx))  # 再跑一次,同名 "skills" 覆写
    built = ctx.builder.build()
    assert built.count("## 可用技能") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_hook.py -v`
Expected: FAIL — 现实现 prepend 到 messages，`ctx.builder.build()` 为空

- [ ] **Step 3: Rewrite SkillHook to use add_section**

`twinkle/agentserver/hooks/builtin/skill_hook.py` 全文替换为：

```python
"""SkillHook — before_model_call 把 skill 清单/提示作为 builder section 注入。

all 模式:全部 skill name+desc 拼 section。auto_list 模式:只一句"调 list_skill"提示。
无 skills → no-op。注入用 ctx.builder.add_section(同名覆写,不堆叠),不碰 ctx.inputs.messages
(system prompt 由 loop 端 builder.build() 统一拼,不靠 hook prepend)。
mode 传 None 时从 config 读 SKILL_MODE(生产用),测试可直传 mode。
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

log = logging.getLogger("twinkle.hooks.skill")


class SkillHook(AgentHook):
    priority = 90  # 功能层(50-99);before_model_call

    def __init__(self, mode: str | None = None) -> None:
        self._mode = mode  # None → 调用时从 config 读

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return  # 无 skill → no-op
        mode = self._mode or _get_skill_mode()
        if mode == "auto_list":
            content = "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。"
        else:  # "all"(默认);未知 mode 也落到 all 并告警,避免静默误配置
            if mode != "all":
                log.warning("unknown SKILL_MODE %r, falling back to 'all'", mode)
            lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
            content = "\n".join(lines)
        ctx.builder.add_section(PromptSection("skills", content, priority=90))


def _get_skill_mode() -> str:
    from twinkle.config import SKILL_MODE
    return SKILL_MODE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_skill_hook.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/skill_hook.py tests/test_skill_hook.py
git commit -m "refactor(skill_hook): inject skills via ctx.builder.add_section (no prepend)"
```

---

## Task 7: MemoryHook → add_section x2 + 去 today

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/memory_hook.py`
- Modify: `tests/test_memory_hook.py`

- [ ] **Step 1: Rewrite the failing tests**

`tests/test_memory_hook.py`：`_ctx` 设 builder；断言改到 `ctx.builder.build()`。策略与召回分两个 section。

```python
# tests/test_memory_hook.py
"""MemoryHook 测试——策略 prompt 常开(进 memory_strategy section)+ opt-in 被动召回(进 memory_recall section)。

覆盖:空 store no-op、策略-only(开关关)、USER.md/MEMORY.md/今日 daily 注入、超 cap 截断、
开关开但无可注入文件回退策略-only。策略 prompt 不含 today(已移尾部 env)。
"""
import asyncio
import datetime as dt

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.prompts import SystemPromptBuilder


def _ctx(messages=None):
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(
            messages=messages or [{"role": "user", "content": "hi"}],
            tools=[],
        ),
        session_id="s",
        request_id="r",
    )
    ctx.builder = SystemPromptBuilder()
    return ctx


def _mgr(tmp_path, **kw):
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)


def _run(hook, ctx):
    asyncio.run(hook.before_model_call(ctx))


def _with_mgr(mgr):
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(mgr)
    return _set_memory_manager


def test_empty_store_noop(tmp_path):
    reset = _with_mgr(_mgr(tmp_path))
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        assert ctx.builder.build() == ""  # 无 section
        assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
    finally:
        reset(None)


def test_strategy_section_has_no_today(tmp_path):
    """策略 prompt 不含 today(today 移尾部 env)。"""
    reset = _with_mgr(_mgr(tmp_path))
    try:
        mgr = _mgr(tmp_path)
        reset(None)
        reset = _with_mgr(mgr)
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "长期记忆" in built
        # today 不在策略 prompt(已移 env)
        assert dt.date.today().isoformat() not in built
        assert "被动召回" not in built  # auto_inject 关
    finally:
        reset(None)


def test_auto_inject_user_md(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "姓名:张三\n偏好中文", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "被动召回" in built
        assert "张三" in built
        assert "USER.md" in built
    finally:
        reset(None)


def test_auto_inject_memory_md(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "项目用 Python 3.12", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "被动召回" in built
        assert "Python 3.12" in built
        assert "MEMORY.md" in built
    finally:
        reset(None)


def test_auto_inject_today_daily(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    today = dt.date.today().isoformat()
    mgr.write(f"daily_memory/{today}.md", "今日部署了 v1.2", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "v1.2" in built
        assert today in built
    finally:
        reset(None)


def test_auto_inject_truncates_when_over_cap(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 50)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "X" * 200, append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "截断" in built
        assert "memory_search" in built
    finally:
        reset(None)


def test_auto_inject_no_injectable_falls_back_to_strategy(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    mgr.write(f"daily_memory/{yesterday}.md", "yesterday note", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        built = ctx.builder.build()
        assert "长期记忆" in built
        assert "被动召回" not in built
    finally:
        reset(None)


def test_strategy_and_recall_are_separate_sections(tmp_path, monkeypatch):
    """策略 + 召回是两个独立 section(同名覆写,不堆叠);messages 不动。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "偏好中文", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        # memory_strategy(80) 在 memory_recall(81) 之前(build 按 priority 升序)
        built = ctx.builder.build()
        assert built.index("长期记忆") < built.index("被动召回")
        assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
    finally:
        reset(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: FAIL — 现实现 prepend 到 messages，`ctx.builder.build()` 为空

- [ ] **Step 3: Rewrite MemoryHook**

`twinkle/agentserver/hooks/builtin/memory_hook.py` 全文替换为：

```python
"""MemoryHook — before_model_call 注入长期记忆策略 + opt-in 被动召回,各为独立 builder section。

策略 prompt 常开(进 memory_strategy section,p80);opt-in(memory.auto_inject.enabled)时
附加被动召回段(进 memory_recall section,p81):把 USER.md + MEMORY.md + 今日 daily 注入,
模型不主动 memory_search 也能看到。两 section 同名覆写不堆叠;不碰 ctx.inputs.messages
(system 由 loop 端 builder.build() 统一拼)。策略 prompt 不含 today(已移尾部 <environment_context>)。
"""
from __future__ import annotations

import datetime

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

_PROMPT_TEMPLATE = """## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/<今日日期>.md", ...)(今日日期见下方环境信息)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""


class MemoryHook(AgentHook):
    priority = 80  # functional layer (50-99); below SkillHook(90)

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        if not mgr.list_files():
            return  # empty store → no-op
        strategy = _build_strategy()
        ctx.builder.add_section(PromptSection("memory_strategy", strategy, priority=80))
        recall = _build_auto_inject(mgr)
        if recall:
            ctx.builder.add_section(PromptSection("memory_recall", recall, priority=81))


def _build_strategy() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(mem_dir=MEMORY_DIR)


def _build_auto_inject(mgr) -> str:
    """被动召回:opt-in 时把 USER.md + MEMORY.md + 今日 daily 注入 memory_recall section。

    模型不主动 memory_search 也能看到长期记忆。开关关或无可注入文件 → 返回空串
    (before_model_call 只注策略 section)。超 max_chars 截断并提示用 memory_search。
    """
    from twinkle.config import MEMORY_AUTO_INJECT_ENABLED, MEMORY_AUTO_INJECT_MAX_CHARS
    if not MEMORY_AUTO_INJECT_ENABLED:
        return ""
    today = datetime.date.today().isoformat()
    sections: list[str] = []
    user_md = mgr.read("USER.md")
    if not user_md.startswith("Error:"):
        sections.append(f"### 用户画像（USER.md）\n{user_md}")
    mem_md = mgr.read("MEMORY.md")
    if not mem_md.startswith("Error:"):
        sections.append(f"### 持久事实（MEMORY.md）\n{mem_md}")
    daily = mgr.read(f"daily_memory/{today}.md")
    if not daily.startswith("Error:"):
        sections.append(f"### 今日记录（daily_memory/{today}.md）\n{daily}")
    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > MEMORY_AUTO_INJECT_MAX_CHARS:
        body = body[:MEMORY_AUTO_INJECT_MAX_CHARS] + "\n…[被动召回注入已截断,更多用 memory_search 查]"
    return "## 被动召回（自动注入的长期记忆）\n" + body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/memory_hook.py tests/test_memory_hook.py
git commit -m "refactor(memory_hook): strategy+recall as builder sections; remove today (env-tail)"
```

---

## Task 8: 接线 RuntimeEnvHook + member/subagent base_sections + 删 pre-seed

> 把 RuntimeEnvHook 加进三处 hook 列表；member/subagent 删 session_store system pre-seed、改用 `base_sections` 注入；subagent 删 `_system_prompt`。

**Files:**
- Modify: `twinkle/agentserver/server.py:102-109`
- Modify: `twinkle/agentserver/team/manager.py:111-150`
- Modify: `twinkle/agentserver/tools/builtin/subagent/executor.py:73-79, 86-99, 150-155`

- [ ] **Step 1: Wire RuntimeEnvHook into `create_agent`**

`twinkle/agentserver/server.py` `create_agent`（约 L87-91 import 区 + L102-109 all_hooks）。import 区加 `RuntimeEnvHook`：

```python
    from twinkle.agentserver.hooks.builtin import (
        SubagentContextHook, ContextCompressionHook,
        ContextOverflowRecoveryHook, MemoryFlushHook,
        RepeatToolCallDetectorHook, RuntimeEnvHook,
    )
```

`all_hooks` 列表加 `RuntimeEnvHook()`（priority 99,最先跑,放列表无所谓——HookManager 按 priority 排序）：

```python
    all_hooks = list(hooks or []) + [
        RuntimeEnvHook(),
        SubagentContextHook(executor),
        WorkflowContextHook(workflow_executor),
        ContextCompressionHook(llm=llm),
        MemoryFlushHook(llm=llm),
        ContextOverflowRecoveryHook(llm=llm),
        RepeatToolCallDetectorHook(),
    ]
```

- [ ] **Step 2: Member — delete pre-seed + inject base_sections + add RuntimeEnvHook**

`twinkle/agentserver/team/manager.py` `_build_member`（L111-150）：

```python
    async def _build_member(self, member_name: str, persona: str) -> "ReActAgent":
        """Build a ReActAgent customized for the given persona.

        Member identity (with persona) is injected via base_sections at
        construction; session_store no longer stores a system prompt.
        """
        from twinkle.agentserver.agent import (
            ReActAgent, member_base_sections)

        # 1. ToolManager filtered by MEMBER_TOOL_WHITELIST
        tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name in MEMBER_TOOL_WHITELIST:
                tm.register(t)

        # 2. Member session — no system pre-seed (identity via base_sections)
        member_sid = self._member_session_id(member_name)
        await self._store.create_session(member_sid)

        # 3. Build ReActAgent — inbox wired; member identity baked into base_sections.
        if member_name not in self._inboxes:
            self._inboxes[member_name] = MessageBox()
        inbox = self._inboxes[member_name]
        from twinkle.agentserver.hooks.builtin import RuntimeEnvHook
        hooks = [RuntimeEnvHook(), SkillHook(), MemoryHook(), MemoryFlushHook(llm=self._llm),
                 LoggingHook(), RetryHook()]
        return ReActAgent(
            self._llm, self._store, tm,
            hooks=tuple(hooks),
            max_steps=SUBAGENT_MAX_STEPS,
            inbox=inbox,
            base_sections=member_base_sections(
                persona=persona, workspace=str(self.workspace), member_name=member_name),
        )
```

> 注意：`build_member_system_prompt` 的 import（原 L118）换成 `member_base_sections`。`SkillHook`/`MemoryHook`/`MemoryFlushHook`/`LoggingHook`/`RetryHook` 已在 manager.py 顶部 import（确认；若 RuntimeEnvHook 未在顶部 import，用函数内 `from ... import RuntimeEnvHook` 如上）。

- [ ] **Step 3: Subagent — delete pre-seed + inject base_sections + add RuntimeEnvHook + delete `_system_prompt`**

`twinkle/agentserver/tools/builtin/subagent/executor.py`：

(a) `_hook_list`（L86-90）加 `RuntimeEnvHook`：

```python
    def _hook_list(self) -> list["AgentHook"]:
        if self._child_hooks is not None:
            return self._child_hooks
        from twinkle.agentserver.hooks.builtin import RuntimeEnvHook
        return [RuntimeEnvHook(), SkillHook(), MemoryHook(), MemoryFlushHook(llm=self._llm),
                LoggingHook(), RetryHook()]
```

(b) `_build_child_agent`（L94-99）传 `base_sections`：

```python
    def _build_child_agent(self) -> "ReActAgent":
        from twinkle.agentserver.agent import ReActAgent, normal_base_sections  # lazy: avoid circular
        from twinkle.agentserver.prompts import PromptSection
        tool_manager = self._build_tool_manager()
        return ReActAgent(self._llm, self._store, tool_manager,
                          hooks=tuple(self._hook_list()),
                          max_steps=self._config.max_steps,
                          base_sections=normal_base_sections()
                          + [PromptSection("subagent_addendum", _SUBAGENT_ADDENDUM, priority=15)])
```

(c) 删 `_system_prompt` 方法（L73-79）——不再被引用（pre-seed 删除后无调用点）。

(d) 删 pre-seed 调用（L152-155 区域，`_drive_child` 或 `_inner_run_stream` 内 `await self._store.append(session_id, {"role": "system", "content": self._system_prompt()})` 这一行）。保留 `create_session`。

> 精确定位：`grep -n "_system_prompt\|append.*system" twinkle/agentserver/tools/builtin/subagent/executor.py` 应只剩历史注释。删 L154 的 append system 行；若 `_system_prompt` 在别处无引用（grep 确认），整段删 L73-79。

- [ ] **Step 4: Verify no orphan references to deleted methods**

Run: `python -m pytest tests/test_subagent.py -v 2>&1 | head -40`（若该文件存在；否则跑 `grep -rn "_system_prompt\|build_member_system_prompt" twinkle/ tests/` 确认无残留调用）

Expected: 无 `AttributeError: _system_prompt` 或 `ImportError: build_member_system_prompt`（注意 `build_member_system_prompt` 仍被 `member_base_sections` 和 `test_team.py` 调用 → 保留函数本身，只删 manager.py 对它的直接 import 调用）。

- [ ] **Step 5: Run team + subagent tests**

Run: `python -m pytest tests/test_team.py tests/test_subagent.py -v`
Expected: PASS（member prompt 结构断言已 Task 4 更新；subagent 黑盒断言不查 system pre-seed）

> 若 `test_subagent.py` 断言子 session 含 system 头，改为断言"发给 LLM 的 messages[0] 是 system 且含身份/子 agent 角色"。

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/server.py twinkle/agentserver/team/manager.py twinkle/agentserver/tools/builtin/subagent/executor.py
git commit -m "feat(wiring): RuntimeEnvHook in all hook chains; member/subagent identity via base_sections (no session_store system seed)"
```

---

## Task 9: 全量回归 + smoke

**Files:** 无（仅验证）

- [ ] **Step 1: 全套测试**

Run: `python -m pytest tests/ -q`
Expected: 无新增失败。基线已知 16 个 pre-existing 失败（15 cron + 1 pptx，见 memory `phase6-cron-tests-environmental-failures`），不属本改动。任何**其它**失败必须排查。

- [ ] **Step 2: 针对性回归受影响面**

Run: `python -m pytest tests/test_prompts.py tests/test_runtime_env_hook.py tests/test_skill_hook.py tests/test_memory_hook.py tests/test_team.py tests/test_agent_loop.py tests/test_agent_loop_context_assembly.py tests/test_compression.py tests/test_compression_hook.py -v`
Expected: 全 PASS（compression 未改但验证未回归）

- [ ] **Step 3: smoke 启服务不崩**

Run: `python -m twinkle.agentserver`（Ctrl+C 停）
Expected: 启动日志正常，无 `AttributeError: builder` / `ImportError` / `TypeError: __init__() got unexpected base_sections`。

- [ ] **Step 4: 最终 commit（若有 test 修正残留）**

```bash
git add tests/
git commit -m "test: fix regressions from context-assembly refactor"
```

---

## Self-Review

**1. Spec coverage**（逐条对照 spec `2026-08-17-context-assembly-kvcache-alignment-design.md`）：

- §1 SystemPromptBuilder（prompts.py）→ Task 1 ✓
- §2 section 划分 + env-at-tail → Task 4（env-strip + base_sections 工厂, single-section-per-mode 偏离已记于"关键设计决策"2）+ Task 3（RuntimeEnvHook）+ Task 5（loop 拼尾部 UserMessage）✓
- §3 hook 改造 + 砍 merge → Task 6（SkillHook add_section）+ Task 7（MemoryHook add_section x2 + 去 today）+ Task 5（删 _merge_system_messages）✓；ContextCompressionHook 不动（spec §3：summary 保留为独立 system msg,不进 builder）→ 未改 compression,验证 Task 9 ✓；RepeatToolCallDetectorHook 不动（spec §3）→ 未改 ✓
- §3a loop 流程 + session_store 不存 system → Task 5（删 session_store system append + 每步 builder 重建）+ Task 8（member/subagent 删 pre-seed）✓；压缩 head 语义变化（无 system head）→ compression 已处理空 head,Task 9 验证 ✓
- §4 env-at-tail 消费链 → Task 3（RuntimeEnvHook 注 ctx.extra）+ Task 5（loop pop + 拼 `<environment_context>` UserMessage）✓
- 改动文件清单 7 项 → 本计划覆盖:prompts.py(✓Task1)、agent.py(✓Task4/5)、skill_hook(✓Task6)、memory_hook(✓Task7)、runtime_env_hook(✓Task3)、hooks/base.py(✓Task2)、hooks/builtin/__init__.py + server(✓Task3/8)。**额外**:manager.py + executor.py（spec §3a 的"session_store 不存 system"要求 member/subagent 也删 pre-seed,spec 改动文件清单未列但逻辑必需——本计划补全）。

**2. Placeholder scan**：无 TBD/TODO；每步含完整代码；测试用例完整可跑。`Task 8 Step 3(d)` 的 pre-seed 删除用 grep 定位（因确切行号待 Step 时确认），但给了精确 grep 命令 + 删除语义——非占位。

**3. Type consistency**：
- `PromptSection(name, content, priority)` ——Task 1 定义,Task 4/5/6/7/8 全用此构造顺序 ✓
- `SystemPromptBuilder.add_section(section)` / `.build()` / `.remove_section(name)` ——Task 1 定义,Task 2/5/6/7 一致 ✓
- `ctx.builder` ——Task 2 加字段,Task 5/6/7/8 一致读取 ✓
- `ctx.extra["environment_context"]` list of `{"content","source"}` ——Task 3 产,Task 5 消费一致 ✓
- `base_sections: list[PromptSection] | None` ——Task 4 工厂返回 `list[PromptSection]`,Task 5 `__init__` 签名 + loop 读取,Task 8 member/subagent 注入一致 ✓
- section name 一致性：`"system_prompt"`(base,p10) / `"skills"`(p90) / `"memory_strategy"`(p80) / `"memory_recall"`(p81) / `"subagent_addendum"`(p15) ——Task 4/6/7/8 一致 ✓
- `RuntimeEnvHook.priority == 99` ——Task 3 定义,Task 3 测试 + spec §4 一致 ✓

**类型/签名风险点**：`SessionStore` 构造签名（Task 5 测试用 `SessionStore(str(tmp_path/"db.json"))`）——若与实际不符,Step 1 注释已指示按 `tests/test_agent_loop.py` fixture 风格调整。`MemoryManager` 构造（test_memory_hook `_mgr` 用 `embed_provider=None`）——沿用现有 test_memory_hook 签名 ✓。

---

## 执行选择

**Plan complete and saved to `docs/superpowers/plans/2026-08-18-context-assembly-kvcache-alignment.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 我每个 Task 派一个 fresh subagent 实现,两阶段 review（spec 合规 → 代码质量）,任务间不停顿。

**2. Inline Execution** — 本会话内按 executing-plans 批量执行,checkpoint 处 review。

**选哪个？**
