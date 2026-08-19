import pytest
from twinkle.config.schema import McpConfig, McpServerConfig, TwinkleConfig


def test_mcp_disabled_by_default() -> None:
    cfg = TwinkleConfig()
    assert cfg.mcp.enabled is False
    assert cfg.mcp.servers == []
    assert cfg.mcp.connect_timeout == 30.0
    assert cfg.mcp.call_timeout == 60.0
    assert cfg.mcp.reconnect_attempts == 3


def test_stdio_server_requires_command() -> None:
    with pytest.raises(ValueError, match="stdio server requires 'command'"):
        McpServerConfig(name="s", transport="stdio", command="")


def test_streamable_http_server_requires_url() -> None:
    with pytest.raises(ValueError, match="streamable-http server requires 'url'"):
        McpServerConfig(name="s", transport="streamable-http", url="")


def test_stdio_server_ok() -> None:
    s = McpServerConfig(name="fs", transport="stdio", command="npx",
                       args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_http_server_ok() -> None:
    s = McpServerConfig(name="my", transport="streamable-http",
                       url="http://127.0.0.1:8080/mcp")
    assert s.url == "http://127.0.0.1:8080/mcp"
    assert s.auth_headers == {}


def test_unknown_transport_rejected() -> None:
    with pytest.raises(Exception):
        McpServerConfig(name="s", transport="sse", command="x")  # type: ignore[arg-type]


def test_twinkle_config_loads_mcp_block_from_yaml() -> None:
    """shipped config.yaml 的 mcp 块能被 loader 加载,默认 enabled=false。"""
    from twinkle.config.loader import load_config
    cfg = load_config()
    assert cfg.mcp.enabled is False
    assert cfg.mcp.servers == []
