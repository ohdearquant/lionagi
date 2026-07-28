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


def test_finds_li_in_a_venv_whose_python_is_a_symlink(monkeypatch, tmp_path):
    # Every venv builds bin/python as a symlink to the base interpreter, and
    # installs its console scripts beside the symlink rather than beside the
    # target. Following the link first therefore searches the base
    # installation, where no `li` exists, and silently misses the venv's own.
    base_bin = tmp_path / "base" / "bin"
    base_bin.mkdir(parents=True)
    (base_bin / "python3").write_text("#!/bin/sh\n")

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(base_bin / "python3")
    venv_li = venv_bin / "li"
    venv_li.write_text("#!/bin/sh\n")

    monkeypatch.delenv("LIONAGI_MCP_LI_BIN", raising=False)
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    assert config.li_command() == [str(venv_li)]


def test_path_is_never_consulted(monkeypatch, tmp_path):
    # The setup guide states this as an absolute: a `li` reachable on PATH must
    # not be selected. Without a decoy on PATH the module-fallback test only
    # rules this out when the ambient environment happens to lack `li`.
    decoy_bin = tmp_path / "decoy"
    decoy_bin.mkdir()
    (decoy_bin / "li").write_text("#!/bin/sh\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("#!/bin/sh\n")  # no sibling `li`

    monkeypatch.delenv("LIONAGI_MCP_LI_BIN", raising=False)
    monkeypatch.setenv("PATH", str(decoy_bin))
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    assert config.li_command() == [str(bindir / "python"), "-m", "lionagi.cli"]
