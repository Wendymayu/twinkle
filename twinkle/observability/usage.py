"""从 LLM usage 对象读取 token 计数。

真实 openai SDK 把 ``Finish.usage`` 暴露为 ``CompletionUsage`` pydantic
对象（属性访问，无 ``.get``）；fakes/tests 用纯 dict。这里的 helper
统一处理两者，使 instrumentor 不假设 dict 形状。
"""
from __future__ import annotations

from typing import Any


def read_usage_token(usage: Any, *keys: str) -> Any:
    """取 ``usage`` 上 ``keys`` 中首个非 None 的值。

    兼容 dict（``.get``）与属性对象（``getattr``）；缺失的 key 解析为
    ``None``。``usage`` 为 falsy 时返回 ``None``。
    """
    if not usage:
        return None
    if isinstance(usage, dict):
        for k in keys:
            v = usage.get(k)
            if v is not None:
                return v
        return None
    for k in keys:
        v = getattr(usage, k, None)
        if v is not None:
            return v
    return None
