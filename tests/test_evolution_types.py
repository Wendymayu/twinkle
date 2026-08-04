"""测试 EvolutionRecord / EvolutionPatch / UsageStats 序列化往返。"""
import json

from twinkle.agentserver.evolution.types import (
    EvolutionRecord, EvolutionPatch, UsageStats, EvolutionLog,
    ConversationSignal, INITIAL_SCORE_BY_SIGNAL,
)


def test_evolution_record_make():
    patch = EvolutionPatch(section="Troubleshooting", action="append",
                           content="如果遇到 timeout，请重试最多 3 次。",
                           summary="timeout 重试指南")
    record = EvolutionRecord.make(
        source="execution_failure", context="command_exec timeout",
        change=patch, score=0.65, summary="添加 timeout 重试",
    )
    assert record.id.startswith("ev_")
    assert len(record.id) == 11  # ev_ + 8 hex
    assert record.source == "execution_failure"
    assert record.score == 0.65
    assert record.change.section == "Troubleshooting"
    assert record.usage_stats is not None
    assert record.usage_stats.times_presented == 0


def test_evolution_record_serialization_roundtrip():
    """验 EvolutionRecord → dict → JSON → dict → EvolutionRecord 往返。"""
    from twinkle.agentserver.evolution.store import EvolutionStore

    patch = EvolutionPatch(
        section="Examples", action="append",
        content="## 示例：调用 weather 工具\n```python\nweather('Beijing')\n```",
        summary="天气查询示例",
        keywords=["weather", "example"],
    )
    record = EvolutionRecord.make(
        source="script_artifact", context="executed weather script",
        change=patch, score=0.60,
    )
    record.usage_stats.times_presented = 5
    record.usage_stats.times_used = 3

    # 序列化
    log = EvolutionLog(entries=[record])
    from twinkle.agentserver.evolution.store import _dataclass_to_dict
    data = _dataclass_to_dict(log)
    json_text = json.dumps(data, ensure_ascii=False)

    # 反序列化
    re_data = json.loads(json_text)
    re_record = EvolutionStore._dict_to_record(re_data["entries"][0])

    assert re_record.id == record.id
    assert re_record.source == "script_artifact"
    assert re_record.score == 0.60
    assert re_record.change.section == "Examples"
    assert re_record.change.keywords == ["weather", "example"]
    assert re_record.usage_stats.times_presented == 5
    assert re_record.usage_stats.times_used == 3


def test_conversation_signal_fields():
    sig = ConversationSignal(
        type="execution_failure", skill_name="weather",
        context="command_exec failed: timeout", msg_index=5,
    )
    assert sig.type == "execution_failure"
    assert sig.skill_name == "weather"
    assert sig.msg_index == 5


def test_initial_scores():
    assert INITIAL_SCORE_BY_SIGNAL["execution_failure"] == 0.65
    assert INITIAL_SCORE_BY_SIGNAL["user_intent"] == 0.70
    assert INITIAL_SCORE_BY_SIGNAL["script_artifact"] == 0.60
    assert INITIAL_SCORE_BY_SIGNAL["conversation_review"] == 0.50
