"""长期记忆的 embedding provider。

OpenAICompatibleEmbeddingProvider = 生产用(复用 LLM_BASE_URL + LLM_API_KEY)。
MockEmbeddingProvider = 仅测试(确定性 hash 伪向量)。从不作为生产降级——无 key 时
降级到纯 FTS(见 store.MemoryManager)。
"""
from __future__ import annotations

import hashlib
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dims(self) -> int: ...
    @property
    def model(self) -> str: ...


class OpenAICompatibleEmbeddingProvider:
    """POST {base_url}/embeddings(兼容 OpenAI/DashScope/OpenRouter)。"""

    def __init__(self, base_url: str, api_key: str, model: str, dims: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])
        return [d["embedding"] for d in data]


class MockEmbeddingProvider:
    """仅测试。基于 md5 的确定性伪向量。不用于生产。"""

    def __init__(self, dims: int = 1536, model: str = "mock") -> None:
        self._dims = dims
        self._model = model

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.md5(t.encode("utf-8")).digest()
            out.append([(h[i % len(h)] / 255.0) for i in range(self._dims)])
        return out
