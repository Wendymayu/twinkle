from twinkle.agentserver.team.message_box import MessageBox


def test_put_drain_returns_messages_in_order():
    box = MessageBox()
    box.put("hello")
    box.put("world")
    assert box.drain() == ["hello", "world"]


def test_drain_empty_returns_empty_list():
    box = MessageBox()
    assert box.drain() == []


def test_drain_clears_queue():
    box = MessageBox()
    box.put("a")
    assert box.drain() == ["a"]
    assert box.drain() == []


def test_empty_reflects_state():
    box = MessageBox()
    assert box.empty() is True
    box.put("x")
    assert box.empty() is False
    box.drain()
    assert box.empty() is True
