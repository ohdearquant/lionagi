# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_claude_mirror(monkeypatch):
    """Keep Studio's in-process mirror disabled for tests that need its settings."""
    import lionagi.studio.config as config_mod

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", False)


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


@pytest.fixture
def _restore_cli_logging():
    """Restore logger state after scheduler tests exercise CLI behavior."""
    import logging

    saved = {}
    for name in _CLI_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (
            logger.level,
            logger.propagate,
            list(logger.handlers),
            getattr(logger, "_lionagi_cli_fallback_level", None),
        )
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
