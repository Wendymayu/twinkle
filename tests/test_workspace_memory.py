import importlib


def test_ensure_workspace_creates_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("TWINKLE_MEMORY_DIR", str(tmp_path / "mem"))
    import twinkle.config as cfg
    importlib.reload(cfg)
    import twinkle.workspace as ws
    importlib.reload(ws)
    ws.ensure_workspace_dir()
    assert (tmp_path / "mem").is_dir()
    assert (tmp_path / "mem" / "daily_memory").is_dir()
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("TWINKLE_MEMORY_DIR", raising=False)
    importlib.reload(cfg)  # restore for downstream tests
