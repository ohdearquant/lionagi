# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Preflight the positional limit shared by MCP spawn commands."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lionagi.mcp import dispatch, jobs, verbs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict, *, playbook: str | None = None) -> dict:
    """Build a spawn op with the schema fingerprint a caller must echo."""
    target: Any = {"verb": op, "playbook": playbook} if playbook is not None else op
    return {"op": op, "args": args, "schema_fingerprint": call(help=target)["schema_fingerprint"]}


@pytest.fixture
def submit_dir(monkeypatch, tmp_path):
    """Provide an isolated playbook for the play submission cases."""
    monkeypatch.chdir(tmp_path)
    books = tmp_path / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "probe.playbook.yaml").write_text("name: probe\nprompt: summarize this\n")
    return tmp_path


@pytest.fixture
def submitted(monkeypatch):
    """Capture calls at the job boundary; no process or run record is created."""
    calls: list[dict[str, Any]] = []

    def fake_submit(kind, flags, **kwargs):
        calls.append({"kind": kind, "flags": list(flags), **kwargs})
        return {"run_id": "rid", "status": "running", "terminal": False, "outcome": None}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    return calls


_SPAWN_CASES = (
    ("agent.submit", "agent", None),
    ("flow.submit", "flow", None),
    ("fanout.submit", "fanout", None),
    ("play.submit", "play", "probe"),
)


def _args(query: list[str], playbook: str | None) -> dict:
    args: dict[str, Any] = {"query": query, "no_mcp_config": True}
    if playbook is not None:
        args["playbook"] = playbook
    return args


def _add_prompt_source(args: dict[str, Any], source: str, submit_dir) -> None:
    if source == "prompt":
        args["prompt"] = "review the patch"
        return
    prompt_path = submit_dir / "prompt.txt"
    prompt_path.write_text("review the patch")
    args["prompt_file"] = str(prompt_path)


@pytest.mark.parametrize(("verb", "kind", "playbook"), _SPAWN_CASES)
def test_three_positionals_are_refused_before_every_affected_spawn(
    submit_dir, submitted, verb, kind, playbook
):
    answer = call(
        ops=[
            spawn_op(
                verb,
                _args(["claude/opus", "review the patch", "unexpected"], playbook),
                playbook=playbook,
            )
        ]
    )["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    message = answer["error"]["message"]
    assert verb in message
    assert "3 positional values" in message
    assert "[MODEL] PROMPT" in message
    assert "quote a multi-word prompt" in message
    assert submitted == [], f"{kind} reached jobs.submit"


@pytest.mark.parametrize(("verb", "kind", "playbook"), _SPAWN_CASES)
@pytest.mark.parametrize("source", ("prompt", "prompt_file"))
def test_server_owned_prompt_counts_toward_every_spawn_limit(
    submit_dir, submitted, verb, kind, playbook, source
):
    args = _args(["claude/opus", "positional prompt"], playbook)
    _add_prompt_source(args, source, submit_dir)

    answer = call(ops=[spawn_op(verb, args, playbook=playbook)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    message = answer["error"]["message"]
    assert verb in message
    assert "3 positional values" in message
    assert "2 in 'query' plus a resolved prompt" in message
    assert submitted == [], f"{kind} reached jobs.submit"


@pytest.mark.parametrize(("verb", "kind", "playbook"), _SPAWN_CASES)
def test_two_positionals_still_reach_every_affected_spawn(
    submit_dir, submitted, verb, kind, playbook
):
    answer = call(
        ops=[
            spawn_op(
                verb,
                _args(["claude/opus", "review the patch"], playbook),
                playbook=playbook,
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert submitted[-1]["kind"] == kind
    assert submitted[-1]["flags"][-3:] == ["--", "claude/opus", "review the patch"]


@pytest.mark.parametrize(("verb", "kind", "playbook"), _SPAWN_CASES)
@pytest.mark.parametrize("source", ("prompt", "prompt_file"))
def test_one_query_plus_server_owned_prompt_still_reaches_every_spawn(
    submit_dir, submitted, verb, kind, playbook, source
):
    args = _args(["claude/opus"], playbook)
    _add_prompt_source(args, source, submit_dir)

    answer = call(ops=[spawn_op(verb, args, playbook=playbook)])["ops"][0]

    assert answer["ok"] is True, answer
    assert submitted[-1]["kind"] == kind
    assert submitted[-1]["flags"][-2:] == ["--", "claude/opus"]
    assert submitted[-1]["prompt"] == "review the patch"


def test_every_registered_spawn_kind_declares_a_positional_limit():
    registered = {verb.job_kind for verb in verbs.VERBS.values() if verb.executor == "spawn"}
    assert registered == set(dispatch._POSITIONAL_LIMITS)
