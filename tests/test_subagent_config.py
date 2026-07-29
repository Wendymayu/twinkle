from twinkle.config.schema import SubagentConfig, TwinkleConfig


def test_subagent_config_defaults():
    c = SubagentConfig()
    assert c.max_steps == 50
    assert c.hard_timeout == 300
    assert c.soft_timeout == 120
    assert c.abort_timeout == 30
    assert c.child_permissions is False
    assert c.model == ""
    assert c.max_result_chars == 8000
    assert c.list_sessions_filter is True


def test_twinkle_config_has_subagent_section():
    cfg = TwinkleConfig()
    assert isinstance(cfg.subagent, SubagentConfig)
    assert cfg.subagent.max_steps == 50


def test_child_permissions_true_rejected_at_startup():
    """v1 has no streaming, so child HITL would deadlock — reject child_permissions=True."""
    from pydantic import ValidationError
    try:
        SubagentConfig(child_permissions=True)
        assert False, "expected ValidationError"
    except ValidationError:
        pass
