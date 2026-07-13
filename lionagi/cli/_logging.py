# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Named logging channels for CLI stdout/stderr output (progress, hint, warn, error)."""

from __future__ import annotations

import logging
import sys

__all__ = (
    "configure_cli_logging",
    "progress",
    "hint",
    "warn",
    "log_error",
)

_PROGRESS_LOGGER_NAME = "lionagi.cli.progress"
_HINT_LOGGER_NAME = "lionagi.cli.hint"
_WARN_LOGGER_NAME = "lionagi.cli.warn"
_ERROR_LOGGER_NAME = "lionagi.cli.error"

# Library loggers silenced in non-verbose mode — they emit the provider's
# own streaming chunks, which duplicate our progress if both are on.
_LIBRARY_LOGGERS = (
    "claude-cli",
    "codex-cli",
    "gemini-cli",
    "lionagi",
)


class _BareFormatter(logging.Formatter):
    """Emit the message as-is, no level/timestamp/module prefix."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class _ErrorFormatter(logging.Formatter):
    """Prefix error messages with `error: ` for a consistent look."""

    def format(self, record: logging.LogRecord) -> str:
        return f"error: {record.getMessage()}"


class _WarnFormatter(logging.Formatter):
    """Prefix non-fatal warnings with `warning: `."""

    def format(self, record: logging.LogRecord) -> str:
        return f"warning: {record.getMessage()}"


class _LazyStderrHandler(logging.StreamHandler):
    """Re-bind to ``sys.stderr`` on every emit so pytest capture-swapping doesn't leave a closed file reference."""

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _setup_channel(name: str, level: int, formatter: logging.Formatter) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    # Guard against re-configuration (e.g. nested main() in tests).
    if not logger.handlers:
        handler = _LazyStderrHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def configure_cli_logging(verbose: bool) -> None:
    """Configure the three CLI channels plus library logger levels. Idempotent."""
    # Progress: on in normal mode, off in verbose (provider stream replaces it)
    _setup_channel(
        _PROGRESS_LOGGER_NAME,
        logging.WARNING if verbose else logging.INFO,
        _BareFormatter(),
    )
    # Hints: always on (post-run help like "to resume: ...")
    _setup_channel(_HINT_LOGGER_NAME, logging.INFO, _BareFormatter())
    # Warnings: always on (non-fatal notices like "not a team member")
    _setup_channel(_WARN_LOGGER_NAME, logging.WARNING, _WarnFormatter())
    # Errors: always on
    _setup_channel(_ERROR_LOGGER_NAME, logging.ERROR, _ErrorFormatter())

    # Library loggers: inverse of progress — quiet in normal mode, chatty in verbose
    library_level = logging.INFO if verbose else logging.WARNING
    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(library_level)


def progress(msg: str) -> None:
    """Emit a progress update. Suppressed in verbose mode."""
    logging.getLogger(_PROGRESS_LOGGER_NAME).info(msg)


def hint(msg: str) -> None:
    """Emit a post-run hint (e.g. resume command). Always visible."""
    logging.getLogger(_HINT_LOGGER_NAME).info(msg)


def warn(msg: str) -> None:
    """Emit a non-fatal warning. Always visible, auto-prefixed with 'warning:'."""
    logging.getLogger(_WARN_LOGGER_NAME).warning(msg)


def log_error(msg: str) -> None:
    """Emit a user-facing error. Always visible, auto-prefixed with 'error:'."""
    logging.getLogger(_ERROR_LOGGER_NAME).error(msg)
