# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""discover_cli must find a CLI that a detached server's PATH cannot see.

MCP bridges and launchd jobs inherit their launcher's PATH, which routinely
lacks per-user install dirs such as ~/.local/bin. discover_cli therefore probes
cli_fallback_bin_dirs() after PATH, and PATH still wins when it resolves, so an
explicit user override is never shadowed.
"""

import os
import stat
from pathlib import Path

import pytest

import lionhub.providers._cli_subprocess as sub
from lionhub.providers._cli_subprocess import discover_cli


def _make_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return empty


def test_fallback_dir_resolves_a_binary_path_cannot_see(tmp_path, monkeypatch, empty_path):
    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    binary = _make_executable(fallback / "somecli")
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))

    assert discover_cli("somecli") == (True, str(binary))


def test_path_wins_over_the_fallback_dirs(tmp_path, monkeypatch):
    on_path = tmp_path / "on-path"
    on_path.mkdir()
    path_binary = _make_executable(on_path / "somecli")
    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    _make_executable(fallback / "somecli")
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))

    assert discover_cli("somecli") == (True, str(path_binary))


def test_a_non_executable_fallback_file_is_not_a_find(tmp_path, monkeypatch, empty_path):
    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    plain = fallback / "somecli"
    plain.write_text("not a program")
    plain.chmod(0o644)
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))

    available, resolved = discover_cli("somecli")
    assert (available, resolved) == (False, None)


def test_absent_everywhere_is_a_clean_negative(tmp_path, monkeypatch, empty_path):
    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))

    assert discover_cli("somecli") == (False, None)


def test_the_shipped_fallback_dirs_are_the_intended_ones_in_order(monkeypatch, tmp_path):
    """Every other test replaces the real tuple, so a typo, omission, or wrong
    order in the shipped value would leave an installed CLI undiscoverable
    while the whole file stayed green."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sub.cli_fallback_bin_dirs() == (
        tmp_path / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    )


def test_the_first_matching_fallback_dir_wins(tmp_path, monkeypatch, empty_path):
    """Ordering is the point of a tuple: a later dir must not shadow an earlier one."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_binary = _make_executable(first / "somecli")
    _make_executable(second / "somecli")
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (first, second))

    assert discover_cli("somecli") == (True, str(first_binary))


def test_a_later_dir_is_reached_when_the_earlier_one_misses(tmp_path, monkeypatch, empty_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    second_binary = _make_executable(second / "somecli")
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (first, second))

    assert discover_cli("somecli") == (True, str(second_binary))


async def test_a_consumer_binds_the_fallback_resolved_path(tmp_path, monkeypatch, empty_path):
    """discover_cli returning a path is not the same as the consumer spawning
    it: the claude transport reads its own resolver, which must hand back the
    fallback-resolved path rather than the import-time constant.

    Asserting resolve_claude_cli()'s own return value does not cover that: an
    implementation that resolves correctly and still spawns the stale module
    constant passes such an assertion. So this drives the consumer and reads the
    argv it actually builds. CLAUDE_CLI is left None on purpose -- a consumer
    reading the constant instead of calling the resolver spawns None here, which
    is precisely the defect being fenced.
    """
    import lionhub.providers.anthropic.claude_code as cc

    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    binary = _make_executable(fallback / "claude")
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))
    monkeypatch.setattr(cc, "CLAUDE_CLI", None)

    spawned: list[list[str]] = []

    async def _record(cmd, **kwargs):
        spawned.append(list(cmd))
        return
        yield  # pragma: no cover - unreachable; makes this an async generator

    monkeypatch.setattr(cc, "ndjson_from_cli", _record)

    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path)
    async for _ in cc._ndjson_from_cli(request):
        pass

    assert spawned, "the consumer never reached the spawn seam"
    assert spawned[0][0] == str(binary), (
        f"the consumer must spawn the fallback-resolved path, got {spawned[0][0]!r}"
    )


def test_a_resolver_miss_does_not_poison_a_later_hit(tmp_path, monkeypatch, empty_path):
    """A long-lived process that started before the CLI existed must recover
    once it is installed, instead of caching the negative for its lifetime."""
    import lionhub.providers.anthropic.claude_code as cc

    fallback = tmp_path / "user-bin"
    fallback.mkdir()
    monkeypatch.setattr(sub, "cli_fallback_bin_dirs", lambda: (fallback,))
    monkeypatch.setattr(cc, "CLAUDE_CLI", None)
    assert cc.resolve_claude_cli() is None

    binary = _make_executable(fallback / "claude")
    assert cc.resolve_claude_cli() == str(binary)


def test_home_is_read_at_call_time_not_import_time(tmp_path, monkeypatch, empty_path):
    """A HOME set after the module imported must still be honoured: the
    fallback dirs are computed per call, never frozen into a module constant."""
    late_home = tmp_path / "late-home"
    user_bin = late_home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    binary = _make_executable(user_bin / "somecli")
    monkeypatch.setenv("HOME", str(late_home))

    assert discover_cli("somecli") == (True, str(binary))


async def test_missing_claude_cli_error_names_where_it_looked(monkeypatch):
    """The old text prescribed an npm install — the wrong remedy on a machine
    whose install merely sits outside the process PATH."""
    import lionhub.providers.anthropic.claude_code as cc

    monkeypatch.setattr(cc, "CLAUDE_CLI", None)
    # The guard re-discovers now, so a None constant on a machine that HAS the
    # CLI resolves to the real binary; patch the resolver to hold the case open.
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: None)
    gen = cc.stream_cc_cli_events(object())
    with pytest.raises(RuntimeError) as excinfo:
        await gen.__anext__()
    message = str(excinfo.value)
    assert "PATH" in message
    assert "fallback dirs" in message
    assert "npm" not in message
