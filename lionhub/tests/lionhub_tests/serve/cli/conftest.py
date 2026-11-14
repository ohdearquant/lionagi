# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for CLI and command adapter tests."""

from dataclasses import dataclass
from pathlib import Path

import pytest
import toml

from tests._engine_loader_support import engine_import_package  # noqa: F401
from tests._kill_scope_test_support import (
    _guard_runner_probes_in_kill_tests as _guard_runner_probes_in_kill_tests,
)

# Loggers mutated by lionhub.orchestration.console.configure_cli_logging(). Tests that
# drive main() end-to-end trigger that call, which sets propagate=False and
# attaches stderr handlers — breaking caplog for any test that runs later in
# the same worker (e.g. lionhub/tests/lionhub_tests/serve/cli/orchestrate/test_flow_spec_file.py).
from ._test_support_cli import restore_cli_logging as restore_cli_logging


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
