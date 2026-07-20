import asyncio
from twinkle.agentserver.tools.registry import ToolRegistry


async def echo_tool(text: str) -> str:
    return f"echo:{text}"


def test_schemas_are_openai_function_defs() -> None:
    reg = ToolRegistry()
    reg.register(
        "echo",
        "echo back text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
    )
    schemas = reg.schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "echo",
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
    reg = ToolRegistry()

    async def run() -> str:
        return await reg.execute("nope", {})

    assert asyncio.run(run()) == "[error] unknown tool: nope"


def test_execute_passes_kwargs() -> None:
    reg = ToolRegistry()
    reg.register(
        "echo",
        "echo back text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
    )

    async def run() -> str:
        return await reg.execute("echo", {"text": "hi"})

    assert asyncio.run(run()) == "echo:hi"
