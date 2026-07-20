from twinkle.agentserver.memory import LongTermMemory


def test_recall_returns_empty() -> None:
    assert LongTermMemory().recall("anything") == []


def test_store_is_noop() -> None:
    m = LongTermMemory()
    m.store("some fact")  # must not raise
    assert m.recall("some fact") == []
