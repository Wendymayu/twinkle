# tests/test_progressive_config.py
"""ProgressiveToolConfig: 默认关 + extra 禁止(对齐 _StrictModel)。"""
import pytest
from pydantic import ValidationError
from twinkle.config.schema import TwinkleConfig, ProgressiveToolConfig


def test_progressive_tool_defaults_disabled():
    cfg = TwinkleConfig()
    assert cfg.progressive_tool.enabled is False
    assert cfg.progressive_tool.eager_tools == []


def test_progressive_tool_extra_keys_rejected():
    with pytest.raises(ValidationError):  # pydantic ValidationError
        ProgressiveToolConfig(enabled=True, bogus_field=1)


def test_twinkle_config_loads_progressive_tool_block_from_yaml() -> None:
    """shipped config.yaml 的 progressive_tool 块能被 loader 加载,默认 enabled=false。"""
    from twinkle.config.loader import load_config
    cfg = load_config()
    assert cfg.progressive_tool.enabled is False
    assert cfg.progressive_tool.eager_tools == []
