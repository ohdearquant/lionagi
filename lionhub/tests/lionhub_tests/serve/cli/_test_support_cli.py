# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Logger isolation for command adapter tests."""

from __future__ import annotations

import logging

import pytest

_CLI_LOGGERS = (
    "lionagi.cli.progress",
    "lionagi.cli.hint",
    "lionagi.cli.warn",
    "lionagi.cli.error",
    "claude-cli",
    "codex-cli",
    "pi-cli",
    "gemini-cli",
    "lionagi",
)


@pytest.fixture(autouse=True)
def restore_cli_logging():
    saved = {}
    for name in _CLI_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (
            logger.level,
            logger.propagate,
            list(logger.handlers),
            getattr(logger, "_lionagi_cli_fallback_level", None),
        )
    for name in _CLI_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
        if hasattr(logger, "_lionagi_cli_fallback_level"):
            del logger._lionagi_cli_fallback_level
    yield
    for name, (level, propagate, handlers, fallback_level) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers[:] = handlers
        if fallback_level is None:
            if hasattr(logger, "_lionagi_cli_fallback_level"):
                del logger._lionagi_cli_fallback_level
        else:
            logger._lionagi_cli_fallback_level = fallback_level
