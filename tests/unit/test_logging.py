from __future__ import annotations

import io
import logging
import stat
from logging.handlers import RotatingFileHandler

import structlog

from reclaude_bot.logging import LOG_FILE_BACKUP_COUNT, LOG_FILE_MAX_BYTES, configure_logging


def _flush_file_handlers() -> None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.flush()


def test_configure_logging_writes_structured_events_to_a_rotating_file(tmp_path) -> None:
    log_path = tmp_path / "nested" / "reclaude-bot.log"
    configure_logging("INFO", log_path)
    try:
        structlog.get_logger("test_logging.file").info("persistent_event", request_id="safe-id")
        _flush_file_handlers()

        assert '"event": "persistent_event"' in log_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(log_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
        file_handlers = [handler for handler in logging.getLogger().handlers if isinstance(handler, RotatingFileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == LOG_FILE_MAX_BYTES
        assert file_handlers[0].backupCount == LOG_FILE_BACKUP_COUNT
    finally:
        configure_logging("INFO")


def test_file_setup_failure_keeps_stdout_handler_and_reports_diagnostic(tmp_path) -> None:
    stream = io.StringIO()
    stream_handler = logging.StreamHandler(stream)
    root_logger = logging.getLogger()
    root_logger.addHandler(stream_handler)
    bad_path = tmp_path / "not-a-file"
    bad_path.mkdir()
    try:
        configure_logging("INFO", bad_path)
        structlog.get_logger("test_logging.fallback").info("stdout_event")
        stream_handler.flush()
        output = stream.getvalue()
        assert "log_file_setup_failed" in output
        assert '"fallback": "stdout"' in output
        assert '"event": "stdout_event"' in output
    finally:
        root_logger.removeHandler(stream_handler)
        configure_logging("INFO")
