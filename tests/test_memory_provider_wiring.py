"""_build_embed_provider 选 provider 的分支 + loud-degrade(不静默谎报 hybrid)。
对齐本次 bug 的教训:local 缺依赖/缺模型 → log.error 一次 + 退 None(诚实 FTS-only),
不 hard raise(memory 是可选子系统,agent 不该因 embed 缺失挂死)。"""
import logging
import sys

import pytest

import twinkle.config as cfg


@pytest.fixture
def wiring():
    # 延迟导入:测试可先 monkeypatch twinkle.config 常量,函数内部 from ... import 才读
    from twinkle.agentserver.memory import _build_embed_provider
    return _build_embed_provider


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(cfg, k, v)


def test_provider_none(monkeypatch, wiring):
    _set(monkeypatch, MEMORY_EMBED_PROVIDER="none", MEMORY_EMBED_DIMS=512)
    prov, dims = wiring()
    assert prov is None and dims == 512


def test_provider_openai_compat_uses_embed_url_and_key(monkeypatch, wiring):
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="openai_compat",
         MEMORY_EMBED_BASE_URL="https://embed.example.com/v1",
         MEMORY_EMBED_API_KEY="sk-embed",
         MEMORY_EMBED_MODEL="text-embedding-3-small",
         MEMORY_EMBED_DIMS=1536,
         LLM_BASE_URL="https://llm.example.com/v1",
         LLM_API_KEY="sk-llm")
    prov, dims = wiring()
    assert prov is not None and dims == 1536
    assert prov.model == "text-embedding-3-small"
    # 用 embed.* 而非 llm.*
    assert prov._base_url == "https://embed.example.com/v1"
    assert prov._api_key == "sk-embed"


def test_provider_openai_compat_falls_back_to_llm(monkeypatch, wiring):
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="openai_compat",
         MEMORY_EMBED_BASE_URL="", MEMORY_EMBED_API_KEY="",
         LLM_BASE_URL="https://llm.example.com/v1",
         LLM_API_KEY="sk-llm",
         MEMORY_EMBED_MODEL="text-embedding-3-small",
         MEMORY_EMBED_DIMS=1536)
    prov, dims = wiring()
    assert prov is not None
    assert prov._base_url == "https://llm.example.com/v1"
    assert prov._api_key == "sk-llm"


def test_provider_openai_compat_no_key_degrades(monkeypatch, wiring, caplog):
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="openai_compat",
         MEMORY_EMBED_BASE_URL="", MEMORY_EMBED_API_KEY="",
         LLM_API_KEY="", LLM_BASE_URL="https://x.com/v1",
         MEMORY_EMBED_DIMS=1536)
    with caplog.at_level(logging.WARNING):
        prov, dims = wiring()
    assert prov is None and dims == 1536
    assert any("api_key" in r.message for r in caplog.records)


def test_provider_local_missing_deps_loud_degrades(monkeypatch, wiring, caplog):
    # 强制 sentence_transformers import 失败(无论本机装没装)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="local",
         MEMORY_EMBED_DIMS=512,
         MEMORY_EMBED_MODEL="bge-small-zh-v1.5",
         MEMORY_EMBED_MODEL_PATH="models/bge-small-zh-v1.5")
    with caplog.at_level(logging.ERROR):
        prov, dims = wiring()
    assert prov is None and dims == 512
    assert any("sentence-transformers" in r.message for r in caplog.records)


def test_provider_local_missing_path_loud_degrades(monkeypatch, tmp_path, wiring, caplog):
    fake_mod = type("M", (), {"SentenceTransformer": object})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="local",
         MEMORY_EMBED_DIMS=512,
         MEMORY_EMBED_MODEL="bge-small-zh-v1.5",
         MEMORY_EMBED_MODEL_PATH=str(tmp_path / "nope"))
    with caplog.at_level(logging.ERROR):
        prov, dims = wiring()
    assert prov is None and dims == 512
    assert any("missing" in r.message for r in caplog.records)


def test_provider_local_ok(monkeypatch, tmp_path, wiring):
    fake_mod = type("M", (), {"SentenceTransformer": object})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    _set(monkeypatch,
         MEMORY_EMBED_PROVIDER="local",
         MEMORY_EMBED_DIMS=512,
         MEMORY_EMBED_MODEL="bge-small-zh-v1.5",
         MEMORY_EMBED_MODEL_PATH=str(tmp_path))  # 目录存在
    prov, dims = wiring()
    from twinkle.agentserver.memory.embeddings import SentenceTransformerProvider
    assert isinstance(prov, SentenceTransformerProvider)
    assert prov.model == "bge-small-zh-v1.5" and prov.dims == 512
    assert dims == 512
