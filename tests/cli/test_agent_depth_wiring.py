# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wiring of LIONAGI_AGENT_DEPTH stamping, and of the nested-agent refusal before it,
into `_run_agent`, `_run_fanout`, `_run_flow`, and the engine subprocess spawn
(`ndjson_from_cli`).

The stamp must land BEFORE any provider/engine spawn, and the refusal before the
stamp: a process inside an agent lionagi started stamps nothing and starts nothing
(see docs/internals/cli.md).
"""

from __future__ import annotations

import os

import pytest

from lionagi.cli import _agent_depth as depth_mod
from lionagi.cli._agent_depth import DEPTH_ENV
from lionagi.ln._proc import _PROCESS_GROUP_MARKER
from lionagi.providers import _subagent_admission as admission
from lionagi.providers._provider_errors import NestedAgentRefusedError


class _StopEarly(Exception):
    """Sentinel raised to abort a function right after the stamp point."""


@pytest.mark.asyncio
async def test_run_agent_stamps_depth_before_any_spawn(monkeypatch):
    import lionagi.cli.agent as agent_mod

    def _boom_build_chat_model(*a, **kw):
        raise _StopEarly("build_chat_model must not be reached before the stamp")

    monkeypatch.setattr(agent_mod, "build_chat_model", _boom_build_chat_model)

    with pytest.raises(_StopEarly):
        await agent_mod._run_agent("claude", "do the thing", agent_name=None)

    # No LIONAGI_SEAT_PROFILES configured, no -a profile -> depth 1.
    assert os.environ[DEPTH_ENV] == "1"


@pytest.mark.asyncio
async def test_run_agent_inside_an_agent_is_refused_before_the_stamp(monkeypatch):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 1)

    def _boom_build_chat_model(*a, **kw):
        raise _StopEarly("build_chat_model must not be reached inside an agent")

    monkeypatch.setattr(agent_mod, "build_chat_model", _boom_build_chat_model)

    with pytest.raises(NestedAgentRefusedError):
        await agent_mod._run_agent("claude", "do the thing", agent_name=None)

    assert DEPTH_ENV not in os.environ


def test_li_agent_inside_an_agent_exits_1_with_the_refusal_on_one_line(monkeypatch, capsys):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    monkeypatch.setattr(admission, "_ancestor_agent_depth", lambda: (1, 4242))
    ran = []

    def _boom_build_chat_model(*a, **kw):
        ran.append(True)
        raise _StopEarly("build_chat_model must not be reached inside an agent")

    monkeypatch.setattr(agent_mod, "build_chat_model", _boom_build_chat_model)

    assert main(["agent", "claude", "do the thing"]) == 1
    err = capsys.readouterr().err
    assert "nested agent refused" in err and "pid 4242" in err
    assert "Traceback" not in err
    assert not ran


@pytest.mark.asyncio
async def test_run_fanout_stamps_depth_before_setup(monkeypatch):
    import lionagi.cli.orchestrate.fanout as fanout_mod

    async def _boom_setup_orchestration(*a, **kw):
        raise _StopEarly("setup_orchestration must not be reached before the stamp")

    monkeypatch.setattr(fanout_mod, "setup_orchestration", _boom_setup_orchestration)

    with pytest.raises(_StopEarly):
        await fanout_mod._run_fanout("claude", "do the thing")

    # Fanout workers are never top-level: always 1.
    assert os.environ[DEPTH_ENV] == "1"


@pytest.mark.asyncio
async def test_run_flow_stamps_depth_before_setup(monkeypatch):
    import lionagi.cli.orchestrate.flow as flow_mod

    async def _boom_setup_orchestration(*a, **kw):
        raise _StopEarly("setup_orchestration must not be reached before the stamp")

    monkeypatch.setattr(flow_mod, "setup_orchestration", _boom_setup_orchestration)

    with pytest.raises(_StopEarly):
        await flow_mod._run_flow("claude", "do the thing")

    # li o flow / li play workers are never top-level: always 1.
    assert os.environ[DEPTH_ENV] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["fanout", "flow"])
async def test_orchestration_inside_an_agent_is_refused_before_setup(monkeypatch, pattern):
    import lionagi.cli.orchestrate.fanout as fanout_mod
    import lionagi.cli.orchestrate.flow as flow_mod

    module, run = {
        "fanout": (fanout_mod, fanout_mod._run_fanout),
        "flow": (flow_mod, flow_mod._run_flow),
    }[pattern]
    monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 1)

    async def _boom_setup_orchestration(*a, **kw):
        raise _StopEarly("setup_orchestration must not be reached inside an agent")

    monkeypatch.setattr(module, "setup_orchestration", _boom_setup_orchestration)

    with pytest.raises(NestedAgentRefusedError):
        await run("claude", "do the thing")
    assert DEPTH_ENV not in os.environ


def test_the_orchestration_runner_exits_1_on_a_nested_refusal(caplog):
    from lionagi.cli.orchestrate import _run_orch_command

    async def _refused():
        raise NestedAgentRefusedError("nested agent refused: test")

    assert _run_orch_command(_refused(), verbose=False) == (None, 1)
    assert "nested agent refused: test" in caplog.text


@pytest.mark.asyncio
async def test_ndjson_from_cli_inherits_stamped_depth(monkeypatch):
    """The CLI engine spawn (shared by claude_code/codex/gemini_code) hands
    create_subprocess_exec the whole of this process's os.environ, so a
    variable stamped here reaches the child with zero endpoint changes."""
    import asyncio

    from lionagi.providers._cli_subprocess import ndjson_from_cli

    # As a run inside an agent inherits it from that agent's process group.
    monkeypatch.setenv(_PROCESS_GROUP_MARKER, "inherited-group-token")
    depth_mod.stamp_agent_depth("implementer")
    assert os.environ[DEPTH_ENV] == "1"

    captured_kwargs: dict = {}
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _spy_create_subprocess_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        # os.environ must still carry the stamp at the moment of spawn.
        captured_kwargs["_depth_at_spawn"] = os.environ.get(DEPTH_ENV)
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create_subprocess_exec)

    chunks = []
    async for obj in ndjson_from_cli(["true"]):
        chunks.append(obj)

    # Every variable this process holds reaches the child, so anything an
    # endpoint stamps is inherited without further wiring. Compared by name
    # only: the spawn env also carries resolved secrets, and a failure here
    # must not print their values. The process-group token is the exception:
    # each child is handed its own, over one a run inside an agent inherits.
    spawn_env = captured_kwargs["env"]
    changed = {key for key, value in os.environ.items() if spawn_env.get(key) != value}
    assert not changed - {_PROCESS_GROUP_MARKER}
    assert spawn_env[_PROCESS_GROUP_MARKER] != "inherited-group-token"
    assert spawn_env[DEPTH_ENV] == "1"
    assert captured_kwargs["_depth_at_spawn"] == "1"


def _background_flow(monkeypatch, tmp_path) -> tuple[int, list]:
    import subprocess

    from lionagi.cli.main import main

    monkeypatch.chdir(tmp_path)
    started: list = []

    class _Proc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        started.append((argv, kwargs))
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    rc = main(["o", "flow", "claude", "do work", "--background", "--save", str(tmp_path / "out")])
    return rc, started


def test_a_background_flow_inside_an_agent_is_refused_before_it_detaches(
    monkeypatch, tmp_path, capsys
):
    # The detached run checks too, but by then this process may have exited and taken the
    # ancestry that shows the run is inside an agent with it.
    monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 1)
    rc, started = _background_flow(monkeypatch, tmp_path)
    assert rc == 1
    assert started == []
    assert "nested agent refused" in capsys.readouterr().err


def test_a_background_flow_outside_any_agent_still_detaches(monkeypatch, tmp_path):
    rc, started = _background_flow(monkeypatch, tmp_path)
    assert rc == 0
    assert len(started) == 1
    assert started[0][1]["start_new_session"] is True
    assert DEPTH_ENV not in started[0][1]["env"]
