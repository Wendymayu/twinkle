"""Context compression — algorithm package.

Sliding-window + LLM summary compression for long conversations: when the
estimated token count of the session messages exceeds a threshold, the middle
is summarized into one system message, keeping the head (system prompt) and
the recent tail (with tool_call/result pairs intact).

The algorithm lives in ``compression``; this package re-exports the entry
points (and the helpers the tests poke at directly) so existing
``from twinkle.agentserver.context_compression import X`` imports keep working
for the ContextCompressionHook and the tests — the file→package swap is
transparent to callers.

Compression output is NOT written back to SessionStore — history.json stays
lossless; this only shapes what the LLM sees. The hook wiring (before_model_call)
lives in ``twinkle.agentserver.hooks.builtin.context_compression_hook``.
"""
from .compression import (
    _render_messages_text,
    _split_keep_tool_pairs,
    _summarize,
    compress_messages,
    estimate_tokens,
)

__all__ = ["compress_messages", "estimate_tokens"]
