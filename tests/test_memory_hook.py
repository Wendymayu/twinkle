"""MemoryHook 测试——策略 prompt 常开 + opt-in 被动召回。

覆盖:空 store no-op、策略-only(开关关)、USER.md 注入、MEMORY.md 注入、
今日 daily 注入、超 cap 截断、"开关开但无 injectable 文件(只有昨日 daily)"
回退到策略-only。沿用 test_memory_store 的 async run 风格。
"""
import asyncio
import datetime as dt

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory.store import MemoryManager


def _ctx(messages=None):
    return HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(
            messages=messages or [{"role": "user", "content": "hi"}],
            tools=[],
        ),
        session_id="s",
        request_id="r",
    )


def _mgr(tmp_path, **kw):
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)


def _run(hook, ctx):
    asyncio.run(hook.before_model_call(ctx))


def _with_mgr(mgr):
    """测试期间设置单例;调用方须在之后 _set(None)。"""
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(mgr)
    return _set_memory_manager


def test_empty_store_noop(tmp_path):
    """空 store → 不注入(messages 不变)。"""
    reset = _with_mgr(_mgr(tmp_path))
    try:
        hook = MemoryHook()
        ctx = _ctx()
        before = list(ctx.inputs.messages)
        _run(hook, ctx)
        assert ctx.inputs.messages == before
    finally:
        reset(None)


def test_strategy_only_when_auto_inject_disabled(tmp_path, monkeypatch):
    """auto_inject 关(默认)→ 只策略 prompt,无「被动召回」段。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_AUTO_INJECT_ENABLED", False)
    mgr = _mgr(tmp_path)
    mgr.write("USER.md", "用户偏好中文", append=True)
    reset = _with_mgr(mgr)
    try:
        hook = MemoryHook()
        ctx = _ctx()
        _run(hook, ctx)
        injected = ctx.inputs.messages[0]["content"]
        assert "长期记忆" in injected        # 策略 prompt 在
        assert "被动召回" not in injected     # 无被动召回
    finally:
        reset(None)


def test_auto_inject_user_md(tmp_path, monkeypatch):
    """auto_inject 开 + USER.md 存在 → 注入含 USER.md 内容。"""
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
        injected = ctx.inputs.messages[0]["content"]
        assert "被动召回" in injected
        assert "张三" in injected
        assert "USER.md" in injected
    finally:
        reset(None)


def test_auto_inject_memory_md(tmp_path, monkeypatch):
    """auto_inject 开 + MEMORY.md 存在 → 注入含 MEMORY.md 内容(持久事实)。"""
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
        injected = ctx.inputs.messages[0]["content"]
        assert "被动召回" in injected
        assert "Python 3.12" in injected
        assert "MEMORY.md" in injected
    finally:
        reset(None)


def test_auto_inject_today_daily(tmp_path, monkeypatch):
    """auto_inject 开 + 今日 daily 存在 → 注入含 daily 内容。"""
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
        injected = ctx.inputs.messages[0]["content"]
        assert "v1.2" in injected
        assert today in injected
    finally:
        reset(None)


def test_auto_inject_truncates_when_over_cap(tmp_path, monkeypatch):
    """内容超 max_chars → 追加截断标记 + 提示用 memory_search。"""
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
        injected = ctx.inputs.messages[0]["content"]
        assert "截断" in injected
        assert "memory_search" in injected
    finally:
        reset(None)


def test_auto_inject_no_injectable_falls_back_to_strategy(tmp_path, monkeypatch):
    """auto_inject 开但只有昨日 daily(不在注入列表)→ 只策略。"""
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
        injected = ctx.inputs.messages[0]["content"]
        assert "长期记忆" in injected
        assert "被动召回" not in injected
    finally:
        reset(None)
