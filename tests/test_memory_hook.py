"""MemoryHook 测试——before_invoke 注 memory_strategy(常开)+ memory_static(opt-in,USER.md+MEMORY.md,无 daily)。

覆盖:空 store no-op、策略-only(开关关)、USER.md 注入、MEMORY.md 注入、daily 不进前缀、
超 cap head+tail 截断(保首尾丢中间,对齐 openclaw trimBootstrapContent)、USER/MEMORY 分预算互不挤占、
开关开但无 injectable 文件(只有昨日 daily)回退策略-only。
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
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 12000)
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
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY", 12000)
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
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 12000)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY", 12000)
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
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 12000)
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


def test_auto_inject_truncates_head_tail(tmp_path, monkeypatch):
    """超 max_chars → head+tail 截断(保首尾丢中间),对齐 openclaw trimBootstrapContent。

    内容=HEAD+X*180+TAIL(188>50):截断后首 HEAD 在/尾 TAIL 在/中部 X 大段丢。
    对比 head-only 会丢全部 TAIL——TAIL 在即 head+tail 证据。
    """
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 50)
    mgr = _mgr(tmp_path)
    body = "HEAD" + "X" * 180 + "TAIL"
    mgr.write("USER.md", body, append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        static = _section(ctx, "memory_static")
        assert static is not None
        assert "HEAD" in static.content          # 首部保留
        assert "TAIL" in static.content          # 尾部保留(head+tail 证据;head-only 会丢)
        assert "X" * 180 not in static.content   # 中部大段丢失(确实截断)
        assert "截断" in static.content
        assert "memory_search" in static.content
    finally:
        reset(None)


def test_auto_inject_separate_budgets(tmp_path, monkeypatch):
    """USER.md 与 MEMORY.md 各走自己预算,互不挤占(对齐 openclaw 分文件预算)。

    两者都超各自 50 上限 → 各自 head+tail 截断(首尾在/中部丢);合并预算会互相挤占丢首尾。
    """
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 50)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY", 50)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "U_HEAD" + "X" * 180 + "U_TAIL", append=True)
    mgr.write("MEMORY.md", "M_HEAD" + "Y" * 180 + "M_TAIL", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        static = _section(ctx, "memory_static")
        assert static is not None
        # USER 段独立截断:首尾在、中部 X 丢
        assert "U_HEAD" in static.content
        assert "U_TAIL" in static.content
        assert "X" * 180 not in static.content
        # MEMORY 段独立截断:首尾在、中部 Y 丢
        assert "M_HEAD" in static.content
        assert "M_TAIL" in static.content
        assert "Y" * 180 not in static.content
    finally:
        reset(None)


def test_auto_inject_no_injectable_falls_back_to_strategy(tmp_path, monkeypatch):
    """auto_inject 开但只有昨日 daily(不在注入列表)→ 只 strategy,无 static。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_MAX_CHARS_USER", 12000)
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
