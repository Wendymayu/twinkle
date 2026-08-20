"""模型上下文窗口目录 + 解析器。

三级优先：config 手动覆盖 > 模型字典(前缀匹配) > 128000 兜底。
对齐 jiuwenswarm(全局单值) 与 openclaw(catalog 查表) 的折中：精简字典 + 兜底。
窗口值用于 A(413 恢复兜底) 与 B(预防压缩触发)。
"""
from __future__ import annotations

from twinkle.config import settings

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
}

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def normalize_model(name: str) -> str:
    """lowercase + 去掉首个 `:` 及之后(兼容 gpt-4o-mini:latest / ollama 带标签写法)。"""
    return name.lower().split(":", 1)[0]


def resolve_context_window_limit(
    *, model: str | None = None, manual_override: int | None = None
) -> int:
    """三级解析当前模型上下文窗口 token 上限。

    1. manual_override > 0(默认读 settings.overflow_recovery.context_window_limit_tokens) → 手动覆盖
    2. 模型字典前缀匹配(取最长 key) → 字典值
    3. 无匹配 → DEFAULT_CONTEXT_WINDOW_TOKENS
    """
    override_tokens = (manual_override if manual_override is not None
          else settings.overflow_recovery.context_window_limit_tokens)
    if override_tokens > 0:
        return override_tokens

    normalized = normalize_model(model if model is not None else settings.llm.model)
    matched = [k for k in MODEL_CONTEXT_WINDOWS if normalized.startswith(k)]
    if matched:
        longest = max(matched, key=len)
        return MODEL_CONTEXT_WINDOWS[longest]
    return DEFAULT_CONTEXT_WINDOW_TOKENS
