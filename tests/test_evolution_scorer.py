"""测试 ExperienceScorer — E/U/F 计算、update_score。"""
import math
from datetime import datetime, timezone, timedelta

from twinkle.agentserver.evolution.scorer import (
    calc_effectiveness, calc_utilization, calc_freshness, calc_score,
)
from twinkle.agentserver.evolution.types import (
    EvolutionRecord, EvolutionPatch, UsageStats,
)


def _make_record(timestamp=None, score=0.6):
    patch = EvolutionPatch(section="Troubleshooting", action="append", content="test")
    rec = EvolutionRecord.make(source="execution_failure", context="test", change=patch)
    if timestamp:
        rec.timestamp = timestamp
    rec.score = score
    return rec


def test_calc_effectiveness_no_data():
    assert calc_effectiveness(None) == 0.5
    assert calc_effectiveness(UsageStats()) == 0.5


def test_calc_effectiveness_all_positive():
    stats = UsageStats(times_positive=5)
    # (5+1)/(5+0+2) = 6/7 ≈ 0.857
    expected = 6 / 7
    assert abs(calc_effectiveness(stats) - expected) < 0.001


def test_calc_effectiveness_mixed():
    stats = UsageStats(times_positive=3, times_negative=2)
    # (3+1)/(3+2+2) = 4/7 ≈ 0.571
    expected = 4 / 7
    assert abs(calc_effectiveness(stats) - expected) < 0.001


def test_calc_effectiveness_all_negative():
    stats = UsageStats(times_negative=5)
    # (0+1)/(0+5+2) = 1/7 ≈ 0.143
    expected = 1 / 7
    assert abs(calc_effectiveness(stats) - expected) < 0.001


def test_calc_utilization_no_data():
    assert calc_utilization(None) == 0.5
    assert calc_utilization(UsageStats()) == 0.5


def test_calc_utilization_half_used():
    stats = UsageStats(times_presented=10, times_used=5)
    assert calc_utilization(stats) == 0.5


def test_calc_utilization_all_used():
    stats = UsageStats(times_presented=10, times_used=10)
    assert calc_utilization(stats) == 1.0


def test_calc_freshness_recent():
    now = datetime.now(timezone.utc).isoformat()
    rec = _make_record(timestamp=now)
    f = calc_freshness(rec, half_life_days=90)
    assert f > 0.95  # 刚创建，接近 1.0


def test_calc_freshness_old():
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    rec = _make_record(timestamp=old)
    f = calc_freshness(rec, half_life_days=90)
    assert 0.7 < f < 0.8  # 一个半衰期后：0.5+0.5*0.5=0.75


def test_calc_freshness_very_old():
    ancient = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    rec = _make_record(timestamp=ancient)
    f = calc_freshness(rec, half_life_days=90)
    assert f < 0.55  # 接近 0.5 底部


def test_calc_freshness_version_mismatch():
    now = datetime.now(timezone.utc).isoformat()
    rec = _make_record(timestamp=now)
    rec.skill_version = "1.0"
    f = calc_freshness(rec, current_skill_version="2.0", stale_penalty=0.7)
    assert f < 0.75  # fresh=~1.0, ×0.7 = ~0.7


def test_calc_score_default_weights():
    now = datetime.now(timezone.utc).isoformat()
    rec = _make_record(timestamp=now)
    rec.usage_stats = UsageStats(times_presented=10, times_used=5, times_positive=3, times_negative=1)
    # E = (3+1)/(3+1+2) = 4/6 = 0.667
    # U = 5/10 = 0.5
    # F ≈ 1.0 (fresh)
    # score = 0.5*0.667 + 0.3*0.5 + 0.2*1.0 = 0.333 + 0.15 + 0.2 = 0.683
    s = calc_score(rec)
    assert 0.67 < s < 0.70


def test_update_score():
    from twinkle.agentserver.evolution.scorer import ExperienceScorer

    rec = _make_record()
    rec.usage_stats = UsageStats()

    scorer = ExperienceScorer(None)  # None LLM — update_score 不需要 LLM
    scorer.update_score(rec, {"used": True, "positive": True, "negative": False})

    assert rec.usage_stats.times_used == 1
    assert rec.usage_stats.times_positive == 1
    assert rec.usage_stats.times_negative == 0
    assert rec.usage_stats.last_evaluated_at is not None
    # 分数应重新计算
    assert rec.score != 0.6  # 不再是种子分


def test_update_score_negative():
    from twinkle.agentserver.evolution.scorer import ExperienceScorer

    rec = _make_record()
    rec.usage_stats = UsageStats(times_presented=5, times_used=3, times_positive=1, times_negative=2)

    scorer = ExperienceScorer(None)
    scorer.update_score(rec, {"used": True, "positive": False, "negative": True})

    assert rec.usage_stats.times_negative == 3  # 2 + 1
    # 分数应下降——初始 score=0.6，负面评价后应该更低
    assert rec.score < 0.65  # 低于种子分
