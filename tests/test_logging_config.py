import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from twinkle import logging_config


@pytest.fixture
def restore_root_logging():
    """快照 root logger 状态;测试后还原并关闭新增 handler。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        if h not in saved_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setattr(logging_config, "LOG_DIR", str(d))
    return d


def test_gateway_log_file_created_and_written(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("gateway")
    logging.getLogger("twinkle.gateway.test").info("hello-gw")
    for h in logging.getLogger().handlers:
        h.flush()
    gw = tmp_log_dir / "gateway.log"
    assert gw.is_file()
    assert "hello-gw" in gw.read_text(encoding="utf-8")


def test_server_log_file_created_and_written(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("agentserver")
    logging.getLogger("twinkle.agentserver.test").info("hello-srv")
    for h in logging.getLogger().handlers:
        h.flush()
    srv = tmp_log_dir / "server.log"
    assert srv.is_file()
    assert "hello-srv" in srv.read_text(encoding="utf-8")


def test_console_stderr_handler_retained(tmp_log_dir, restore_root_logging):
    # TimedRotatingFileHandler 是 StreamHandler 子类,要排除 FileHandler 子类
    logging_config.setup_logging("agentserver")
    root = logging.getLogger()
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


def test_setup_logging_idempotent_no_duplicate_file_handlers(
    tmp_log_dir, restore_root_logging
):
    logging_config.setup_logging("agentserver")
    logging_config.setup_logging("agentserver")
    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1


def test_gateway_role_does_not_create_server_log(tmp_log_dir, restore_root_logging):
    logging_config.setup_logging("gateway")
    assert not (tmp_log_dir / "server.log").exists()
    assert (tmp_log_dir / "gateway.log").is_file()
