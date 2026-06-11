"""Shared fixtures for CLI tests."""

import logging

import pytest

# Loggers mutated by lionagi.cli._logging.configure_cli_logging(). Tests that
# drive main() end-to-end trigger that call, which sets propagate=False and
# attaches stderr handlers — breaking caplog for any test that runs later in
# the same worker (e.g. tests/cli/orchestrate/test_flow_spec_file.py).
_CLI_LOGGERS = (
    "lionagi.cli.progress",
    "lionagi.cli.hint",
    "lionagi.cli.warn",
    "lionagi.cli.error",
    "claude-cli",
    "codex-cli",
    "gemini-cli",
    "lionagi",
)


@pytest.fixture(autouse=True)
def _restore_cli_logging():
    """Snapshot and restore CLI logger state around every test."""
    saved = {}
    for name in _CLI_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (logger.level, logger.propagate, list(logger.handlers))
    yield
    for name, (level, propagate, handlers) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers[:] = handlers
