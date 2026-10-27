# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The user-facing output channels (progress, hint, warn, error) as named loggers.

Any part of lionagi can write to them. The CLI decides how they print
(``lionagi.cli._logging.configure_cli_logging``); with nothing configured they are
ordinary loggers.
"""

from __future__ import annotations

import logging

from lionagi.state.engine import mask_credentials

__all__ = (
    "ERROR_LOGGER_NAME",
    "HINT_LOGGER_NAME",
    "PROGRESS_LOGGER_NAME",
    "WARN_LOGGER_NAME",
    "hint",
    "log_error",
    "progress",
    "warn",
)

PROGRESS_LOGGER_NAME = "lionagi.cli.progress"
HINT_LOGGER_NAME = "lionagi.cli.hint"
WARN_LOGGER_NAME = "lionagi.cli.warn"
ERROR_LOGGER_NAME = "lionagi.cli.error"


def progress(msg: str) -> None:
    """Emit a progress update. Suppressed in verbose mode."""
    logging.getLogger(PROGRESS_LOGGER_NAME).info(msg)


def hint(msg: str) -> None:
    """Emit a post-run hint (e.g. resume command). Always visible."""
    logging.getLogger(HINT_LOGGER_NAME).info(msg)


def warn(msg: str) -> None:
    """Emit a non-fatal warning. Always visible, auto-prefixed with 'warning:'."""
    logging.getLogger(WARN_LOGGER_NAME).warning(msg)


def log_error(msg: str) -> None:
    """Emit a user-facing error. Always visible, auto-prefixed with 'error:'."""
    logging.getLogger(ERROR_LOGGER_NAME).error(mask_credentials(msg))
