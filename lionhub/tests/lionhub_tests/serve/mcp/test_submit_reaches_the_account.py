# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A role's ``codex_home`` reaches the child environment when the leg is started through the
MCP submit verbs, not only through ``li agent --account``.

``agent.submit`` spawns ``li agent`` with the role named; the command it spawns is run here
through the real CLI entry and ``_run_agent``, and the child environment of the turn is read.
``flow.submit`` and ``fanout.submit`` hand the role to the orchestrator, whose workers resolve
the role's account when they are built (``test_a_worker_runs_on_its_roles_account_and_a_bare_
worker_on_none``)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lionhub.providers._cli_subprocess import reveal_redacted
from lionhub.serve.mcp import config, dispatch, jobs

from ..cli.test_agent_resume_model_override import _wire_agent_stubs


class _FakeProcess:
    pid = 4242

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["li-under-test"])
    monkeypatch.setattr(config, "child_actor_suffix", lambda: None)
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})
    home = tmp_path / "codex-home"
    home.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    # The profile keys are honoured only in the global profile directory.
    user_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(user_home))
    agents = user_home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "homed.md").write_text(
        f"---\nmodel: codex/gpt-5.5\nmcp: none\ncodex_home: {home.resolve()}\n---\nA role.\n"
    )
    spawned: list[list[str]] = []

    def popen(argv, **kwargs):
        if argv[0] == "li-under-test":
            spawned.append(list(argv))
        return _FakeProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return cwd, home.resolve(), spawned


def _submit(op, args):
    fingerprint = asyncio.run(dispatch.request(help=op))["schema_fingerprint"]
    response = asyncio.run(
        dispatch.request(ops=[{"op": op, "args": args, "schema_fingerprint": fingerprint}])
    )
    return response["ops"][0]


def test_agent_submit_runs_the_child_in_the_roles_codex_home(submission, monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch
    from lionhub.serve.cli.main import main

    cwd, home, spawned = submission
    result = _submit("agent.submit", {"agent": "homed", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"], result
    argv = spawned[0]
    assert "--agent=homed" in argv

    envs: list[dict] = []
    manifests: list[str] = []

    async def fake_operate(self, instruction=None, **kw):
        envs.append(reveal_redacted(self.chat_model.endpoint._runtime_state.get("env")))
        return "ok"

    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda manifest: manifests.append(json.dumps(manifest)),
        ),
    )

    main(argv[1:])

    assert envs, "the spawned command never reached a turn"
    assert envs[-1]["CODEX_HOME"] == str(home)
    assert json.loads(manifests[0])["codex_home"] == str(home)


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
def test_orchestrated_submits_hand_the_role_to_the_orchestrator(submission, op):
    cwd, _home, spawned = submission
    result = _submit(op, {"agent": "homed", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"], result
    assert "--agent=homed" in spawned[0]
