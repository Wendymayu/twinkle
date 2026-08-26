"""LLMClient — openai SDK 流式 chat completions 的薄封装。

产出两种事件：
  - TextDelta(content)：每个流式文本片段
  - Finish(finish_reason, assistant_message)：流结束时发一次

Tool-call 片段跨 chunk 分片到达(按 index)；我们将其累积成单个
assistant_message，使 agent loop 能把它追加到 session store 并在下一轮
回喂 tool 结果。
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
    reasoning: str | None = None


def _delta_reasoning(delta: Any) -> str | None:
    """从流式 ``ChoiceDelta`` 上取下 provider 的思考片段。

    该字段是 provider 特有的、位于 OpenAI spec 之外，因此我们探测多个已知
    名称外加 pydantic ``model_extra`` 包(SDK 在 ``extra='allow'`` 时藏匿未知
    字段的地方)：
      - DeepSeek / Qwen / GLM thinking models → ``reasoning_content``
      - OpenAI o-series → ``reasoning``
    返回找到的第一个非空片段，否则返回 None。
    """
    for attr in ("reasoning_content", "reasoning"):
        val = getattr(delta, attr, None)
        if val:
            return val
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning"):
            val = extra.get(key)
            if val:
                return val
    return None


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float | None = None,
    ) -> None:
        self._model = model
        # timeout -> AsyncOpenAI httpx read timeout：每 chunk 之间的空闲——某模型
        # 若 N 秒不发 chunk 则抛 APITimeoutError
        # (transient -> RetryHook 重试)；稳定流(chunk 间隔 < N)不会超时，
        # 故长回答安全。None = SDK 默认。
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
        reasoning_parts: list[str] = []  # provider 思考(reasoning_content/reasoning)
        tool_call_accumulator: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = "stop"

        usage: dict | None = None
        async for chunk in stream:
            # 捕获 token usage——当 provider 下发时(OpenAI 带
            # stream_options.include_usage,或 dashscope)：有些 provider 把 usage
            # 挂在最后一个 content chunk 上,另一些挂在一个 choices 为空的
            # 尾部 usage-only chunk 上。最后一个非空值胜出。
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = chunk_usage
            # OpenAI 兼容流(dashscope、带 stream_options.include_usage 的 openai)
            # 以一个 ``choices`` 列表为空的 usage-only chunk 结尾。跳过它——
            # 此 chunk 无 delta 可消费。
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                yield TextDelta(delta.content)
            # Provider 思考片段——与回答分开累积,使 chain-of-thought 不污染
            # ReAct message stream。在 Finish 上暴露以便持久化(display/evolution),
            # 但不在下一轮回喂给 model(见 SessionStore)。
            reasoning_chunk = _delta_reasoning(delta)
            if reasoning_chunk:
                reasoning_parts.append(reasoning_chunk)
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
        reasoning = "".join(reasoning_parts) or None
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
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        if reasoning is not None:
            assistant_message["reasoning"] = reasoning
        yield Finish(
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            usage=usage,
            reasoning=reasoning,
        )
