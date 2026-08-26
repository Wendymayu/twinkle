from dataclasses import is_dataclass

from twinkle.agentserver.tools.base import Tool, ToolCard


def test_toolcard_is_dataclass_with_three_fields() -> None:
    c = ToolCard(name="echo", description="echoes", parameters={"type": "object"})
    assert is_dataclass(ToolCard)
    assert c.name == "echo"
    assert c.description == "echoes"
    assert c.parameters == {"type": "object"}


def test_tool_protocol_has_card_and_invoke() -> None:
    # Tool 是结构性 Protocol:任何带 `card` + async `invoke` 的对象都满足它。
    attrs = {n for n in dir(Tool) if not n.startswith("_")}
    assert "card" in Tool.__annotations__
    assert hasattr(Tool, "invoke")
