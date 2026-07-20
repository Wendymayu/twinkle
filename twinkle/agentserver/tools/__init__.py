"""AgentServer tools package + default registry builder."""
from __future__ import annotations

from twinkle.agentserver.tools import web_fetch, web_search
from twinkle.agentserver.tools.registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    """Register the Phase 1 read-only tools. Phase 2 evolves this."""
    reg = ToolRegistry()
    reg.register(
        name="web_fetch",
        description="Fetch a URL and return its visible text content.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
                "max_chars": {"type": "integer", "default": 8000, "description": "Max chars to return."},
            },
            "required": ["url"],
        },
        execute=web_fetch.web_fetch,
    )
    reg.register(
        name="web_search",
        description="Search the web via DuckDuckGo; returns title + URL lines.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "default": 5, "description": "Max results to return."},
            },
            "required": ["query"],
        },
        execute=web_search.web_search,
    )
    return reg
