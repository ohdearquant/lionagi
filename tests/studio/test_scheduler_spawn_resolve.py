# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Regression tests: resolving an absolute `li` executable path independent of
the daemon's own cwd/PATH (build_argv's uv-run-li default depends on both),
plus build_argv's executable_prefix passthrough and prompt substitution."""

from __future__ import annotations

import os
import stat

import pytest

from lionagi.studio.scheduler import subprocess as sched_subprocess

# ---------------------------------------------------------------------------
# resolve_li_executable
# ---------------------------------------------------------------------------


def test_resolve_li_executable_finds_absolute_path_in_normal_env():
    """In the actual dev/test venv, at least one resolution strategy succeeds
    and returns an absolute path — this is the "normal env" half of the
    regression contract."""
    prefix, detail = sched_subprocess.resolve_li_executable()

    assert detail is None
    assert prefix is not None
    assert os.path.isabs(prefix[0])


def test_resolve_li_executable_prefers_shutil_which(monkeypatch, tmp_path):
    """shutil.which is tried first; a hit there short-circuits the rest."""
    fake_li = tmp_path / "li"
    fake_li.write_text("#!/bin/sh\n")
    fake_li.chmod(fake_li.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: str(fake_li))

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert detail is None
    assert prefix == [str(fake_li)]


def test_resolve_li_executable_rejects_relative_path_hit_and_falls_through(monkeypatch, tmp_path):
    """A relative PATH entry (e.g. "." or "relbin") makes shutil.which return
    a relative hit ("relbin/li"). Accepting it as-is would resolve argv[0]
    against whatever cwd the spawn ends up using, not the daemon environment
    that found it — reintroducing cwd-dependent spawn + a PATH-hijack
    surface. The relative hit must be rejected and the returned prefix, if
    any, must be absolute."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("")
    fake_li = bin_dir / "li"
    fake_li.write_text("#!/bin/sh\n")
    fake_li.chmod(fake_li.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: "relbin/li")
    monkeypatch.setattr(sched_subprocess.sys, "executable", str(fake_python))

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert detail is None
    assert prefix != ["relbin/li"]
    assert prefix == [str(fake_li)]
    assert os.path.isabs(prefix[0])


def test_resolve_li_executable_relative_path_hit_with_no_fallback_fails_clean(
    monkeypatch, tmp_path
):
    """Same relative-PATH hit, but with no venv-adjacent file or entry point
    to fall through to: returns (None, detail) naming the rejection, never
    the relative path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("")
    # deliberately no `li` file next to fake_python

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: "relbin/li")
    monkeypatch.setattr(sched_subprocess.sys, "executable", str(fake_python))
    monkeypatch.setattr(sched_subprocess.importlib_metadata, "entry_points", lambda group=None: [])

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert prefix is None
    assert detail is not None
    assert "non-absolute" in detail or "rejected" in detail


def test_resolve_li_executable_falls_back_to_venv_adjacent_file(monkeypatch, tmp_path):
    """No PATH hit, but a `li` file sits next to sys.executable (the normal
    shape of a venv that installed the `li` console script)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("")
    fake_li = bin_dir / "li"
    fake_li.write_text("#!/bin/sh\n")
    fake_li.chmod(fake_li.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: None)
    monkeypatch.setattr(sched_subprocess.sys, "executable", str(fake_python))

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert detail is None
    assert prefix == [str(fake_li)]


def test_resolve_li_executable_falls_back_to_entry_point_module(monkeypatch, tmp_path):
    """No PATH hit and no venv-adjacent `li` file: fall back to invoking the
    registered console-script's target module via `python -m`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("")

    class _FakeEntryPoint:
        name = "li"
        value = "lionagi.cli.main:main"

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: None)
    monkeypatch.setattr(sched_subprocess.sys, "executable", str(fake_python))
    monkeypatch.setattr(
        sched_subprocess.importlib_metadata,
        "entry_points",
        lambda group=None: [_FakeEntryPoint()],
    )

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert detail is None
    assert prefix == [str(fake_python), "-m", "lionagi.cli.main"]


def test_resolve_li_executable_relative_sys_executable_skips_entry_point_fallback(monkeypatch):
    """A relative sys.executable (e.g. "python3" with no bin dir prefix) must
    not leak through the console-entry-point fallback either: that tier
    invokes sys.executable directly as the interpreter, so a relative value
    there is exactly as unsafe as a relative shutil.which() hit. A registered
    `li` entry point must NOT be used in this case."""

    class _FakeEntryPoint:
        name = "li"
        value = "lionagi.cli.main:main"

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: None)
    monkeypatch.setattr(sched_subprocess.sys, "executable", "python3")
    monkeypatch.setattr(
        sched_subprocess.importlib_metadata,
        "entry_points",
        lambda group=None: [_FakeEntryPoint()],
    )

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert prefix is None
    assert prefix != ["python3", "-m", "lionagi.cli.main"]
    assert detail is not None
    assert "sys.executable" in detail
    assert "not absolute" in detail


def test_resolve_li_executable_returns_none_and_names_every_tried_strategy_when_unresolved(
    monkeypatch, tmp_path
):
    """Every strategy fails -> clean (None, detail) with detail naming each
    thing that was tried, not a raw ENOENT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("")
    # deliberately no `li` file next to fake_python

    monkeypatch.setattr(sched_subprocess.shutil, "which", lambda name: None)
    monkeypatch.setattr(sched_subprocess.sys, "executable", str(fake_python))
    monkeypatch.setattr(sched_subprocess.importlib_metadata, "entry_points", lambda group=None: [])

    prefix, detail = sched_subprocess.resolve_li_executable()

    assert prefix is None
    assert detail is not None
    assert "PATH" in detail
    assert "sys.executable" in detail
    assert "console_scripts" in detail


# ---------------------------------------------------------------------------
# build_argv: executable_prefix passthrough
# ---------------------------------------------------------------------------


def _minimal_agent_schedule(**overrides) -> dict:
    base = {
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(overrides)
    return base


def test_build_argv_default_prefix_unchanged_when_executable_prefix_omitted():
    """Backward compatibility: no executable_prefix -> still ["uv","run","li"]."""
    argv, _tmp = sched_subprocess.build_argv(_minimal_agent_schedule(), {})
    assert argv[:3] == ["uv", "run", "li"]


def test_build_argv_uses_explicit_executable_prefix():
    """An absolute executable_prefix replaces the default uv-run-li prefix,
    bypassing uv's cwd-dependent project/venv resolution entirely."""
    argv, _tmp = sched_subprocess.build_argv(
        _minimal_agent_schedule(), {}, executable_prefix=["/opt/venv/bin/li"]
    )
    assert argv[0] == "/opt/venv/bin/li"
    assert argv[1] == "agent"


# ---------------------------------------------------------------------------
# render_action_prompt
# ---------------------------------------------------------------------------


def test_render_action_prompt_substitutes_trigger_context_vars():
    schedule = {"action_prompt": "review {{pr_number}}"}
    result = sched_subprocess.render_action_prompt(schedule, {"pr_number": "42"})
    assert result == "review 42"


def test_render_action_prompt_returns_none_when_no_prompt_template():
    assert sched_subprocess.render_action_prompt({"action_prompt": None}, {}) is None
    assert sched_subprocess.render_action_prompt({}, {}) is None


def test_render_action_prompt_stringifies_non_string_context_values():
    """A present-but-non-string trigger_context value (e.g. the numeric
    metric/value/threshold fields a threshold-alert fire puts directly on
    trigger_context) must be stringified, not passed through raw -- re.sub's
    replacement callback requires a str return."""
    schedule = {"action_prompt": "{{metric}} breached {{threshold}} (observed {{value}})"}
    result = sched_subprocess.render_action_prompt(
        schedule, {"metric": "failed_sessions", "threshold": 5, "value": 9.0}
    )
    assert result == "failed_sessions breached 5 (observed 9.0)"
