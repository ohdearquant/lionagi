"""Shared fixtures for CLI tests."""

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
import toml

from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._engine_loader_support import engine_import_package  # noqa: F401

# Loggers mutated by lionhub.orchestration.console.configure_cli_logging(). Tests that
# drive main() end-to-end trigger that call, which sets propagate=False and
# attaches stderr handlers — breaking caplog for any test that runs later in
# the same worker (e.g. tests/cli/orchestrate/test_flow_spec_file.py).


@pytest.fixture(autouse=True)
def _guard_runner_probes_in_kill_tests(request, monkeypatch):
    """Kill tests must explicitly supply every runner PID observation."""
    if request.path.name not in {
        "test_kill.py",
        "test_kill_recorded_workers.py",
        "test_kill_degraded_run.py",
        "test_smart_staleness.py",
    }:
        return

    def unexpected_runner(pid):
        raise AssertionError(f"unconfigured runner process probe: {pid}")

    monkeypatch.setattr("lionagi.cli._kill_service._runner_process_create_time", unexpected_runner)


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
