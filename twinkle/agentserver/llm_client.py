"""LLMClient — thin wrapper over the openai SDK streaming chat completions.

Emits two event types:
  - TextDelta(content) for each streamed text fragment
  - Finish(finish_reason, assistant_message) once, at stream end

Tool-call fragments arrive split across chunks (indexed); we accumulate
them into a single assistant_message so the agent loop can append it to
the session store and feed tool results back in the next turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from openai import AsyncOpenAI


@dataclass
class TextDelta:
    content: str


@dataclass
class Finish:
    finish_reason: str
    assistant_message: dict
    usage: dict | None = None


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float | None = None,
    ) -> None:
        self._model = model
        # timeout -> AsyncOpenAI read timeout: a hung model (no chunk for N
        # seconds) raises APITimeoutError (transient -> retried by RetryHook)
        # instead of blocking the request forever. None = SDK default.
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout
        )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AsyncIterator[TextDelta | Finish]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        stream = await self._client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        tool_call_accumulator: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = "stop"

        usage: dict | None = None
        async for chunk in stream:
            # Capture token usage if the provider emits it (OpenAI with
            # stream_options.include_usage, or dashscope) — some providers
            # attach usage to the last content chunk, others to a trailing
            # usage-only chunk with empty choices. Last non-null wins.
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = chunk_usage
            # OpenAI-compatible streams (dashscope, openai with
            # stream_options.include_usage) end with a usage-only chunk whose
            # ``choices`` list is empty. Skip it — there is no delta to consume.
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                yield TextDelta(delta.content)
            tool_call_deltas = getattr(delta, "tool_calls", None)
            if tool_call_deltas:
                for tool_call_delta in tool_call_deltas:
                    index = tool_call_delta.index
                    call_entry = tool_call_accumulator.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    if getattr(tool_call_delta, "id", None):
                        call_entry["id"] = tool_call_delta.id
                    func = getattr(tool_call_delta, "function", None)
                    if func is not None:
                        if getattr(func, "name", None):
                            call_entry["name"] = func.name
                        if getattr(func, "arguments", None):
                            call_entry["arguments"] += func.arguments
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        content = "".join(text_parts) or None
        tool_calls = None
        if finish_reason == "tool_calls" and tool_call_accumulator:
            tool_calls = [
                {
                    "id": tool_call_accumulator[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tool_call_accumulator[i]["name"],
                        "arguments": tool_call_accumulator[i]["arguments"],
                    },
                }
                for i in sorted(tool_call_accumulator)
            ]
        yield Finish(
            finish_reason=finish_reason,
            assistant_message={
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
            usage=usage,
        )
