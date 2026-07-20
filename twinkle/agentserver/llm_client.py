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


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AsyncIterator[TextDelta | Finish]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        stream = await self._client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        tool_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = "stop"

        async for chunk in stream:
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
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    idx = tc.index
                    slot = tool_acc.setdefault(
                        idx, {"id": None, "name": None, "arguments": ""}
                    )
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        content = "".join(text_parts) or None
        tool_calls = None
        if finish_reason == "tool_calls" and tool_acc:
            tool_calls = [
                {
                    "id": tool_acc[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tool_acc[i]["name"],
                        "arguments": tool_acc[i]["arguments"],
                    },
                }
                for i in sorted(tool_acc)
            ]
        yield Finish(
            finish_reason=finish_reason,
            assistant_message={
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
        )
