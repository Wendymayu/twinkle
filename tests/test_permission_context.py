from twinkle.agentserver.permission_context import (
    APPROVAL_CHANNEL, get_permission_channel, set_permission_channel)


def test_default_channel():
    assert get_permission_channel() == "default"


def test_set_and_get():
    tok = set_permission_channel("web")
    try:
        assert get_permission_channel() == "web"
    finally:
        # Python 3.14 移除了 Token.reset()；改用 ContextVar.reset(token)
        # （与项目 observability 的 _Token wrapper 调用相同）。
        APPROVAL_CHANNEL.reset(tok)
    assert get_permission_channel() == "default"
