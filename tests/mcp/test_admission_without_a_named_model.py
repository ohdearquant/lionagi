# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What the pre-spawn model check admits, per command.

The check exists to refuse a submission the command would reject in its first
second, because such a run never reaches the hook that records an end: the job
stays non-terminal and a caller waiting on it waits forever. So its answer has
to track what each command actually does with the arguments, not a general idea
of what a model source looks like.

Two commands disagreed with it.

Flow and fanout read "no model and no agent" as a request to orchestrate and
answer it with the default orchestrator profile. A submission naming neither is
therefore complete, and refusing it here refused a run that would have started.

An agent reads its positionals as ``[MODEL] PROMPT``, so a lone positional is
the prompt. Treating any positional as a model admitted ``query=["do it"]`` with
no model anywhere, which is exactly the run that dies on start.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from lionagi.mcp import dispatch, jobs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict) -> dict:
    """A spawn op carrying the fingerprint its verb requires.

    Fetched the way a caller has to fetch it, so these tests exercise the
    round-trip rather than reaching past it.
    """
    return {"op": op, "args": args, "schema_fingerprint": call(help=op)["schema_fingerprint"]}


class _RecordingPopen:
    """Stand in for the spawn, keeping the command line it was handed.

    The pid it reports is this process's own: the code after the spawn reads the
    child's start time and process group, and a made-up number either fails
    those reads or names whatever process happens to hold it.
    """

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.pid = os.getpid()

    def __call__(self, argv: list[str], *a: Any, **kw: Any) -> Any:
        self.argv = list(argv)
        return self


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Nothing is started and nothing is written outside tmp_path."""
    popen = _RecordingPopen()
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return popen


def _positionals(argv: list[str]) -> list[str]:
    """Everything after the sentinel: what the command reads as positionals."""
    return argv[argv.index("--") + 1 :]


# ── the orchestrating commands ───────────────────────────────────────────────


def test_a_flow_naming_neither_a_model_nor_an_agent_reaches_the_spawn(spawned):
    answer = call(
        ops=[spawn_op("flow.submit", {"prompt": "summarize this", "no_mcp_config": True})]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    # The prompt is the sole positional, and nothing named a model: the run
    # starts and resolves the default orchestrator profile for itself.
    assert _positionals(spawned.argv) == ["summarize this"]
    assert "--agent" not in spawned.argv


def test_a_fanout_naming_neither_a_model_nor_an_agent_reaches_the_spawn(spawned):
    answer = call(
        ops=[spawn_op("fanout.submit", {"prompt": "summarize this", "no_mcp_config": True})]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    assert _positionals(spawned.argv) == ["summarize this"]
    assert "--agent" not in spawned.argv


def test_a_flow_naming_a_model_still_puts_it_ahead_of_the_prompt(spawned):
    """The form that already worked has to keep working: admitting the bare one
    by collapsing the two positionals into one would be worse than the refusal
    it replaces."""
    answer = call(
        ops=[
            spawn_op(
                "flow.submit",
                {
                    "query": ["claude_code/claude-opus-5"],
                    "prompt": "summarize this",
                    "no_mcp_config": True,
                },
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert _positionals(spawned.argv) == ["claude_code/claude-opus-5", "summarize this"]


# ── the agent command ────────────────────────────────────────────────────────


def test_an_agent_given_one_positional_and_no_model_is_refused(spawned):
    """The single positional is the prompt, so this submission names no model
    at all. It used to be admitted and then rejected by the command itself,
    which is the stranded run this check exists to prevent."""
    answer = call(ops=[spawn_op("agent.submit", {"query": ["do it"], "no_mcp_config": True})])[
        "ops"
    ][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert spawned.argv is None, "the submission reached the spawn"
    message = answer["error"]["message"]
    # The correction has to be writable from the message, and has to name only
    # arguments this command takes.
    assert "a lone positional is read as the prompt, not as a model" in message, message
    assert "'prompt'" in message, message
    assert "name a profile with 'agent'" in message, message


def test_an_agent_given_a_model_and_a_prompt_reaches_the_spawn(spawned):
    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"query": ["claude_code/claude-opus-5"], "prompt": "do it", "no_mcp_config": True},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    # The model is the whole positional bucket; the prompt travels in a file,
    # which is why one positional means something different here than it does
    # to flow and fanout.
    assert _positionals(spawned.argv) == ["claude_code/claude-opus-5"]
    assert "--prompt-file" in spawned.argv


def test_an_agent_given_a_model_and_a_prompt_as_two_positionals_reaches_the_spawn(spawned):
    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"query": ["claude_code/claude-opus-5", "do it"], "no_mcp_config": True},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert _positionals(spawned.argv) == ["claude_code/claude-opus-5", "do it"]
