from __future__ import annotations

import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
_FILE_HANDLER_MARKER = "_reclaude_bot_file_handler"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep active and newly rotated log files private to the bot user."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            stream.close()
            raise
        return stream


def _remove_file_handler(root_logger: logging.Logger) -> None:
    for handler in root_logger.handlers[:]:
        if getattr(handler, _FILE_HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()


def _add_file_handler(root_logger: logging.Logger, log_file_path: str | Path) -> None:
    path = Path(log_file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(root_logger.level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _FILE_HANDLER_MARKER, True)
    root_logger.addHandler(handler)


def configure_logging(level: str = "INFO", log_file_path: str | Path | None = None) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=numeric_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    _remove_file_handler(root_logger)
    if log_file_path is None or (isinstance(log_file_path, str) and not log_file_path.strip()):
        return
    try:
        _add_file_handler(root_logger, log_file_path)
    except (OSError, TypeError, ValueError) as exc:
        # Persistent logging is optional: keep stdout available when its mount is
        # missing or not writable, while making the deployment failure explicit.
        structlog.get_logger(__name__).error(
            "log_file_setup_failed",
            log_file_path=str(log_file_path),
            error=str(exc),
            fallback="stdout",
        )
