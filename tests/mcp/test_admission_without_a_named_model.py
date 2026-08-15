# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What the pre-spawn model check admits, per command.

The check exists to refuse a submission the command would reject in its first
second: such a run never reaches the hook that records an end, so the job
stays non-terminal and a caller waiting on it waits forever. Its answer has to
track what each command actually does with the arguments, not a general idea
of what a model source looks like. Two commands disagreed with a naive check:
flow/fanout read "no model and no agent" as a request to orchestrate (and use
the default orchestrator profile), so refusing that combination refused a run
that would have started; and `agent` reads its positionals as
``[MODEL] PROMPT``, so treating any positional as a model wrongly admitted
``query=["do it"]`` with no model anywhere -- exactly the run that dies on
start.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from lionagi.mcp import dispatch, jobs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict, playbook: str | None = None) -> dict:
    """A spawn op carrying the fingerprint its verb requires.

    Fetched the way a caller has to fetch it, so these tests exercise the
    round-trip rather than reaching past it. A playbook varies the schema, so a
    submission naming one has to fetch its help the same way.
    """
    target: Any = {"verb": op, "playbook": playbook} if playbook is not None else op
    return {"op": op, "args": args, "schema_fingerprint": call(help=target)["schema_fingerprint"]}


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


# ── the prompt the orchestrating default does not supply ─────────────────────


@pytest.fixture
def submit_dir(monkeypatch, tmp_path):
    """A submitting directory with no playbooks or spec files above it.

    A playbook name is resolved from the current directory upwards, so an
    ancestor holding one would otherwise decide what these tests admit.
    """
    d = tmp_path / "submit"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
def test_an_orchestrating_submission_with_no_prompt_never_reaches_the_spawn(spawned, verb):
    """Naming neither a model nor an agent is a request to orchestrate, and the
    command answers it with the default orchestrator profile. It has no such
    answer for a missing prompt: it refuses before either runner is entered,
    which strands a run the caller already holds a handle to."""
    answer = call(ops=[spawn_op(verb, {"no_mcp_config": True})])["ops"][0]

    assert spawned.argv is None, "the submission reached the spawn"
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    message = answer["error"]["message"]
    assert "has no prompt and nothing to supply one" in message, message
    # The correction has to be writable from the message, in arguments this
    # command takes.
    assert "'prompt'" in message, message
    assert "'query'" in message, message


def test_a_fanout_is_not_offered_the_spec_file_it_has_no_argument_for(spawned):
    """Fanout takes no spec file and no playbook, so naming either as a way to
    supply a prompt would send the caller into a second refusal, this time from
    argument validation."""
    answer = call(ops=[spawn_op("fanout.submit", {"no_mcp_config": True})])["ops"][0]

    message = answer["error"]["message"]
    assert "'file'" not in message, message
    assert "'playbook'" not in message, message


def test_a_flow_whose_prompt_is_only_in_a_spec_file_reaches_the_spawn(spawned, submit_dir):
    """The spec file may carry a prompt: key, and this check does not read the
    file. The command does, so the question is left to it."""
    spec = submit_dir / "flow.yaml"
    spec.write_text("prompt: summarize this\n")

    answer = call(ops=[spawn_op("flow.submit", {"file": str(spec), "no_mcp_config": True})])["ops"][
        0
    ]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    assert any(str(spec) in token for token in spawned.argv), spawned.argv


def test_a_play_naming_only_a_playbook_reaches_the_spawn(spawned, submit_dir):
    """A playbook is a flow whose prompt is already written down, so naming one
    is a complete submission."""
    books = submit_dir / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "probe.playbook.yaml").write_text("name: probe\nprompt: summarize this\n")

    answer = call(
        ops=[spawn_op("play.submit", {"playbook": "probe", "no_mcp_config": True}, "probe")]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    assert "--playbook=probe" in spawned.argv, spawned.argv


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
def test_an_orchestrating_submission_whose_prompt_is_empty_never_reaches_the_spawn(spawned, verb):
    """The command refuses a falsy prompt, not an absent one. An empty string is
    present and not true, so admitting it here strands the same run a missing
    prompt would."""
    answer = call(ops=[spawn_op(verb, {"prompt": "", "no_mcp_config": True})])["ops"][0]

    assert spawned.argv is None, "the submission reached the spawn"
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "has no prompt and nothing to supply one" in answer["error"]["message"], answer


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
@pytest.mark.parametrize("query", ([""], ["claude_code/claude-opus-5", ""]))
def test_an_orchestrating_submission_whose_last_positional_is_empty_never_reaches_the_spawn(
    spawned, verb, query
):
    """The command reads the last positional as the prompt: a lone one is it,
    and with two the model comes first. So a model ahead of an empty prompt is
    refused exactly as a lone empty one is."""
    answer = call(ops=[spawn_op(verb, {"query": query, "no_mcp_config": True})])["ops"][0]

    assert spawned.argv is None, "the submission reached the spawn"
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "has no prompt and nothing to supply one" in answer["error"]["message"], answer


def test_a_flow_with_an_empty_positional_and_a_spec_file_reaches_the_spawn(spawned, submit_dir):
    """The spec file may carry a prompt: key, and the command reads it after
    assigning the positionals. So an empty positional beside a file is still the
    command's question to answer, and refusing it here would refuse a run that
    would have started."""
    spec = submit_dir / "flow.yaml"
    spec.write_text("prompt: summarize this\n")

    answer = call(
        ops=[spawn_op("flow.submit", {"query": [""], "file": str(spec), "no_mcp_config": True})]
    )["ops"][0]

    assert answer["ok"] is True, answer
    assert spawned.argv is not None, "the submission was refused before the spawn"
    assert _positionals(spawned.argv) == [""]


def test_every_kind_the_prompt_check_refuses_has_a_correction_to_offer():
    """The refusal reads its remediation by kind, so a kind admitted into the
    check without one would raise past the caller instead of answering."""
    assert set(dispatch._PROMPT_SOURCES) == set(dispatch._ORCHESTRATING_KINDS)
