"""Long-term memory — STUB (Phase 1).

Interface shape only. recall() always returns []; store() is a no-op.
Real long-term memory (recall/store over a vector/wiki store) is
explicitly deferred per roadmap. Slotting in a real implementation later
requires no change to callers.
"""
from __future__ import annotations


class LongTermMemory:
    def recall(self, query: str) -> list[str]:
        return []

    def store(self, fact: str) -> None:
        return None
