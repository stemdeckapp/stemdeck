"""Tests for the rotating file log setup (#291, #292)."""

from __future__ import annotations

import logging

import pytest

from app.core import logging_setup


def _strip_our_handlers(root: logging.Logger) -> None:
    for h in list(root.handlers):
        if getattr(h, logging_setup._HANDLER_MARK, False):
            root.removeHandler(h)
            h.close()


@pytest.fixture()
def _clean_logger(monkeypatch, tmp_path):
    """Point LOGS_DIR at a temp dir and strip our handler around each test.

    Stripping BEFORE the test matters: importing app.main anywhere in the
    suite already ran configure_logging() against the real LOGS_DIR, and the
    idempotence guard would otherwise skip attaching a handler here."""
    monkeypatch.setattr(logging_setup, "LOGS_DIR", tmp_path / "logs")
    root = logging.getLogger("stemdeck")
    saved_level = root.level
    _strip_our_handlers(root)
    yield root
    _strip_our_handlers(root)
    root.setLevel(saved_level)


def _our_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, logging_setup._HANDLER_MARK, False)]


def test_creates_log_file_on_first_record(_clean_logger, tmp_path):
    logging_setup.configure_logging()
    log_file = tmp_path / "logs" / "stemdeck.log"
    assert not log_file.exists()  # delay=True: nothing written yet
    logging.getLogger("stemdeck.test").info("hello file log")
    assert log_file.is_file()
    text = log_file.read_text(encoding="utf-8")
    assert "hello file log" in text
    assert "stemdeck.test" in text


def test_idempotent_across_repeat_calls(_clean_logger):
    logging_setup.configure_logging()
    logging_setup.configure_logging()  # uvicorn --reload re-imports app.main
    assert len(_our_handlers(_clean_logger)) == 1


def test_rotation_keeps_bounded_backups(_clean_logger, monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "_MAX_BYTES", 200)
    monkeypatch.setattr(logging_setup, "_BACKUP_COUNT", 2)
    logging_setup.configure_logging()
    log = logging.getLogger("stemdeck.test")
    for i in range(30):
        log.info("filler record %03d %s", i, "x" * 40)
    logs_dir = tmp_path / "logs"
    assert (logs_dir / "stemdeck.log").is_file()
    assert (logs_dir / "stemdeck.log.1").is_file()
    assert not (logs_dir / "stemdeck.log.3").exists()  # bounded at backupCount


def test_level_from_env(_clean_logger, monkeypatch):
    monkeypatch.setenv("STEMDECK_LOG_LEVEL", "DEBUG")
    logging_setup.configure_logging()
    assert _clean_logger.level == logging.DEBUG


def test_debug_shorthand_wins(_clean_logger, monkeypatch):
    monkeypatch.setenv("STEMDECK_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("STEMDECK_DEBUG", "1")
    logging_setup.configure_logging()
    assert _clean_logger.level == logging.DEBUG


def test_bogus_level_falls_back_to_info(_clean_logger, monkeypatch):
    monkeypatch.setenv("STEMDECK_LOG_LEVEL", "SHOUTING")
    logging_setup.configure_logging()
    assert _clean_logger.level == logging.INFO


def test_unwritable_logs_dir_degrades_gracefully(_clean_logger, monkeypatch, tmp_path):
    # LOGS_DIR path occupied by a *file*: mkdir raises, startup must not.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(logging_setup, "LOGS_DIR", blocker)
    logging_setup.configure_logging()  # must not raise
    assert _our_handlers(_clean_logger) == []
    assert _clean_logger.level == logging.INFO  # level still applied
