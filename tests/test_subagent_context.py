from twinkle.agentserver.tools.builtin.subagent.context import (
    SUBAGENT_EXECUTOR,
    SUBAGENT_PARENT_SESSION_ID,
    SUBAGENT_PARENT_REQUEST_ID,
    get_subagent_executor,
    get_subagent_parent_session_id,
    get_subagent_parent_request_id,
)


def test_defaults_are_none():
    assert get_subagent_executor() is None
    assert get_subagent_parent_session_id() is None
    assert get_subagent_parent_request_id() is None


def test_set_and_get_round_trip():
    tok_e = SUBAGENT_EXECUTOR.set("exec-x")
    tok_s = SUBAGENT_PARENT_SESSION_ID.set("parent-session-id")
    tok_r = SUBAGENT_PARENT_REQUEST_ID.set("parent-request-id")
    try:
        assert get_subagent_executor() == "exec-x"
        assert get_subagent_parent_session_id() == "parent-session-id"
        assert get_subagent_parent_request_id() == "parent-request-id"
    finally:
        SUBAGENT_EXECUTOR.reset(tok_e)
        SUBAGENT_PARENT_SESSION_ID.reset(tok_s)
        SUBAGENT_PARENT_REQUEST_ID.reset(tok_r)
