"""Centralized logging configuration with console and rotating file handlers."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import get_settings

ROOT_LOGGER_NAME = "library"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
MAX_LOG_BYTES = 1_000_000
BACKUP_COUNT = 3
CONSOLE_LEVEL = logging.WARNING

_configured = False


def _configure_root() -> logging.Logger:
    """Attach the console and rotating file handlers to the application root logger once."""
    global _configured
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if _configured:
        return root

    settings = get_settings()
    formatter = logging.Formatter(LOG_FORMAT)
    root.setLevel(settings.log_level)
    root.propagate = False

    console_handler = logging.StreamHandler()
    console_handler.setLevel(CONSOLE_LEVEL)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    log_path = Path(settings.log_file)
    if log_path.parent != Path():
        log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(settings.log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child of the application logger, configuring the handlers on first use."""
    root = _configure_root()
    short_name = name.removeprefix("src.")
    return root.getChild(short_name)


def set_level(level: str) -> None:
    """Change the level of the application logger and its file handler at runtime."""
    root = _configure_root()
    root.setLevel(level)
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.setLevel(level)
