from dataclasses import is_dataclass

from twinkle.agentserver.tools.base import Tool, ToolCard


def test_toolcard_is_dataclass_with_three_fields() -> None:
    c = ToolCard(name="echo", description="echoes", parameters={"type": "object"})
    assert is_dataclass(ToolCard)
    assert c.name == "echo"
    assert c.description == "echoes"
    assert c.parameters == {"type": "object"}


def test_tool_protocol_has_card_and_invoke() -> None:
    # Tool is a structural Protocol: any object with `card` + async `invoke` satisfies it.
    attrs = {n for n in dir(Tool) if not n.startswith("_")}
    assert "card" in Tool.__annotations__
    assert hasattr(Tool, "invoke")
