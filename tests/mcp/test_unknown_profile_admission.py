# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A submitted agent profile name that resolves to nothing, checked before spawn.

Unchecked, this used to die inside the spawned process on its own first
second: the job record already existed by then, so the caller was left
holding a run_id for a run that would never reach a terminal status for a
reason job.status could name. The check runs the same resolver `agent.submit`
itself uses (see test_roster.py for its directory-precedence rules), so it
can only refuse a name the run would also have refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lionagi.mcp import dispatch, jobs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict, *, playbook: str | None = None) -> dict:
    target: Any = {"verb": op, "playbook": playbook} if playbook is not None else op
    return {"op": op, "args": args, "schema_fingerprint": call(help=target)["schema_fingerprint"]}


@pytest.fixture
def isolated_profiles(monkeypatch, tmp_path: Path):
    """A project root with one real profile, and no real ``~/.lionagi/`` in reach.

    Mirrors test_roster.py's ``roots`` fixture: HOME is redirected so this
    can't accidentally resolve against whatever the machine running it has.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lionagi" / "agents").mkdir(parents=True)
    project_agents = project / ".lionagi" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "reviewer.md").write_text("---\nmodel: claude_code/sonnet\n---\nReview it.\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def spawned(monkeypatch):
    """Nothing is actually started; the argv/kwargs handed to jobs.submit is kept."""
    calls: list[dict[str, Any]] = []

    def fake_submit(kind, flags, **kwargs):
        calls.append({"kind": kind, "flags": list(flags), **kwargs})
        return {"run_id": "rid", "status": "running", "terminal": False, "outcome": None}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    return calls


@pytest.mark.parametrize("verb", ("agent.submit", "flow.submit", "fanout.submit"))
def test_an_unresolvable_profile_name_never_reaches_the_spawn(isolated_profiles, spawned, verb):
    answer = call(ops=[spawn_op(verb, {"agent": "no-such-profile", "prompt": "do it"})])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert spawned == [], "the submission reached the spawn"
    message = answer["error"]["message"]
    assert "no-such-profile" in message, message
    # The loader's own remediation: what does exist, so a caller can correct
    # the name without a second round trip.
    assert "reviewer" in message, message


def test_a_resolvable_profile_name_reaches_the_spawn(isolated_profiles, spawned):
    answer = call(ops=[spawn_op("agent.submit", {"agent": "reviewer", "prompt": "do it"})])["ops"][
        0
    ]

    assert answer["ok"] is True, answer
    assert len(spawned) == 1, "the submission was refused before the spawn"
    assert "--agent=reviewer" in spawned[0]["flags"]


def test_a_submission_naming_no_agent_at_all_is_unaffected(isolated_profiles, spawned):
    """The check only fires when 'agent' is actually given a name."""
    answer = call(
        ops=[spawn_op("flow.submit", {"query": ["claude_code/claude-opus-5"], "prompt": "do it"})]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert len(spawned) == 1


def test_the_refusal_names_a_profile_this_cwd_would_actually_find(isolated_profiles, spawned):
    """A name declared in the project root resolves — the check is not a
    hardcoded allowlist, it is the same directory walk agent.submit uses."""
    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"agent": "reviewer", "prompt": "do it", "cwd": str(isolated_profiles)},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
