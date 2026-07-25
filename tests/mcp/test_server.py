# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the MCP tool surface.

``jobs.submit`` is stubbed so nothing spawns; these assert on what the tools hand
it and on the input they refuse before a run is ever recorded.
"""

from __future__ import annotations

import pytest

# The tool surface is defined with fastmcp, which lives in the ``mcp`` extra; the
# rest of tests/mcp only touches the extra-free job plumbing. CI syncs all extras,
# so these run there — a skip here means a bare local install, not a green check.
pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import server  # noqa: E402 — must follow the extra guard

# Every submit tool takes the instruction the same way, so the path handling is
# checked against all three rather than only the one it was added for. The kind is
# carried alongside so each case can assert it reached the right run kind: without
# that, a regression routing flow or fanout through "agent" would still pass.
SUBMITTERS = pytest.mark.parametrize(
    ("submit", "kind"),
    [
        (server.submit_agent, "agent"),
        (server.submit_flow, "flow"),
        (server.submit_fanout, "fanout"),
    ],
    ids=["agent", "flow", "fanout"],
)


@pytest.fixture
def captured(monkeypatch):
    """Replace jobs.submit with a recorder, so a call that reaches it is visible."""
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen["kind"] = kind
        seen["flags"] = list(flags)
        seen.update(kwargs)
        return {"run_id": "x", "pid": 1, "status": "running"}

    monkeypatch.setattr(server.jobs, "submit", fake_submit)
    return seen


@SUBMITTERS
def test_prompt_file_contents_are_submitted_as_the_prompt(captured, tmp_path, submit, kind):
    body = "a long instruction with 'quotes' and `code`\n"
    pf = tmp_path / "prompt.md"
    pf.write_text(body)

    submit(prompt_file=str(pf))

    # The text is read here, so submit() snapshots it into the job directory. The
    # caller's path is deliberately not forwarded: see the snapshot test below.
    assert captured["kind"] == kind
    assert captured["prompt"] == body
    assert "--prompt-file" not in captured["flags"]
    assert str(pf) not in captured["flags"]


@SUBMITTERS
def test_prompt_file_accepts_a_tilde_path(captured, tmp_path, monkeypatch, submit, kind):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "prompt.md").write_text("body")

    submit(prompt_file="~/prompt.md")

    assert captured["kind"] == kind
    assert captured["prompt"] == "body"


@SUBMITTERS
def test_prompt_and_prompt_file_together_are_refused(captured, tmp_path, submit, kind):
    pf = tmp_path / "prompt.md"
    pf.write_text("body")

    with pytest.raises(ValueError, match="not both"):
        submit(prompt="inline", prompt_file=str(pf))

    assert captured == {}  # refused before anything was submitted


@SUBMITTERS
def test_missing_prompt_file_is_refused_before_submitting(captured, tmp_path, submit, kind):
    # Without this the path reaches the CLI, the job record is written, a process
    # is spawned, and the caller learns about the typo only by reading a console
    # log of a failed run.
    with pytest.raises(ValueError, match="could not read"):
        submit(prompt_file=str(tmp_path / "nope.md"))

    assert captured == {}


@SUBMITTERS
def test_a_directory_is_refused_rather_than_passed_on(captured, tmp_path, submit, kind):
    with pytest.raises(ValueError, match="could not read"):
        submit(prompt_file=str(tmp_path))

    assert captured == {}


@SUBMITTERS
@pytest.mark.parametrize("body", ["", "   \n\t\n"], ids=["empty", "whitespace"])
def test_a_prompt_file_with_no_instruction_in_it_is_refused(captured, tmp_path, submit, kind, body):
    # An agent handed a blank instruction burns a real run to produce nothing.
    pf = tmp_path / "empty.md"
    pf.write_text(body)

    with pytest.raises(ValueError, match="is empty"):
        submit(prompt_file=str(pf))

    assert captured == {}


@SUBMITTERS
def test_relative_prompt_file_is_refused(captured, submit, kind):
    # The file is read in the server process, so a relative path would resolve
    # against the server's working directory rather than the run's cwd.
    with pytest.raises(ValueError, match="absolute path"):
        submit(prompt_file="prompt.md", cwd="/project")

    assert captured == {}


@SUBMITTERS
def test_stdin_prompt_file_is_refused(captured, submit, kind):
    # The CLI accepts "-" for stdin, but a background run is spawned with stdin at
    # DEVNULL, so "-" would yield an empty prompt and a failed run.
    with pytest.raises(ValueError, match="no stdin"):
        submit(prompt_file="-")

    assert captured == {}


@SUBMITTERS
def test_plain_prompt_still_goes_through_untouched(captured, submit, kind):
    submit(prompt="hello")

    assert captured["kind"] == kind
    assert captured["prompt"] == "hello"
    assert "--prompt-file" not in captured["flags"]


@SUBMITTERS
def test_neither_prompt_nor_prompt_file_leaves_the_prompt_unset(captured, submit, kind):
    # Both are optional: a resumed or playbook-driven run supplies its own text.
    submit(agent="reviewer")

    assert captured["kind"] == kind
    assert captured["prompt"] is None


def test_the_text_is_snapshotted_at_submit_time(captured, tmp_path):
    # An editable prompt file must not be able to change what an already-submitted
    # run executes. Reading at submit time is what guarantees that; forwarding the
    # path would leave the text live until the CLI opened it.
    pf = tmp_path / "prompt.md"
    pf.write_text("original")

    server.submit_agent(prompt_file=str(pf))
    pf.write_text("edited after submitting")

    assert captured["prompt"] == "original"
