# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli.main — _handle_play_shortcut."""


from lionagi.cli.main import _handle_play_shortcut


def test_handle_play_shortcut_rewrites_name_to_flow_argv():
    """play <name> [flags] is rewritten to o flow -p <name> [flags]."""
    result = _handle_play_shortcut(["play", "triage", "--x"])
    assert result == ["o", "flow", "-p", "triage", "--x"]


def test_handle_play_shortcut_rejects_flag_before_name(monkeypatch):
    """play --bad returns exit code 1 because flag comes before name."""
    import lionagi.cli._logging as log_mod

    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)
    result = _handle_play_shortcut(["play", "--bad"])
    assert result == 1


def test_handle_play_shortcut_passthrough_for_non_play():
    """Non-play first arg returns argv unchanged."""
    argv = ["agent", "x"]
    result = _handle_play_shortcut(argv)
    assert result == argv
