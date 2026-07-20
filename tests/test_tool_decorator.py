import asyncio

from twinkle.agentserver.tools.decorator import tool


async def _fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL."""
    return url


@tool
async def _plain(url: str) -> str:
    """Plain decorated."""
    return url


@tool()
async def _called(url: str) -> str:
    """Called-no-args decorated."""
    return url


@tool(name="renamed", description="custom desc", input_params={"type": "object", "properties": {}, "required": []})
async def _override(url: str) -> str:
    return url


def test_bare_tool_returns_localfunction() -> None:
    assert _plain.card.name == "_plain"
    assert _plain.card.description == "Plain decorated."
    assert _plain.card.parameters["required"] == ["url"]


def test_called_no_args_returns_localfunction() -> None:
    assert _called.card.name == "_called"


def test_override_name_description_params() -> None:
    assert _override.card.name == "renamed"
    assert _override.card.description == "custom desc"
    assert _override.card.parameters == {"type": "object", "properties": {}, "required": []}


def test_bare_call_form_tool_fn() -> None:
    lf = tool(_fetch)
    assert lf.card.name == "_fetch"
    assert lf.card.parameters["required"] == ["url"]
    assert lf.card.parameters["properties"]["max_chars"] == {"type": "integer", "default": 8000}


def test_decorated_function_invokable_via_invoke() -> None:
    out = asyncio.run(_plain.invoke({"url": "u"}))
    assert out == "u"
