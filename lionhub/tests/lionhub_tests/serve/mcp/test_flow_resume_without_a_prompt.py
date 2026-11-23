# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A flow resume needs no prompt, through MCP admission as on the command line.

``li o flow --resume <run id>`` replays the saved checkpoint and reads no prompt, model or
playbook from its arguments. ``flow.submit`` used to refuse ``resume`` alone with the check that
requires a prompt for a new flow, so the request never reached that path.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import pytest

from lionhub.serve.mcp import dispatch, jobs

RUN_ID = "flow-run-0123456789abcdef"


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict) -> dict:
    """A spawn op carrying the fingerprint its verb requires, fetched as a caller fetches it."""
    return {"op": op, "args": args, "schema_fingerprint": call(help=op)["schema_fingerprint"]}


class _RecordingPopen:
    """Stand in for the spawn, keeping the command line it was handed."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.pid = os.getpid()

    def __call__(self, argv: list[str], *a: Any, **kw: Any) -> Any:
        self.argv = list(argv)
        return self

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Nothing is started, nothing is written outside tmp_path, and no playbook or spec
    file above the submitting directory decides what is admitted."""
    popen = _RecordingPopen()
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path / "jobs", raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    submit = tmp_path / "submit"
    submit.mkdir()
    monkeypatch.chdir(submit)
    return popen


def _flow_namespace(argv: list[str]) -> argparse.Namespace:
    """The flow command's own reading of the arguments the spawn was handed."""
    from lionhub.serve.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li", add_help=False)
    flow = add_orchestrate_subparser(parser.add_subparsers(dest="command"))["flow"]
    start = argv.index("flow") + 1
    return flow.parse_args(argv[start:])


def test_a_resume_alone_reaches_the_flow_resume_path(spawned):
    answer = call(ops=[spawn_op("flow.submit", {"resume": RUN_ID})])["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the resume was refused before the spawn"
    ns = _flow_namespace(spawned.argv)
    # The command takes its resume branch on this value and reads no prompt.
    assert ns.resume == RUN_ID
    assert not ns.query


def test_a_new_flow_with_no_prompt_is_still_refused(spawned):
    answer = call(ops=[spawn_op("flow.submit", {"no_mcp_config": True})])["ops"][0]

    assert spawned.argv is None, "the submission reached the spawn"
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "has no prompt and nothing to supply one" in answer["error"]["message"], answer


def test_a_resume_with_a_prompt_is_admitted_as_the_command_line_takes_it(spawned):
    """The command line accepts ``--resume`` beside a prompt and takes the resume branch,
    which reads nothing but the checkpoint. The same request is admitted here and reaches
    that branch with the same arguments."""
    answer = call(ops=[spawn_op("flow.submit", {"resume": RUN_ID, "prompt": "ignored"})])["ops"][0]

    assert answer["ok"] is True, answer
    ns = _flow_namespace(spawned.argv)
    assert ns.resume == RUN_ID
    assert ns.query == ["ignored"]


def test_a_resume_that_changes_the_mcp_choice_is_still_refused(spawned):
    """The resume replays its recorded MCP choice; admitting a resume without a prompt
    does not admit one that tries to change that choice."""
    answer = call(ops=[spawn_op("flow.submit", {"resume": RUN_ID, "no_mcp_config": True})])["ops"][
        0
    ]

    assert spawned.argv is None, "the submission reached the spawn"
    assert answer["ok"] is False, answer
    assert "replays its recorded MCP choice" in answer["error"]["message"], answer
