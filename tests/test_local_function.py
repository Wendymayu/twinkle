import asyncio
from dataclasses import is_dataclass

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.local_function import LocalFunction


async def _echo(text: str) -> str:
    return f"echo:{text}"


def test_localfunction_is_dataclass() -> None:
    assert is_dataclass(LocalFunction)


def test_invoke_passes_kwargs_and_returns_str() -> None:
    lf = LocalFunction(
        card=ToolCard(name="echo", description="", parameters={}),
        func=_echo,
    )
    out = asyncio.run(lf.invoke({"text": "hi"}))
    assert out == "echo:hi"


def test_localfunction_satisfies_tool_protocol() -> None:
    lf = LocalFunction(
        card=ToolCard(name="echo", description="", parameters={}),
        func=_echo,
    )
    assert lf.card.name == "echo"
    assert hasattr(lf, "invoke")
