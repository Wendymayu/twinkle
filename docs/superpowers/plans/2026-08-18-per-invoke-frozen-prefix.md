# Per-Invoke Frozen Prefix + Memory 静态化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 skills / USER.md·MEMORY.md / tools 从 per-step 重算改为 per-invoke 一次冻结，使 `builder.build()` 跨步字节稳定，provider 自动 prefix cache 真正命中。

**Architecture:** 注入机制 A——`ctx.extra["frozen_sections"]`。`BEFORE_INVOKE` hooks（SkillHook/MemoryHook）把稳定 `PromptSection` 追加到该 list（此时 builder 尚未创建）；`_run_react_loop` 每步套用该 list 到新建的 builder。tools 在 loop 顶部算一次 `schemas()` + team 过滤，for-step 内复用。memory 拆 `memory_strategy`（常开）+ `memory_static`（USER.md+MEMORY.md，opt-in 默认开，无 daily）；`memory_recall` 删；daily 改走 `memory_search`（tool message = 动态区）。

**Tech Stack:** Python 3.12 / asyncio / pytest / pytest-asyncio（`asyncio.run` 风格，非 async fixture）/ pydantic-settings（config schema）。

**Spec:** `docs/superpowers/specs/2026-08-18-per-invoke-frozen-prefix-design.md`

**前置事实（已验证，供执行者参考，不必再查）:**
- `agent.py:423-432`：`run()` 建 `ctx`（`event=BEFORE_INVOKE`, `inputs=InvokeInputs`, `extra={}`）→ `execute(BEFORE_INVOKE, ctx)` → `async for frame in self._run_react_loop(ctx, request)`。同一 `ctx` 贯穿，`ctx.extra` 跨 step 持久（每步只重赋 `ctx.inputs`/`ctx.builder`）。
- `agent.py:489` `is_team_mode = request.mode == "team"`；`agent.py:499` `for _step in range(self._max_steps):`；`agent.py:507-510` 每步 `tool_schemas = self._tool_manager.schemas()` + team 过滤；`agent.py:513` `builder = SystemPromptBuilder()`；`agent.py:514-521` base sections 选择 + `for sec in base: builder.add_section(sec)`；`agent.py:522` `ctx.builder = builder`；`agent.py:524` `ModelCallInputs(messages=msgs, tools=tool_schemas)`；`agent.py:528-531` 注 `builder.build()` 为首条 system。
- `base.py:188` `HookContext.extra: dict`（已存在，无需新字段）；`base.py:72` `before_invoke(self, ctx)` 是 AgentHook 默认 no-op 生命周期回调，override 即被 `get_callbacks()` 收集。
- `manager.py:31-42` `schemas()` 每次新建 list，无缓存无 I/O；team 过滤（509-510）建新 list 不 mutate 原 list → 复用 frozen list 安全。
- 工具 register/unregister 调用点：仅 `tools/__init__.py`（启动期 `tool_manager()` 工厂）+ `tools/builtin/subagent/executor.py:71`（subagent 自建 ToolManager，独立于父 agent）。**无 hook 在父 agent invoke 内对父的 ToolManager 做 register/unregister** → 父 agent per-invoke 冻结 tools 安全。
- SkillHook/MemoryHook 在三条路径都注册：`server.py:243`（normal）、`team/manager.py:137`（team）、`subagent/executor.py:82`（subagent），三者都是 ReActAgent，都过同一 `run()`→`_run_react_loop` → frozen_sections 机制对所有 mode 生效。
- `config/__init__.py:53` `MEMORY_AUTO_INJECT_ENABLED = settings.memory.auto_inject.enabled`；`config/schema.py:127-129` `MemoryAutoInjectConfig.enabled: bool = False`；`config.yaml:65-66` `auto_inject:\n  enabled: false`。
- `prompts.py` `PromptSection(name, content, priority)` + `SystemPromptBuilder.add_section`（同名覆写不堆叠）+ `build()`（priority 升序 join）。

**测试约定（沿用现有风格）:** `asyncio.run(...)` + `async for _frame in agent.run(req)`；`session_store` fixture（conftest）；`_ScriptedLLM(scripts)` 按序吐事件；`_reg_with_echo_tool()` 注册真 echo tool；hook 单测用 `HookContext(...)` 直构 + `asyncio.run(hook.before_invoke(ctx))`。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `twinkle/agentserver/hooks/builtin/skill_hook.py` | SkillHook：before_invoke 把 skill 清单 stash 到 `ctx.extra["frozen_sections"]` | 改（近全改写） |
| `twinkle/agentserver/hooks/builtin/memory_hook.py` | MemoryHook：before_invoke 注 strategy + memory_static；删 recall；prompt 加 daily-search 提示 | 改（近全改写） |
| `twinkle/agentserver/agent.py` | `_run_react_loop`：tools 冻结移出 for-loop + frozen_sections 套用 | 改（2 处 Edit） |
| `twinkle/config/schema.py` | `MemoryAutoInjectConfig.enabled` 默认 `False`→`True` | 改（1 行） |
| `twinkle/resources/config.yaml` | `memory.auto_inject.enabled` `false`→`true` + 注释更新 | 改（1 行） |
| `tests/test_skill_hook.py` | 断言 before_invoke + `ctx.extra["frozen_sections"]` | 改（近全改写） |
| `tests/test_memory_hook.py` | 断言 memory_strategy + memory_static（无 daily）+ before_invoke | 改（近全改写） |
| `tests/test_agent_loop_context_assembly.py` | frozen_sections 套用 + 跨步字节稳定 + tools 冻结一次 | 改（追加 helpers + 3 测试） |
| `tests/test_config_defaults.py` | `MemoryAutoInjectConfig.enabled` 默认 True | 新建 |

---

### Task 1: SkillHook — before_model_call → before_invoke + frozen_sections

**Files:**
- Modify: `tests/test_skill_hook.py`（近全改写）
- Modify: `twinkle/agentserver/hooks/builtin/skill_hook.py:25-40`

- [ ] **Step 1: 改写测试为 before_invoke + frozen_sections 断言（RED）**

把 `tests/test_skill_hook.py` 全量替换为：

```python
import asyncio
from pathlib import Path
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.skills import _set_skill_manager, SkillManager


def _make_skill(dir_: Path, name: str, desc: str) -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def _ctx(query="hi") -> HookContext:
    """before_invoke 时 builder 尚不存在(每步在 loop 里才建);inputs 是 InvokeInputs。"""
    return HookContext(
        agent=None, event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query=query, mode=""),
        session_id="s", request_id="r",
    )


def _skills_section(ctx: HookContext) -> PromptSection | None:
    secs = ctx.extra.get("frozen_sections", [])
    return next((s for s in secs if s.name == "skills"), None)


@pytest.fixture
def isolated_skills(tmp_path):
    _make_skill(tmp_path / "a", "a", "desc a")
    _make_skill(tmp_path / "b", "b", "desc b")
    _set_skill_manager(SkillManager(str(tmp_path)))
    yield tmp_path
    _set_skill_manager(None)


def test_all_mode_stashes_catalog_section(isolated_skills):
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    sec = _skills_section(ctx)
    assert sec is not None
    assert sec.priority == 90
    assert "## 可用技能" in sec.content
    assert "a: desc a" in sec.content
    assert "b: desc b" in sec.content


def test_auto_list_mode_stashes_note_section(isolated_skills):
    hook = SkillHook(mode="auto_list")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    sec = _skills_section(ctx)
    assert sec is not None
    assert "list_skill" in sec.content


def test_stashes_exactly_one_skills_section(isolated_skills):
    """同名覆写语义在 frozen_sections 上仍只留一条 skills section。"""
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    skills_secs = [s for s in ctx.extra.get("frozen_sections", []) if s.name == "skills"]
    assert len(skills_secs) == 1


def test_no_skills_is_noop(tmp_path):
    _set_skill_manager(SkillManager(str(tmp_path)))  # 空目录
    try:
        ctx = _ctx()
        asyncio.run(SkillHook(mode="all").before_invoke(ctx))
        assert "frozen_sections" not in ctx.extra  # 无 skill → 不 stash
    finally:
        _set_skill_manager(None)


def test_before_invoke_does_not_touch_builder(isolated_skills):
    """before_invoke 在 builder 存在前跑;不应碰 ctx.builder(仍为 None)。"""
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    assert ctx.builder is None
```

- [ ] **Step 2: 运行测试，确认 RED**

Run: `python -m pytest tests/test_skill_hook.py -v`
Expected: FAIL — `AttributeError: 'SkillHook' object has no attribute 'before_invoke'`（当前只有 `before_model_call`）。

- [ ] **Step 3: 实现 SkillHook.before_invoke**

把 `twinkle/agentserver/hooks/builtin/skill_hook.py` 全量替换为：

```python
"""SkillHook — before_invoke 注入 skill 清单/提示到 ctx.extra["frozen_sections"]。

all 模式:把全部 skill name+desc 拼成 section stash(每步由 loop 套用到 builder,跨步稳定)。
auto_list 模式:只 stash 一句"调 list_skill"提示(模型要时自己拉清单)。
无 skills → no-op。注入走 ctx.extra["frozen_sections"](loop 每步 add_section),
hook 不碰 messages/builder(before_invoke 时 builder 尚不存在)。
mode 传 None 时从 config 读 SKILL_MODE(生产用),测试可直传 mode。
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

log = logging.getLogger("twinkle.hooks.skill")


class SkillHook(AgentHook):
    priority = 90  # 功能层(50-99);before_invoke,与 PermissionHook(before_tool_call)不同事件

    def __init__(self, mode: str | None = None) -> None:
        self._mode = mode  # None → 调用时从 config 读

    async def before_invoke(self, ctx: HookContext) -> None:
        from twinkle.agentserver.skills import get_skill_manager
        skills = get_skill_manager().list_skills()
        if not skills:
            return  # 无 skill → no-op(不创建 frozen_sections key)
        mode = self._mode or _get_skill_mode()
        if mode == "auto_list":
            content = "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。"
        else:  # "all"(默认);未知 mode 也落到 all 并告警,避免静默误配置
            if mode != "all":
                log.warning("unknown SKILL_MODE %r, falling back to 'all'", mode)
            lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
            content = "\n".join(lines)
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("skills", content, priority=90))


def _get_skill_mode() -> str:
    from twinkle.config import SKILL_MODE
    return SKILL_MODE
```

- [ ] **Step 4: 运行测试，确认 GREEN**

Run: `python -m pytest tests/test_skill_hook.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 跑现有 agent loop 测试确认无回归（SkillHook 旧 before_model_call 调用点已不存在）**

Run: `python -m pytest tests/test_agent_loop.py tests/test_agent_loop_context_assembly.py -v`
Expected: PASS（SkillHook 不在这两个文件的 agent 构造里；若 PASS 说明无依赖旧 before_model_call 的测试）。

- [ ] **Step 6: Commit**

```bash
git add tests/test_skill_hook.py twinkle/agentserver/hooks/builtin/skill_hook.py
git commit -m "refactor(skill_hook): before_model_call→before_invoke, stash to ctx.extra[frozen_sections]

skills section per-invoke 冻结(跨步稳定),对齐 jiuwenswarm。loop 套用在 Task 3 落地。"
```

---

### Task 2: MemoryHook — before_invoke + memory_strategy + memory_static（无 daily）

**Files:**
- Modify: `tests/test_memory_hook.py`（近全改写）
- Modify: `twinkle/agentserver/hooks/builtin/memory_hook.py`（全量替换）

- [ ] **Step 1: 改写测试为 before_invoke + memory_static 断言（RED）**

把 `tests/test_memory_hook.py` 全量替换为：

```python
"""MemoryHook 测试——before_invoke 注 memory_strategy(常开)+ memory_static(opt-in,USER.md+MEMORY.md,无 daily)。

覆盖:空 store no-op、策略-only(开关关)、USER.md 注入、MEMORY.md 注入、daily 不进前缀、
超 cap 截断、开关开但无 injectable 文件(只有昨日 daily)回退策略-only。
daily 不再自动注入——需 daily 时 memory_search('daily_memory/<日期>')。沿用 asyncio.run 风格。
"""
import asyncio
import datetime as dt

from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.prompts import PromptSection


def _ctx(query="hi") -> HookContext:
    """before_invoke 时 builder 尚不存在;inputs 是 InvokeInputs。"""
    return HookContext(
        agent=None, event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query=query, mode=""),
        session_id="s", request_id="r",
    )


def _mgr(tmp_path, **kw):
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)


def _run(hook, ctx):
    asyncio.run(hook.before_invoke(ctx))


def _with_mgr(mgr):
    """测试期间设置单例;调用方须在之后 _set(None)。"""
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(mgr)
    return _set_memory_manager


def _section(ctx, name: str) -> PromptSection | None:
    secs = ctx.extra.get("frozen_sections", [])
    return next((s for s in secs if s.name == name), None)


def test_empty_store_noop(tmp_path):
    """空 store → 不注入(frozen_sections 无 key)。"""
    reset = _with_mgr(_mgr(tmp_path))
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        assert "frozen_sections" not in ctx.extra  # 空 store → no-op,不 stash
    finally:
        reset(None)


def test_strategy_only_when_auto_inject_disabled(tmp_path, monkeypatch):
    """auto_inject 关 → 只 strategy section,无 memory_static;env(today)不进策略。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", False)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "用户偏好中文", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        strat = _section(ctx, "memory_strategy")
        assert strat is not None
        assert "长期记忆" in strat.content
        assert "memory_search('daily_memory" in strat.content  # 新提示:需 daily 用 search
        assert dt.date.today().isoformat() not in strat.content  # 今日日期不进 prefix
        assert _section(ctx, "memory_static") is None  # 开关关 → 无 static
    finally:
        reset(None)


def test_auto_inject_user_md(tmp_path, monkeypatch):
    """auto_inject 开 + USER.md → memory_static 含 USER.md 内容。"""
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
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "张三" in static.content
        assert "USER.md" in static.content
    finally:
        reset(None)


def test_auto_inject_memory_md(tmp_path, monkeypatch):
    """auto_inject 开 + MEMORY.md → memory_static 含 MEMORY.md 内容(持久事实)。"""
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
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "Python 3.12" in static.content
        assert "MEMORY.md" in static.content
    finally:
        reset(None)


def test_daily_excluded_from_static(tmp_path, monkeypatch):
    """daily 不进 memory_static(只 USER.md+MEMORY.md);需 daily 用 memory_search。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    today = dt.date.today().isoformat()
    mgr.write("USER.md", "姓名:张三", append=True)
    mgr.write(f"daily_memory/{today}.md", "今日部署了 v1.2", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "张三" in static.content        # USER.md 在
        assert "v1.2" not in static.content   # daily 内容不进 prefix
        assert today not in static.content     # daily 日期不进 prefix
        strat = _section(ctx, "memory_strategy")
        assert "memory_search('daily_memory" in strat.content  # 策略提示去搜 daily
    finally:
        reset(None)


def test_auto_inject_truncates_when_over_cap(tmp_path, monkeypatch):
    """内容超 max_chars → memory_static 追加截断标记 + 提示用 memory_search。"""
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
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "截断" in static.content
        assert "memory_search" in static.content
    finally:
        reset(None)


def test_auto_inject_no_injectable_falls_back_to_strategy(tmp_path, monkeypatch):
    """auto_inject 开但只有昨日 daily(不在注入列表)→ 只 strategy,无 static。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    mgr.write(f"daily_memory/{yesterday}.md", "yesterday note", append=True)  # 昨日,不注入
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        assert _section(ctx, "memory_strategy") is not None
        assert _section(ctx, "memory_static") is None  # 无 USER/MEMORY → 无 static
    finally:
        reset(None)
```

- [ ] **Step 2: 运行测试，确认 RED**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: FAIL — `MemoryHook` 仍只有 `before_model_call`，`before_invoke` 是基类 no-op → 各 `assert _section(...) is not None` 失败（frozen_sections 空）。

- [ ] **Step 3: 实现 MemoryHook.before_invoke + memory_static（删 recall）**

把 `twinkle/agentserver/hooks/builtin/memory_hook.py` 全量替换为：

```python
"""MemoryHook — before_invoke 注 strategy + opt-in 静态召回(USER.md/MEMORY.md)到 ctx.extra["frozen_sections"]。

No-op when the memory store is empty. 注入走 ctx.extra["frozen_sections"](loop 每步套用到 builder):
- memory_strategy(priority 80):何时搜/写的策略 prompt(稳定,常开;提示需 daily 时 memory_search)。
- memory_static(priority 81, opt-in):USER.md + MEMORY.md 被注入(读一次/invoke,无 daily)。
daily 不再自动注入——需 daily 时 memory_search('daily_memory/<日期>')(= tool message = 动态区)。
"""
from __future__ import annotations

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

_PROMPT_TEMPLATE = """## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。
需要今日/昨日记录时,先 memory_search('daily_memory/<日期>')(今日日期见下方环境信息)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/<今日日期>.md", ...)(今日日期见下方环境信息)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""


class MemoryHook(AgentHook):
    priority = 80  # functional layer (50-99); below SkillHook(90)

    async def before_invoke(self, ctx: HookContext) -> None:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        if not mgr.list_files():
            return  # empty store → no-op
        frozen = ctx.extra.setdefault("frozen_sections", [])
        frozen.append(PromptSection("memory_strategy", _build_prompt(), priority=80))
        static = _build_static(mgr)
        if static:
            frozen.append(PromptSection("memory_static", static, priority=81))


def _build_prompt() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(mem_dir=MEMORY_DIR)


def _build_static(mgr) -> str:
    """opt-in 时把 USER.md + MEMORY.md 注入(读一次/invoke,无 daily)。

    开关关或无可注入文件 → 返回空串(只注策略)。超 max_chars 截断并提示用 memory_search。
    daily 不再自动注入——需要时模型 memory_search('daily_memory/<日期>')。
    """
    from twinkle.config import MEMORY_AUTO_INJECT_ENABLED, MEMORY_AUTO_INJECT_MAX_CHARS
    if not MEMORY_AUTO_INJECT_ENABLED:
        return ""
    sections: list[str] = []
    user_md = mgr.read("USER.md")
    if not user_md.startswith("Error:"):
        sections.append(f"### 用户画像（USER.md）\n{user_md}")
    mem_md = mgr.read("MEMORY.md")
    if not mem_md.startswith("Error:"):
        sections.append(f"### 持久事实（MEMORY.md）\n{mem_md}")
    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > MEMORY_AUTO_INJECT_MAX_CHARS:
        body = body[:MEMORY_AUTO_INJECT_MAX_CHARS] + "\n…[静态注入已截断,更多用 memory_search 查]"
    return "## 被动召回（自动注入的长期记忆）\n" + body
```

- [ ] **Step 4: 运行测试，确认 GREEN**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: PASS（7 passed）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_hook.py twinkle/agentserver/hooks/builtin/memory_hook.py
git commit -m "refactor(memory_hook): before_invoke 注 strategy+memory_static, 删 recall, 无 daily

memory_strategy 常开(加 daily 用 memory_search 提示);memory_static(opt-in)注 USER.md+MEMORY.md,
无 daily(改走 memory_search=动态区)。per-invoke 冻结,跨步稳定。loop 套用在 Task 3 落地。"
```

---

### Task 3: agent.py loop — 冻结 tools + 套用 frozen_sections

**Files:**
- Modify: `tests/test_agent_loop_context_assembly.py`（追加 helpers + 3 测试）
- Modify: `twinkle/agentserver/agent.py`（2 处 Edit：tools 冻结移出 for-loop；frozen_sections 套用）

- [ ] **Step 1: 追加 3 个测试（RED）**

在 `tests/test_agent_loop_context_assembly.py` 末尾追加（保留现有 `_FinishLLM`/`_TM`/`_make_agent` 及现有 3 测试不动）。需新增 imports + helpers + 3 测试：

在文件顶部 import 区追加（`from twinkle.agentserver.llm_client import Finish` 已有；补 `AgentHook`/`PromptSection`/`tool`/`ToolManager`）：

```python
from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
```

在文件末尾追加：

```python
# --- per-invoke frozen prefix tests --- #


class _MarkerHook(AgentHook):
    """before_invoke: stash a marker section to frozen_sections (simulates SkillHook/MemoryHook)."""
    async def before_invoke(self, ctx: HookContext) -> None:
        ctx.extra.setdefault("frozen_sections", []).append(
            PromptSection("marker", "MARKER-TOKEN-XYZ", priority=50))


def _reg_with_echo_tool():
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    m = ToolManager()
    m.register(echo)
    return m


class _ScriptedLLM:
    """Returns one canned event-list per stream() call, in order; captures system msg each call."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.seen_systems: list[str] = []

    async def stream(self, messages, tools):
        self.seen_systems.append(messages[0]["content"])
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


class _CountingTM:
    """Wraps a ToolManager; counts schemas() calls to verify per-invoke freeze."""
    def __init__(self, inner):
        self._inner = inner
        self.schemas_calls = 0

    def schemas(self):
        self.schemas_calls += 1
        return self._inner.schemas()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_frozen_sections_applied_to_builder(tmp_path):
    """before_invoke stashed section → loop 每步套用到 builder → builder.build() 含它。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = _make_agent(store, base_sections=normal_base_sections(), hooks=[_MarkerHook()])
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert "MARKER-TOKEN-XYZ" in agent._llm.captured[0]["content"]


def test_frozen_sections_byte_stable_across_steps(tmp_path):
    """frozen_sections 每步套用 + system prefix 跨步字节一致。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    reg = _reg_with_echo_tool()
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    agent = ReActAgent(llm, store, reg, hooks=(_MarkerHook(),),
                       base_sections=normal_base_sections(), max_steps=3)
    req = AgentRequest(session_id="s1", request_id="r1", query="call echo")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert llm.calls == 2
    assert "MARKER-TOKEN-XYZ" in llm.seen_systems[0]
    assert "MARKER-TOKEN-XYZ" in llm.seen_systems[1]
    assert llm.seen_systems[0] == llm.seen_systems[1]  # byte-stable across steps


def test_tool_schemas_frozen_once_per_invoke(tmp_path):
    """tool_schemas 在 invoke 内只算一次(for-loop 复用),不每步重建。"""
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    tm = _CountingTM(_reg_with_echo_tool())
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    agent = ReActAgent(llm, store, tm, base_sections=normal_base_sections(), max_steps=3)
    req = AgentRequest(session_id="s1", request_id="r1", query="call echo")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    assert llm.calls == 2            # 跑了 2 步
    assert tm.schemas_calls == 1     # 但 schemas() 只调一次 = 冻结
```

- [ ] **Step 2: 运行新测试，确认 RED**

Run: `python -m pytest tests/test_agent_loop_context_assembly.py -v -k "frozen_sections or tool_schemas"`
Expected: 
- `test_frozen_sections_applied_to_builder` FAIL — `"MARKER-TOKEN-XYZ" not in ...`（loop 还没套用 frozen_sections）。
- `test_frozen_sections_byte_stable_across_steps` FAIL — 同上。
- `test_tool_schemas_frozen_once_per_invoke` FAIL — `tm.schemas_calls == 2`（每步重建，实际调 2 次）。

- [ ] **Step 3: 实现 Edit A — tools 冻结移出 for-loop**

Edit `twinkle/agentserver/agent.py`，把 tools 计算 + team 过滤从 for-loop 内移到 for-loop 前。

old_string（精确匹配 `agent.py` 现状）：
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
```

new_string：
```python
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
```

- [ ] **Step 4: 实现 Edit B — loop 套用 frozen_sections**

Edit `twinkle/agentserver/agent.py`，在 base sections 套用后追加 frozen_sections 套用。

old_string：
```python
            for sec in base:
                builder.add_section(sec)
            ctx.builder = builder
```

new_string：
```python
            for sec in base:
                builder.add_section(sec)
            # per-invoke 冻结段(before_invoke hooks 如 SkillHook/MemoryHook 注入,跨步稳定)
            for sec in ctx.extra.get("frozen_sections", []):
                builder.add_section(sec)
            ctx.builder = builder
```

- [ ] **Step 5: 运行新测试，确认 GREEN**

Run: `python -m pytest tests/test_agent_loop_context_assembly.py -v -k "frozen_sections or tool_schemas"`
Expected: PASS（3 passed）。

- [ ] **Step 6: 运行整个 context_assembly + agent_loop 套件确认无回归**

Run: `python -m pytest tests/test_agent_loop_context_assembly.py tests/test_agent_loop.py tests/test_agent_loop_with_hooks.py tests/test_agent_loop_compress.py tests/test_agent_loop_failure.py -v`
Expected: PASS（tools 冻结对 team/parallel/compress/failure 路径无副作用；frozen_sections 默认空 list 对无 hook 路径 no-op）。

- [ ] **Step 7: Commit**

```bash
git add tests/test_agent_loop_context_assembly.py twinkle/agentserver/agent.py
git commit -m "refactor(agent): 冻结 tools per-invoke + loop 套用 frozen_sections

tools schemas() 移出 for-loop(一 invoke 一次);base sections 后套用 ctx.extra[frozen_sections]
(before_invoke hooks 注入,跨步稳定)。builder.build() 跨步字节稳定 → provider 自动 prefix cache。"
```

---

### Task 4: config — MEMORY_AUTO_INJECT_ENABLED 默认 True

**Files:**
- Create: `tests/test_config_defaults.py`
- Modify: `twinkle/config/schema.py:128`
- Modify: `twinkle/resources/config.yaml:66`

- [ ] **Step 1: 写失败测试（RED）**

新建 `tests/test_config_defaults.py`：

```python
"""config 默认值断言——per-invoke frozen prefix 后 memory auto-inject 默认开。"""


def test_memory_auto_inject_default_enabled():
    """Schema 默认 auto_inject.enabled=True(被动召回 USER.md/MEMORY.md 默认开)。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    assert MemoryAutoInjectConfig().enabled is True


def test_memory_auto_inject_max_chars_default():
    """max_chars 默认不变(12000)。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    assert MemoryAutoInjectConfig().max_chars == 12000
```

- [ ] **Step 2: 运行测试，确认 RED**

Run: `python -m pytest tests/test_config_defaults.py -v`
Expected: FAIL — `assert False is True`（schema 默认仍 `False`）。

- [ ] **Step 3: 翻 schema 默认**

Edit `twinkle/config/schema.py`。

old_string：
```python
    enabled: bool = False    # 被动召回开关（opt-in；默认关=维持 5a "只注入策略 prompt"）
```

new_string：
```python
    enabled: bool = True     # 被动召回开关（默认开=before_invoke 注 USER.md+MEMORY.md 进 prefix；关=只策略 prompt）
```

- [ ] **Step 4: 翻 config.yaml + 注释更新**

Edit `twinkle/resources/config.yaml`。

old_string（line 66，整行精确匹配）：
```
    enabled: false                # 被动召回(opt-in):每步 before_model_call 把 USER.md + MEMORY.md + 今日 daily 注入 system prompt(顺序 USER→MEMORY→daily,cap 超限截断),模型不主动 memory_search 也能看到。默认关=维持 5a 只注入策略
```

new_string：
```
    enabled: true                 # 被动召回(默认开):before_invoke 一次把 USER.md + MEMORY.md 注入 system prefix(无 daily;daily 用 memory_search 召回=动态区),cap 超限截断。对齐 jiuwenswarm 跨步稳定 prefix
```

- [ ] **Step 5: 运行测试，确认 GREEN**

Run: `python -m pytest tests/test_config_defaults.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 6: 跑 memory_hook 测试确认 monkeypatch 语义仍成立**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: PASS（monkeypatch 显式设 True/False 仍覆盖默认；`test_strategy_only_when_auto_inject_disabled` 显式关 → 只 strategy；其余显式开 → 含 static）。

- [ ] **Step 7: Commit**

```bash
git add tests/test_config_defaults.py twinkle/config/schema.py twinkle/resources/config.yaml
git commit -m "chore(config): memory auto_inject 默认 false→true

USER.md+MEMORY.md 默认注入 system prefix(无 daily)。schema + config.yaml 同翻。"
```

---

### Task 5: 全量回归 + smoke

**Files:** 无（验证 only）

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: PASS（全绿）。若 `test_team.py` / `test_subagent` 等断 frozen_sections/member 路径失败，按 spec「改动文件」表「可能改」处理：member/subagent 同过 `run()`→`_run_react_loop`，frozen_sections 对其亦生效，断言若硬编码 system 内容需同步。

- [ ] **Step 2: smoke —— 导入 + 构造 agent 不崩**

Run:
```bash
python -c "from twinkle.agentserver.agent import ReActAgent, normal_base_sections; from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook; from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook; print('import ok')"
```
Expected: `import ok`（无 ImportError / 语法错）。

- [ ] **Step 3: 确认 SkillHook/MemoryHook 无 before_model_call 残留（grep）**

Run: `grep -rn "before_model_call" twinkle/agentserver/hooks/builtin/skill_hook.py twinkle/agentserver/hooks/builtin/memory_hook.py`
Expected: 无输出（两个 hook 已无 before_model_call；SkillEvolutionHook 等其他 hook 不在范围，不动）。

- [ ] **Step 4: 确认 memory_recall section 已删（grep）**

Run: `grep -rn "memory_recall" twinkle/ tests/`
Expected: 无输出（section 名 `memory_recall` 已彻底移除，改为 `memory_static`）。

- [ ] **Step 5: Commit（若有随全量回归产生的小修；否则跳过）**

仅当 Step 1 触发了 member/subagent/team 断言同步修改时：
```bash
git add -A
git commit -m "test: 同步 member/subagent/team 路径 frozen_sections 断言"
```

---

## Self-Review

**1. Spec coverage:**
- 点2 tools 冻结 → Task 3（Edit A + `test_tool_schemas_frozen_once_per_invoke`）。✓
- 点3 skills per-invoke → Task 1（SkillHook.before_invoke + `test_*` 5 个）。✓
- 点4 memory 静态化 → Task 2（strategy + memory_static、删 recall、无 daily、prompt 加 daily-search）+ Task 4（默认 True）。✓
- 注入机制 A（ctx.extra["frozen_sections"]）→ Task 1/2 stash + Task 3 Edit B 套用 + `test_frozen_sections_applied_to_builder`/`test_frozen_sections_byte_stable_across_steps`。✓
- 不做项（cache_control/sentinel/per-session/cached_tokens/SkillEvolutionHook/SkillManager memoize）→ 均无对应 task，符合「不做」。✓
- 成功标准「builder.build() 跨步字节稳定」→ `test_frozen_sections_byte_stable_across_steps`。✓
- 成功标准「tools 一 invoke 只 schemas() 一次」→ `test_tool_schemas_frozen_once_per_invoke`。✓
- 成功标准「daily 不在前缀」→ `test_daily_excluded_from_static`。✓
- 成功标准「MEMORY_AUTO_INJECT_ENABLED 默认 True」→ `test_memory_auto_inject_default_enabled`。✓
- 已知限制（无 cached_tokens 观测、SkillEvolutionHook experience prepend out of scope）→ spec 已记，plan 不涉及。✓

**2. Placeholder scan:** 无 TBD/TODO；每步含完整代码或精确命令 + expected。✓

**3. Type/签名一致性:**
- `PromptSection(name, content, priority)` 全 plan 一致（Task 1 priority=90、Task 2 priority=80/81、Task 3 `_MarkerHook` priority=50）。✓
- `ctx.extra.setdefault("frozen_sections", []).append(PromptSection(...))` Task 1/2/3 三处一致。✓
- loop 套用 `for sec in ctx.extra.get("frozen_sections", []): builder.add_section(sec)`（Task 3 Edit B）与 stash 端一致。✓
- `before_invoke(self, ctx: HookContext)` 签名 Task 1/2 + `_MarkerHook` 一致，与 `base.py:72` 基类签名匹配。✓
- `MEMORY_AUTO_INJECT_ENABLED`/`MEMORY_AUTO_INJECT_MAX_CHARS` 从 `twinkle.config` 导入，Task 2 + Task 4 一致。✓
- `InvokeInputs(query, mode)` 与 `base.py:113-118` 一致；Task 1/2 测试 helper 用法一致。✓

无问题，plan 定稿。
