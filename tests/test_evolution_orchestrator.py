"""测试 OnlineEvolutionOrchestrator — run_feedback_loop 呈现计数落盘(U 维回归)。

回归 🔴:presented 计数若只改临时对象不落盘 → evolutions.json 里 times_presented
恒 0 → calc_utilization 永走 0.5 兜底 → EUF 的 U 维失效。
机制:after_tool_call(read_skill) 记 _presented_ids_by_skill(只记 id,不 +1);
run_feedback_loop 从 store fresh 读后 +1 再 save 落盘。本测试直调 run_feedback_loop 验落盘。
"""
import asyncio
import json

import pytest

from twinkle.agentserver.evolution.orchestrator import OnlineEvolutionOrchestrator
from twinkle.agentserver.evolution.scorer import ExperienceScorer, calc_utilization
from twinkle.agentserver.evolution.store import EvolutionStore
from twinkle.agentserver.evolution.types import ConversationSignal, EvolutionPatch, EvolutionRecord


@pytest.fixture
def store(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    return EvolutionStore(str(skills_dir))


def _make_skill(store, name="test", content="# Test Skill\n\nbody\n"):
    skill_dir = store._skill_dir(name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _make_record(summary="timeout retry"):
    patch = EvolutionPatch(section="Troubleshooting", action="append",
                           content="遇到 timeout 重试", summary=summary)
    return EvolutionRecord.make(source="execution_failure", context="ctx",
                                change=patch, summary=summary)


class _FakeEvalLLM:
    """scorer.evaluate 用:chat 返 choices[0].message.content(JSON 数组),按序消费。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    async def chat(self, messages, tools=None):
        text = self._responses[self._i] if self._i < len(self._responses) else "[]"
        self._i += 1
        return _Resp(text)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Msg:
    def __init__(self, content):
        self.content = content


def test_feedback_loop_persists_times_presented(store):
    """run_feedback_loop 必须把 times_presented 落盘并跨调用累积,否则 U 维恒 0.5 兜底。"""
    _make_skill(store)
    rec = _make_record()
    store.save_evolution_log("test", [rec])

    used = json.dumps([{"record_id": rec.id, "used": True, "positive": True,
                        "negative": False, "reason": "采纳并成功"}])
    scorer = ExperienceScorer(_FakeEvalLLM([used, used]))
    orch = OnlineEvolutionOrchestrator(store=store, optimizer=None,
                                       scorer=scorer, detector=None)

    asyncio.run(orch.run_feedback_loop("test", [rec.id], "agent 采纳重试并成功"))

    r1 = store.read_evolution_log("test").entries[0]
    assert r1.usage_stats.times_presented == 1   # 修复前为 0(呈现层 +1 不落盘)
    assert r1.usage_stats.times_used == 1
    assert r1.usage_stats.last_presented_at is not None

    asyncio.run(orch.run_feedback_loop("test", [rec.id], "再次采纳"))

    r2 = store.read_evolution_log("test").entries[0]
    assert r2.usage_stats.times_presented == 2   # 累积 → 证明落盘非临时对象
    assert r2.usage_stats.times_used == 2
    # U 维真生效:走 used/presented = 2/2 = 1.0,非 0.5 兜底
    assert calc_utilization(r2.usage_stats) == 1.0


def test_feedback_loop_no_presented_ids_is_noop(store):
    """presented_ids 命中 0 条 → 不调 LLM、不增 presented(fail-soft no-op)。"""
    _make_skill(store)
    rec = _make_record()
    store.save_evolution_log("test", [rec])

    fake = _FakeEvalLLM([])  # 不该被调
    scorer = ExperienceScorer(fake)
    orch = OnlineEvolutionOrchestrator(store=store, optimizer=None,
                                       scorer=scorer, detector=None)
    asyncio.run(orch.run_feedback_loop("test", ["ev_nonexistent"], "snippet"))
    assert fake._i == 0  # LLM 未调
    r = store.read_evolution_log("test").entries[0]
    assert r.usage_stats.times_presented == 0  # 不误增


class _CountingDetector:
    """detect 计数器 + 返预设 flat 信号列表（按 skill_name 让 evolve_all 分组）。"""

    def __init__(self, flat_signals):
        self._flat = list(flat_signals)
        self.call_count = 0

    def detect(self, messages, skill_names, enabled_signals=None):
        self.call_count += 1
        return list(self._flat)


class _SpyOptimizer:
    """generate_records 记录每个 skill 收到的 signals，返 [] → evolve 走 no_records（不调 LLM）。"""

    def __init__(self):
        self.received: dict = {}

    async def generate_records(self, skill_name, signals, skill_content, existing_records,
                               max_text=2, max_script=1):
        self.received[skill_name] = list(signals)
        return []


def test_evolve_all_detects_once_and_dispatches_signals_by_skill(store):
    """evolve_all 必须只跑一次 detector、按 skill_name 把信号分发给各 skill 的 evolve。

    回归 per-skill 循环重跑 detector：N 个 skill → detector 调 1 次而非 N 次；
    且信号按 skill_name 正确分发（不是每个 skill 都收到全部信号）。
    """
    for name in ("alpha", "beta", "gamma"):
        _make_skill(store, name)

    sig_a = ConversationSignal(type="execution_failure", skill_name="alpha", context="err")
    sig_b = ConversationSignal(type="script_artifact", skill_name="beta", context="ok")
    sig_c = ConversationSignal(type="user_intent", skill_name="gamma", context="不对")

    detector = _CountingDetector([sig_a, sig_b, sig_c])
    optimizer = _SpyOptimizer()
    orch = OnlineEvolutionOrchestrator(store=store, optimizer=optimizer,
                                       scorer=None, detector=detector)

    results = asyncio.run(orch.evolve_all([], ["alpha", "beta", "gamma"]))

    assert detector.call_count == 1                       # detect 只跑一次（非 3 次）
    assert set(results) == {"alpha", "beta", "gamma"}    # 每个 skill 都有结果
    assert optimizer.received["alpha"] == [sig_a]         # 信号按 skill 分发
    assert optimizer.received["beta"] == [sig_b]
    assert optimizer.received["gamma"] == [sig_c]
    assert all(r.status == "no_records" for r in results.values())  # optimizer 返 [] → no_records


class _RecordingDetector:
    """detect 记录收到的 skill_names（验单 skill 进化只喂 [skill_name]、非全集）。"""

    def __init__(self, signals_to_return):
        self._signals = list(signals_to_return)
        self.received_skill_names = None
        self.call_count = 0

    def detect(self, messages, skill_names, enabled_signals=None):
        self.received_skill_names = list(skill_names)
        self.call_count += 1
        return list(self._signals)


def test_evolve_single_skill_does_not_query_all_skills(store, monkeypatch):
    """evolve(skill_name, signals=None) 只给 detector 喂 [skill_name]、不调 list_skills()。

    回归 evolve 复用批量 detect(all_skills) 再过滤到一个的怪味：单 skill 进化
    不该知道别的 skill 存在。改成 detect(messages, [skill_name]) 后 evolve 不再
    伸手进 skills manager。
    """
    from types import SimpleNamespace
    _make_skill(store, "alpha")

    # 假 manager：list_skills 返多 skill 全集。若 evolve 还调它，detector 会收到全集而非 ["alpha"]。
    class _FakeManager:
        def __init__(self):
            self.list_calls = 0

        def list_skills(self):
            self.list_calls += 1
            return [SimpleNamespace(name=n) for n in ("alpha", "beta", "gamma")]

    fake_manager = _FakeManager()
    monkeypatch.setattr("twinkle.agentserver.skills.get_skill_manager", lambda: fake_manager)

    sig = ConversationSignal(type="execution_failure", skill_name="alpha", context="err")
    detector = _RecordingDetector([sig])
    optimizer = _SpyOptimizer()  # generate_records 返 [] → evolve 走 no_records
    orch = OnlineEvolutionOrchestrator(store=store, optimizer=optimizer,
                                       scorer=None, detector=detector)

    result = asyncio.run(orch.evolve("alpha", [], signals=None))

    assert detector.received_skill_names == ["alpha"]   # 只喂单 skill，非全集
    assert detector.call_count == 1
    assert fake_manager.list_calls == 0                  # 没查 list_skills
    assert result.status == "no_records"


# --- 回归 A/B/C:反馈环消息源 / skip 名额 / config 上限接线 --- #


class _SnippetSpyOrch:
    """记录 run_feedback_loop 收到的 snippet,验证 hook 真把对话喂进去了。"""

    def __init__(self):
        self.snippets: dict[str, str] = {}

    async def run_feedback_loop(self, skill_name, record_ids, snippet):
        self.snippets[skill_name] = snippet


def test_feedback_loop_reads_agent_messages_not_ctx_inputs():
    """AFTER_INVOKE 时 ctx.inputs 是 InvokeInputs(无 messages 字段)。

    回归 🔴:旧实现 messages = getattr(ctx.inputs, "messages", []) → 恒 [] →
    snippet="" → `if record_ids and snippet` 永假 → run_feedback_loop 从不被调 →
    times_presented 恒 0、EUF 反馈环整条命门在 hook 路径里是死的(只有直调单测能跑)。
    修复:_run_feedback_loop 改从 ctx.agent._messages 取消息(同 _run_evolution)。
    """
    from types import SimpleNamespace
    from twinkle.agentserver.hooks.builtin.evolution_hook import SkillEvolutionHook
    from twinkle.agentserver.hooks.base import HookContext, InvokeInputs, HookEvent

    spy = _SnippetSpyOrch()
    hook = SkillEvolutionHook(orchestrator=spy)
    hook._presented_ids_by_skill = {"alpha": ["ev_1"]}  # 模拟 after_tool_call(read_skill) 呈现过

    # ctx.inputs = InvokeInputs(无 messages);真消息只在 agent._messages
    agent = SimpleNamespace(_messages=[
        {"role": "assistant", "content": "我按经验重试了"},
        {"role": "user", "content": "成功了"},
    ])
    ctx = HookContext(agent=agent, event=HookEvent.AFTER_INVOKE,
                      inputs=InvokeInputs(query="x"), session_id=None, request_id=None)

    asyncio.run(hook._run_feedback_loop(ctx))

    assert "alpha" in spy.snippets        # 反馈环被调了(旧实现这里为空)
    assert spy.snippets["alpha"]          # snippet 非空


def test_skip_drafts_do_not_consume_text_slots():
    """skip draft 不该占 text/script 名额。

    回归 🔴:旧实现先计数 text/script 再 `if action == "skip": continue` →
    [skip, real, real] 在 max_text=2 下 skip 吃掉一个名额,只留 1 条 real。
    修复:skip 检查前置于计数。
    """
    from twinkle.agentserver.evolution.optimizer import SkillExperienceOptimizer

    optimizer = SkillExperienceOptimizer(llm_client=None)  # _build_records 不调 LLM
    drafts = [
        {"action": "skip", "skip_reason": "duplicate", "target": "body",
         "section": "Troubleshooting", "content": ""},
        {"action": "append", "target": "body", "section": "Troubleshooting",
         "summary": "r1", "content": "c1"},
        {"action": "append", "target": "body", "section": "Troubleshooting",
         "summary": "r2", "content": "c2"},
    ]
    sig = ConversationSignal(type="execution_failure", skill_name="alpha", context="err")
    records = optimizer._build_records_from_drafts(drafts, [sig], max_text=2, max_script=1)

    assert [r.summary for r in records] == ["r1", "r2"]   # 两条 real 都留(旧实现只剩 r1)


def test_evolve_passes_configured_max_records_to_optimizer(store, monkeypatch):
    """config 的 max_text_records/max_script_records 必须传到 generate_records。

    回归 🔴:旧实现 evolve 调 generate_records 不传 max_text/max_script →
    optimizer 永用默认 2/1 → EVOLUTION_MAX_TEXT_RECORDS/MAX_SCRIPT_RECORDS 形同虚设
    (定义了却没人读)。修复:evolve 读 config 并传入。
    """
    _make_skill(store, "alpha")
    captured: dict = {}

    class _MaxCaptureOptimizer:
        async def generate_records(self, skill_name, signals, skill_content,
                                   existing_records, max_text=2, max_script=1):
            captured["max_text"] = max_text
            captured["max_script"] = max_script
            return []

    sig = ConversationSignal(type="execution_failure", skill_name="alpha", context="err")
    detector = _RecordingDetector([sig])
    orch = OnlineEvolutionOrchestrator(store=store, optimizer=_MaxCaptureOptimizer(),
                                       scorer=None, detector=detector)

    # 改 config 值为非默认,验证传的是 config 值(不是硬编码 2/1)
    monkeypatch.setattr("twinkle.config.EVOLUTION_MAX_TEXT_RECORDS", 5)
    monkeypatch.setattr("twinkle.config.EVOLUTION_MAX_SCRIPT_RECORDS", 3)

    asyncio.run(orch.evolve("alpha", [], signals=None))

    assert captured["max_text"] == 5
    assert captured["max_script"] == 3


# --- after_tool_call: read_skill 呈现触发 presented(取代旧 before_model_call 每步全量注入) --- #


def test_after_tool_call_read_skill_marks_presented(store):
    """模型调 read_skill(skill,"SKILL.md") = 加载该 skill 主体(含经验索引块) →
    记该 skill 全部 non-skip 经验 presented,供 after_invoke 反馈环判定。

    取代旧 before_model_call 每步全量所有 skill 注入摘要:经验不再主动灌进
    system message,只在模型真 read_skill 时才算"被呈现",presented 真实(不读不涨)。
    """
    from types import SimpleNamespace
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, ToolCallInputs
    from twinkle.agentserver.hooks.builtin.evolution_hook import SkillEvolutionHook

    _make_skill(store, "alpha")
    rec1, rec2 = _make_record(summary="r1"), _make_record(summary="r2")
    store.save_evolution_log("alpha", [rec1, rec2])

    orch = OnlineEvolutionOrchestrator(store=store, optimizer=None,
                                       scorer=None, detector=None)
    hook = SkillEvolutionHook(orchestrator=orch)

    ctx = HookContext(
        agent=SimpleNamespace(),
        event=HookEvent.AFTER_TOOL_CALL,
        inputs=ToolCallInputs(name="read_skill",
                              args={"skill_name": "alpha", "relative_file_path": "SKILL.md"},
                              tool_call_id="tc1"),
        session_id=None, request_id=None,
    )
    asyncio.run(hook.after_tool_call(ctx))

    assert hook._presented_ids_by_skill.get("alpha") == [rec1.id, rec2.id]


def test_after_tool_call_non_read_skill_is_noop(store):
    """非 read_skill 工具(如 list_skill)不触发 presented 记录。"""
    from types import SimpleNamespace
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, ToolCallInputs
    from twinkle.agentserver.hooks.builtin.evolution_hook import SkillEvolutionHook

    _make_skill(store, "alpha")
    rec = _make_record()
    store.save_evolution_log("alpha", [rec])

    orch = OnlineEvolutionOrchestrator(store=store, optimizer=None,
                                       scorer=None, detector=None)
    hook = SkillEvolutionHook(orchestrator=orch)

    ctx = HookContext(
        agent=SimpleNamespace(),
        event=HookEvent.AFTER_TOOL_CALL,
        inputs=ToolCallInputs(name="list_skill", args={}, tool_call_id="tc1"),
        session_id=None, request_id=None,
    )
    asyncio.run(hook.after_tool_call(ctx))

    assert "alpha" not in hook._presented_ids_by_skill  # 非 read_skill 不记


def test_after_tool_call_read_sidecar_not_marked(store):
    """read_skill 读 sidecar(evolution/*.md)不重复记 presented——
    读 SKILL.md(主体含索引块)时已记全部索引,sidecar 是详情,不二次 +1。"""
    from types import SimpleNamespace
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, ToolCallInputs
    from twinkle.agentserver.hooks.builtin.evolution_hook import SkillEvolutionHook

    _make_skill(store, "alpha")
    rec = _make_record()
    store.save_evolution_log("alpha", [rec])

    orch = OnlineEvolutionOrchestrator(store=store, optimizer=None,
                                       scorer=None, detector=None)
    hook = SkillEvolutionHook(orchestrator=orch)

    ctx = HookContext(
        agent=SimpleNamespace(),
        event=HookEvent.AFTER_TOOL_CALL,
        inputs=ToolCallInputs(name="read_skill",
                              args={"skill_name": "alpha",
                                    "relative_file_path": "evolution/Troubleshooting.md"},
                              tool_call_id="tc1"),
        session_id=None, request_id=None,
    )
    asyncio.run(hook.after_tool_call(ctx))

    assert "alpha" not in hook._presented_ids_by_skill  # 读 sidecar 不记
