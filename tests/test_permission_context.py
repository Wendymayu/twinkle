from twinkle.agentserver.permission_context import (
    APPROVAL_CHANNEL, get_permission_channel, set_permission_channel)


def test_default_channel():
    assert get_permission_channel() == "default"


def test_set_and_get():
    tok = set_permission_channel("web")
    try:
        assert get_permission_channel() == "web"
    finally:
        # Python 3.14 removed Token.reset(); use ContextVar.reset(token)
        # (same call the project's observability _Token wrapper makes).
        APPROVAL_CHANNEL.reset(tok)
    assert get_permission_channel() == "default"
