from twinkle.agentserver.memory.embeddings import (
    MockEmbeddingProvider, OpenAICompatibleEmbeddingProvider,
)


def test_mock_is_deterministic():
    p = MockEmbeddingProvider(dims=8)
    a = p.embed(["hello", "world"])
    assert len(a) == 2
    assert len(a[0]) == 8 and len(a[1]) == 8
    assert p.embed(["hello"])[0] == a[0]
    assert p.embed(["hello"])[0] != p.embed(["world"])[0]
    assert p.model == "mock" and p.dims == 8


def test_mock_empty():
    assert MockEmbeddingProvider(dims=4).embed([]) == []


def test_openai_compatible_parses_response(monkeypatch):
    calls = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]},
                             {"index": 0, "embedding": [0.4, 0.5, 0.6]}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            calls["url"] = url
            calls["headers"] = headers
            calls["json"] = json
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    p = OpenAICompatibleEmbeddingProvider(
        base_url="https://api.example.com/v1", api_key="sk-x",
        model="text-embedding-3-small", dims=3)
    out = p.embed(["foo", "bar"])
    assert out == [[0.4, 0.5, 0.6], [0.1, 0.2, 0.3]]  # 按 index 排序
    assert calls["url"] == "https://api.example.com/v1/embeddings"
    assert calls["headers"]["Authorization"] == "Bearer sk-x"
    assert calls["json"]["model"] == "text-embedding-3-small"
    assert calls["json"]["input"] == ["foo", "bar"]


def test_sentence_transformer_provider_translates_encode(monkeypatch):
    """local provider: embed() -> SentenceTransformer(path).encode(normalize=True).tolist()。
    懒加载:构造不 import sentence_transformers,首次 embed 才加载(故测试无 torch 也能跑)。"""
    captured = {}

    class FakeArray:
        def __init__(self, rows): self._rows = rows
        def tolist(self): return self._rows

    class FakeModel:
        def __init__(self, path): captured["path"] = path
        def encode(self, texts, normalize_embeddings=False):
            captured["normalize"] = normalize_embeddings
            captured["texts"] = list(texts)
            return FakeArray([[0.1, 0.2, 0.3] for _ in texts])

    import sys
    fake_mod = type("M", (), {"SentenceTransformer": FakeModel})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    from twinkle.agentserver.memory.embeddings import SentenceTransformerProvider
    p = SentenceTransformerProvider(
        model_path="models/bge-small-zh-v1.5", dims=512,
        model_name="bge-small-zh-v1.5")
    out = p.embed(["hello", "world"])
    assert out == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert captured["path"] == "models/bge-small-zh-v1.5"
    assert captured["normalize"] is True
    assert captured["texts"] == ["hello", "world"]
    assert p.dims == 512 and p.model == "bge-small-zh-v1.5"


def test_sentence_transformer_provider_empty():
    from twinkle.agentserver.memory.embeddings import SentenceTransformerProvider
    p = SentenceTransformerProvider("models/x", 8, "x")
    assert p.embed([]) == []           # 空输入短路,不触发懒加载
    assert p.dims == 8 and p.model == "x"
