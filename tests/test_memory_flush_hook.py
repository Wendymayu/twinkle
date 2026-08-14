"""MemoryFlushHook 测试——压缩前兜底。覆盖 spec §9.1 七项。"""
import asyncio

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_flush_hook import MemoryFlushHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory.store import MemoryManager


def _ctx(messages):
    return HookContext(
        agent=None, event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=messages, tools=[]),
        session_id="s", request_id="r",
    )


def _mgr(tmp_path):
    return MemoryManager(str(tmp_path), embed_provider=None)


def _with_mgr(mgr):
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(mgr)
    return _set_memory_manager


def _run(hook, ctx):
    asyncio.run(hook.before_model_call(ctx))


class _FakeLLM:
    """记录调用次数；按顺序返回预设响应文本。"""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
    async def stream(self, messages, tools):
        idx = self.calls
        self.calls += 1
        text = self._responses[idx] if idx < len(self._responses) else "[]"
        yield TextDelta(text)
        yield Finish(finish_reason="stop",
                      assistant_message={"role": "assistant", "content": text})


def _big_messages():
    """构造让 should_compress=True 的消息：须同时过 token 闸与 middle 闸。
    token 闸：180003 字符 // 3 = 60001 > 60000（180000 恰好 60000，<= 判定 False）。
    middle 闸：msg 数须 > tail_count(=CONTEXT_KEEP_RECENT_PAIRS*2=12)，否则全进 tail、
    middle 为空。故 1 条大消息铺 token 闸 + 12 条小消息把大消息挤进 middle。"""
    return ([{"role": "user", "content": "x" * 180003}]
            + [{"role": "user", "content": "y"} for _ in range(12)])


def _small_messages():
    return [{"role": "user", "content": "hi"}]


def test_flush_disabled_is_noop(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", False)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        llm = _FakeLLM(["[{}]"])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_big_messages()))
        assert llm.calls == 0
    finally:
        reset(None)


def test_should_compress_false_skips(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        llm = _FakeLLM(["[]"])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_small_messages()))   # 未达阈值
        assert llm.calls == 0
    finally:
        reset(None)


def test_flush_no_llm_is_noop(tmp_path, monkeypatch):
    """无 LLM（None）→ no-op，不崩。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        hook = MemoryFlushHook(None)
        _run(hook, _ctx(_big_messages()))   # 不崩
    finally:
        reset(None)


def test_flush_empty_writes_nothing(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    mgr = _mgr(tmp_path)
    reset = _with_mgr(mgr)
    try:
        llm = _FakeLLM(["[]"])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_big_messages()))
        assert llm.calls == 1
        assert mgr.list_files() == []   # 没写
    finally:
        reset(None)


def test_flush_writes_extracted_items(tmp_path, monkeypatch):
    import twinkle.config, json
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    mgr = _mgr(tmp_path)
    reset = _with_mgr(mgr)
    try:
        payload = json.dumps(
            [{"path": "MEMORY.md", "content": "项目用 Python 3.12", "append": True}])
        llm = _FakeLLM([payload])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_big_messages()))
        assert "项目用 Python 3.12" in mgr.read("MEMORY.md")
    finally:
        reset(None)


def test_flush_non_json_fails_soft(tmp_path, monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    mgr = _mgr(tmp_path)
    reset = _with_mgr(mgr)
    try:
        llm = _FakeLLM(["not json at all"])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_big_messages()))   # 不崩，不写
        assert mgr.list_files() == []
    finally:
        reset(None)


def test_flush_write_failure_fails_soft(tmp_path, monkeypatch):
    """write_memory 拿到非法 path（白名单挡）→ 返回 Error 串，不崩。"""
    import twinkle.config, json
    monkeypatch.setattr(twinkle.config, "MEMORY_FLUSH_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 60000)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 6)
    mgr = _mgr(tmp_path)
    reset = _with_mgr(mgr)
    try:
        payload = json.dumps(
            [{"path": "../etc/passwd", "content": "x", "append": True}])
        llm = _FakeLLM([payload])
        hook = MemoryFlushHook(llm)
        _run(hook, _ctx(_big_messages()))   # 白名单挡，不崩
    finally:
        reset(None)
