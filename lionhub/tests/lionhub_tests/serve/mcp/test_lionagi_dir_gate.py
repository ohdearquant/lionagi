# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""With ``lionagi_dir_trust.gate: mcp``, the MCP server takes project ``.lionagi`` files only from a
trusted directory, at submit and in the job it starts (ADR-0088 Amendment 2). With the gate off,
nothing changes."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from lionagi._lionagi_dir_trust import GATE_ENV_VAR, RECORD_KEY, UntrustedLionagiDirError
from lionhub.orchestration.spawn import load_agent_profile
from lionhub.serve.cli.orchestrate._orchestration import resolve_worker_spec
from lionhub.serve.mcp import _notify_hook, dispatch, jobs, roster


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict, *, playbook: str | None = None) -> dict:
    target: Any = op
    if playbook is not None:
        target = {"verb": op, "playbook": playbook}
        if "cwd" in args:
            target["cwd"] = args["cwd"]
    return {"op": op, "args": args, "schema_fingerprint": call(help=target)["schema_fingerprint"]}


class _RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pid = os.getpid()

    def __call__(self, argv: list[str], *a: Any, **kw: Any) -> Any:
        # Profile lookup asks git for the repository root; the project here is no repository.
        if argv and argv[0] == "git":
            raise FileNotFoundError("git")
        self.calls.append({"argv": list(argv), "env": dict(kw.get("env") or {})})
        return self

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def world(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    """A project whose .lionagi declares a profile, a playbook naming it, and nothing trusted."""
    home = tmp_path / "home"
    (home / ".lionagi" / "playbooks").mkdir(parents=True)
    project = tmp_path / "project"
    lionagi_dir = project / ".lionagi"
    (lionagi_dir / "agents").mkdir(parents=True)
    (lionagi_dir / "agents" / "worker.md").write_text(
        "---\nmodel: claude_code/sonnet\n---\nWork.\n"
    )
    (lionagi_dir / "playbooks").mkdir()
    (lionagi_dir / "playbooks" / "local.playbook.yaml").write_text(
        "name: local\nmodel: claude_code/sonnet\nprompt: work\n"
    )
    # A user-level playbook is never gated; this one names the project profile.
    (home / ".lionagi" / "playbooks" / "global.playbook.yaml").write_text(
        "name: global\nagent: worker\nprompt: work\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(GATE_ENV_VAR, raising=False)
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr(jobs.config, "JOBS_DIR", jobs_root, raising=False)
    popen = _RecordingPopen()
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    monkeypatch.chdir(project)
    return SimpleNamespace(
        home=home, project=project, lionagi=lionagi_dir, jobs=jobs_root, popen=popen, tmp=tmp_path
    )


def _gate(world, *trusted: Path, gate: str = "mcp") -> None:
    (world.home / ".lionagi" / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "lionagi_dir_trust": {"gate": gate},
                RECORD_KEY: [os.path.realpath(d) for d in trusted],
            }
        )
    )


def _no_job(world) -> None:
    root = world.jobs
    assert not root.exists() or list(root.iterdir()) == [], (
        "a refused submission left a job directory"
    )
    assert world.popen.calls == [], "a refused submission reached the spawn"
    assert jobs.list_jobs() == []


def _refused(answer: dict, what: str, path: Path, lionagi_dir: Path) -> None:
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    real_dir = os.path.realpath(lionagi_dir)
    assert answer["error"]["message"] == (
        f"{what} {os.path.realpath(path)} comes from {real_dir}, which is not a trusted .lionagi "
        "directory; this run was started through the MCP server with lionagi_dir_trust.gate set "
        f"to mcp. Run `li lionagi-dir trust {os.path.dirname(real_dir)}` to trust it, or start "
        "the run from a directory without project files."
    ), answer


_PROFILE_SUBMITS = (
    ("agent.submit", {"agent": "worker", "prompt": "do it"}, None),
    ("flow.submit", {"agent": "worker", "prompt": "do it"}, None),
    ("flow.submit", {"agent": "worker", "prompt": "do it", "dry_run": True}, None),
    ("fanout.submit", {"agent": "worker", "prompt": "do it"}, None),
    ("play.submit", {"playbook": "global"}, "global"),
)
_IDS = ("agent", "flow", "flow-dry-run", "fanout", "play")


# Gate off: what runs today runs unchanged.


@pytest.mark.parametrize(("verb", "args", "playbook"), _PROFILE_SUBMITS, ids=_IDS)
def test_gate_off_each_submit_starts_with_no_gate_variable(world, verb, args, playbook):
    answer = call(ops=[spawn_op(verb, args, playbook=playbook)])["ops"][0]
    assert answer["ok"] is True, answer
    assert len(world.popen.calls) == 1
    assert GATE_ENV_VAR not in world.popen.calls[0]["env"]


def test_gate_off_a_project_profile_keeps_its_bypass(world):
    (world.lionagi / "agents" / "worker.md").write_text(
        "---\nmodel: claude_code/sonnet\nbypass: true\n---\nWork.\n"
    )
    _gate(world, gate="off")
    answer = call(ops=[spawn_op("agent.submit", {"agent": "worker", "prompt": "do it"})])["ops"][0]
    assert answer["ok"] is True, answer
    assert load_agent_profile("worker").bypass is True


def test_gate_off_the_catalog_has_no_trusted_field(world):
    shown = roster.profile_show("worker", cwd=str(world.project))
    assert shown["source"]["scope"] == "project"
    assert "trusted" not in shown["source"]
    listed = roster.profile_list(cwd=str(world.project))["profiles"]
    assert all("trusted" not in entry["source"] for entry in listed)


def test_gate_off_the_terminal_hook_runs_a_project_notify_command(world):
    (world.lionagi / "settings.yaml").write_text(_SETTINGS_FILES["notify"])
    argv, reason = _notify_hook._resolve_command(None, cwd=str(world.project))
    assert reason is None
    assert argv is not None and argv[0] == "echo"


# Gate on: refused at submit, before any job record or directory exists.


@pytest.mark.parametrize(("verb", "args", "playbook"), _PROFILE_SUBMITS, ids=_IDS)
def test_gate_on_each_submit_refuses_an_untrusted_project_profile(world, verb, args, playbook):
    _gate(world)
    answer = call(ops=[spawn_op(verb, args, playbook=playbook)])["ops"][0]
    _refused(answer, "agent profile", world.lionagi / "agents" / "worker.md", world.lionagi)
    _no_job(world)


@pytest.mark.parametrize("verb", ("flow.submit", "play.submit"))
def test_gate_on_an_untrusted_project_playbook_is_refused(world, verb):
    _gate(world)
    answer = call(ops=[spawn_op(verb, {"playbook": "local"}, playbook="local")])["ops"][0]
    _refused(answer, "playbook", world.lionagi / "playbooks" / "local.playbook.yaml", world.lionagi)
    _no_job(world)


def test_gate_on_a_trusted_playbook_naming_an_untrusted_profile_is_refused(world):
    child = world.project / "service"
    child_dir = child / ".lionagi"
    (child_dir / "playbooks").mkdir(parents=True)
    (child_dir / "playbooks" / "svc.playbook.yaml").write_text(
        "name: svc\nagent: worker\nprompt: work\n"
    )
    # The child directory is trusted; the profile comes from the parent's, which is not.
    _gate(world, child_dir)
    args = {"playbook": "svc", "cwd": str(child)}
    answer = call(ops=[spawn_op("play.submit", args, playbook="svc")])["ops"][0]
    _refused(answer, "agent profile", world.lionagi / "agents" / "worker.md", world.lionagi)
    _no_job(world)


_SETTINGS_FILES = {
    "notify": "notify:\n  on_terminal:\n    adapter:\n      kind: exec\n      argv: [echo, done]\n",
    "dispatch": "dispatch:\n  notify_template: [echo, '{run_id}']\n",
    "hooks_external": (
        "hooks_external:\n  PreToolUse:\n    - hooks:\n        - type: command\n"
        "          command: [echo, hi]\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_SETTINGS_FILES))
def test_gate_on_an_untrusted_settings_layer_refuses_at_submit_and_in_the_job(
    world, monkeypatch, name
):
    settings = world.lionagi / "settings.yaml"
    settings.write_text(_SETTINGS_FILES[name])
    _gate(world)
    args = {"query": ["claude_code/sonnet"], "prompt": "do it"}
    answer = call(ops=[spawn_op("flow.submit", args)])["ops"][0]
    _refused(answer, "project settings", settings, world.lionagi)
    _no_job(world)

    from lionagi._settings import load_settings

    monkeypatch.setenv(GATE_ENV_VAR, "1")
    with pytest.raises(UntrustedLionagiDirError):
        load_settings(project_dir=world.project)


def test_gate_on_the_terminal_hook_does_not_run_an_untrusted_notify_command(world):
    (world.lionagi / "settings.yaml").write_text(_SETTINGS_FILES["notify"])
    _gate(world)
    assert _notify_hook._resolve_command(None, cwd=str(world.project)) == (
        None,
        "notify_settings_untrusted_lionagi_dir",
    )


def test_gate_on_a_graph_node_naming_an_untrusted_profile_is_refused(world):
    graph = world.tmp / "g.json"
    graph.write_text(
        json.dumps({"nodes": [{"id": "a", "agent": "worker", "prompt": "do a", "needs": []}]})
    )
    _gate(world)
    args = {"graph": str(graph), "cwd": str(world.project)}
    answer = call(ops=[spawn_op("flow.submit", args)])["ops"][0]
    _refused(answer, "agent profile", world.lionagi / "agents" / "worker.md", world.lionagi)
    _no_job(world)


def test_gate_on_an_invalid_setting_refuses_every_submit_as_unavailable(world):
    (world.home / ".lionagi" / "settings.yaml").write_text("lionagi_dir_trust:\n  gate: on\n")
    answer = call(ops=[spawn_op("agent.submit", {"agent": "worker", "prompt": "do it"})])["ops"][0]
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "unavailable", answer
    _no_job(world)


# Gate on: a trusted directory passes, and the job carries the gate.


@pytest.mark.parametrize(("verb", "args", "playbook"), _PROFILE_SUBMITS, ids=_IDS)
def test_gate_on_a_trusted_directory_starts_a_gated_job(world, verb, args, playbook):
    _gate(world, world.lionagi)
    answer = call(ops=[spawn_op(verb, args, playbook=playbook)])["ops"][0]
    assert answer["ok"] is True, answer
    assert len(world.popen.calls) == 1
    assert world.popen.calls[0]["env"][GATE_ENV_VAR] == "1"


def test_gate_on_a_link_to_a_trusted_directory_passes(world):
    shared = world.tmp / "shared" / ".lionagi"
    (shared / "agents").mkdir(parents=True)
    (shared / "agents" / "linked.md").write_text("---\nmodel: claude_code/sonnet\n---\nLinked.\n")
    other = world.tmp / "other"
    other.mkdir()
    (other / ".lionagi").symlink_to(shared, target_is_directory=True)
    _gate(world, shared)
    args = {"agent": "linked", "prompt": "do it", "cwd": str(other)}
    answer = call(ops=[spawn_op("agent.submit", args)])["ops"][0]
    assert answer["ok"] is True, answer
    assert world.popen.calls[0]["env"][GATE_ENV_VAR] == "1"


# In the job: the variable gates every place a project directory supplies something.


def test_in_a_gated_process_a_flow_worker_from_an_untrusted_profile_refuses(world, monkeypatch):
    assert resolve_worker_spec("worker")[1] is not None
    monkeypatch.setenv(GATE_ENV_VAR, "1")
    # Refused, never read as a missing profile and run as the model spec "worker".
    with pytest.raises(UntrustedLionagiDirError):
        resolve_worker_spec("worker")


def test_a_person_typed_run_without_the_variable_is_unchanged(world):
    _gate(world)
    assert load_agent_profile("worker").model == "claude_code/sonnet"


def test_the_variable_alone_gates_a_person_typed_run(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV_VAR, "1")
    with pytest.raises(UntrustedLionagiDirError):
        load_agent_profile("worker")


def test_a_profile_edited_under_a_trusted_directory_is_used_as_edited(world, monkeypatch):
    _gate(world, world.lionagi)
    monkeypatch.setenv(GATE_ENV_VAR, "1")
    assert load_agent_profile("worker").model == "claude_code/sonnet"
    (world.lionagi / "agents" / "worker.md").write_text(
        "---\nmodel: claude_code/opus\nbypass: true\n---\nEdited.\n"
    )
    edited = load_agent_profile("worker")
    assert (edited.model, edited.bypass) == ("claude_code/opus", True)


# The catalog says which project profiles a gated submit would refuse.


def test_gate_on_the_catalog_marks_project_profiles_by_trust(world):
    _gate(world)
    shown = roster.profile_show("worker", cwd=str(world.project))
    assert shown["source"]["trusted"] is False
    listed = {e["name"]: e for e in roster.profile_list(cwd=str(world.project))["profiles"]}
    assert listed["worker"]["source"]["trusted"] is False

    _gate(world, world.lionagi)
    assert roster.profile_show("worker", cwd=str(world.project))["source"]["trusted"] is True
    listed = {e["name"]: e for e in roster.profile_list(cwd=str(world.project))["profiles"]}
    assert listed["worker"]["source"]["trusted"] is True
