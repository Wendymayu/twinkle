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


def test_auto_inject_user_and_memory_md_together(tmp_path, monkeypatch):
    """auto_inject 开 + USER.md + MEMORY.md 都在 → memory_static 含两者,join 拼接。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS", 12000)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "姓名:张三", append=True)
    mgr.write("MEMORY.md", "项目用 Python 3.12", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "张三" in static.content
        assert "Python 3.12" in static.content
        assert "USER.md" in static.content
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
        assert "X" * 200 not in static.content   # body actually sliced, not just marker appended
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
