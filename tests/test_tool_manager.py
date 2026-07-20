import asyncio

from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


async def _echo(text: str) -> str:
    """echo back text"""
    return f"echo:{text}"


def _make_manager() -> ToolManager:
    m = ToolManager()
    m.register(tool(_echo))
    return m


def test_schemas_are_openai_function_defs() -> None:
    m = _make_manager()
    schemas = m.schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "_echo",
                "description": "echo back text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_unknown_tool_returns_error_string() -> None:
    m = _make_manager()
    assert asyncio.run(m.execute("nope", {})) == "[error] unknown tool: nope"


def test_execute_passes_kwargs() -> None:
    m = _make_manager()
    assert asyncio.run(m.execute("_echo", {"text": "hi"})) == "echo:hi"


def test_execute_swallows_tool_exception_as_error_string() -> None:
    async def _boom(x: str) -> str:
        raise ValueError("boom")
    m = ToolManager()
    m.register(tool(_boom))
    out = asyncio.run(m.execute("_boom", {"x": "1"}))
    assert out == "[tool error] ValueError: boom"


def test_unregister_returns_true_when_present_false_when_absent() -> None:
    m = _make_manager()
    assert m.unregister("_echo") is True
    assert m.unregister("_echo") is False
    assert m.get("_echo") is None


def test_list_returns_all_registered() -> None:
    m = _make_manager()
    assert [t.card.name for t in m.list()] == ["_echo"]


def test_dynamic_register_visible_in_schemas_immediately() -> None:
    async def _later(n: int) -> str:
        """later"""
        return str(n)
    m = _make_manager()
    m.register(tool(_later))
    names = {s["function"]["name"] for s in m.schemas()}
    assert names == {"_echo", "_later"}


def test_tool_manager_registers_web_fetch_and_web_search() -> None:
    tm = tool_manager()
    names = {t.card.name for t in tm.list()}
    assert names == {"web_fetch", "web_search"}


def test_tool_manager_schemas_have_required_url_or_query() -> None:
    tm = tool_manager()
    by_name = {s["function"]["name"]: s for s in tm.schemas()}
    assert by_name["web_fetch"]["function"]["parameters"]["required"] == ["url"]
    assert by_name["web_search"]["function"]["parameters"]["required"] == ["query"]
    assert by_name["web_fetch"]["function"]["parameters"]["properties"]["max_chars"] == {
        "type": "integer", "default": 8000
    }
    assert by_name["web_search"]["function"]["parameters"]["properties"]["max_results"] == {
        "type": "integer", "default": 5
    }
