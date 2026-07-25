# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for li CLI resolution (config.li_command).

The server runs inside lionagi's own environment, so the CLI it spawns is the
one installed next to the running interpreter — no working-tree hunting and no
dependency resync on spawn.
"""

from __future__ import annotations

import sys

from lionagi.mcp import config


def test_li_bin_override_wins(monkeypatch):
    monkeypatch.setenv("LIONAGI_MCP_LI_BIN", "/usr/bin/li --flag")
    assert config.li_command() == ["/usr/bin/li", "--flag"]


def test_prefers_li_next_to_interpreter(monkeypatch, tmp_path):
    monkeypatch.delenv("LIONAGI_MCP_LI_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("#!/bin/sh\n")
    li = bindir / "li"
    li.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    assert config.li_command() == [str(li)]


def test_module_fallback_when_no_sibling_li(monkeypatch, tmp_path):
    monkeypatch.delenv("LIONAGI_MCP_LI_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("#!/bin/sh\n")  # no sibling `li`
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    cmd = config.li_command()
    assert cmd == [str(bindir / "python"), "-m", "lionagi.cli"]
