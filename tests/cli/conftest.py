"""Shared fixtures for CLI tests."""

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
import toml

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


@dataclass
class CodexHome:
    """A private ``$CODEX_HOME`` for a test, plus the ambient server table.

    Anything that hands codex an exclusive server set has to know which servers
    codex would load on its own, and anything that forwards secret-bearing
    fields writes a profile file into this directory. Both read the real
    operator tree unless a test redirects them.
    """

    path: Path

    def write_config(self, servers: dict) -> None:
        (self.path / "config.toml").write_text(toml.dumps({"mcp_servers": servers}))

    def profile_files(self) -> list[Path]:
        return sorted(self.path.glob("lionagi-mcp-*.config.toml"))


@pytest.fixture
def codex_home(monkeypatch, tmp_path) -> CodexHome:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return CodexHome(home)
