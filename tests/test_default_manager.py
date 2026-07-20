from twinkle.agentserver.tools import default_tool_manager


def test_default_manager_registers_web_fetch_and_web_search() -> None:
    tool_manager = default_tool_manager()
    names = {t.card.name for t in tool_manager.list()}
    assert names == {"web_fetch", "web_search"}


def test_default_manager_schemas_have_required_url_or_query() -> None:
    tool_manager = default_tool_manager()
    by_name = {s["function"]["name"]: s for s in tool_manager.schemas()}
    assert by_name["web_fetch"]["function"]["parameters"]["required"] == ["url"]
    assert by_name["web_search"]["function"]["parameters"]["required"] == ["query"]
    assert by_name["web_fetch"]["function"]["parameters"]["properties"]["max_chars"] == {
        "type": "integer", "default": 8000
    }
    assert by_name["web_search"]["function"]["parameters"]["properties"]["max_results"] == {
        "type": "integer", "default": 5
    }
