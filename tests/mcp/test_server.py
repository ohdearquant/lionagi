# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the MCP tool surface.

``jobs.submit`` is stubbed so nothing spawns; these assert on the flags the tool
builds and on the input it refuses before a run is ever recorded.
"""

from __future__ import annotations

import pytest

# The tool surface is defined with fastmcp, which lives in the ``mcp`` extra; the
# rest of tests/mcp only touches the extra-free job plumbing. CI syncs all extras,
# so these run there — a skip here means a bare local install, not a green check.
pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import server  # noqa: E402 — must follow the extra guard


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


def test_prompt_file_is_passed_through_as_a_cli_flag(captured, tmp_path):
    pf = tmp_path / "prompt.md"
    pf.write_text("a long instruction with 'quotes' and `code`\n")

    server.submit_agent(prompt_file=str(pf), agent="reviewer")

    assert captured["flags"][:2] == ["--prompt-file", str(pf)]
    # The text is not read here and not inlined: the path is handed to the CLI.
    assert captured["prompt"] is None


def test_prompt_file_accepts_a_tilde_path(captured, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    pf = tmp_path / "prompt.md"
    pf.write_text("body")

    server.submit_agent(prompt_file="~/prompt.md")

    assert captured["flags"] == ["--prompt-file", str(pf)]


def test_prompt_and_prompt_file_together_are_refused(captured, tmp_path):
    pf = tmp_path / "prompt.md"
    pf.write_text("body")

    with pytest.raises(ValueError, match="not both"):
        server.submit_agent(prompt="inline", prompt_file=str(pf))

    assert captured == {}  # refused before anything was submitted


def test_missing_prompt_file_is_refused_before_submitting(captured, tmp_path):
    # Without this the path reaches the CLI, the job record is written, a process
    # is spawned, and the caller learns about the typo only by reading a console
    # log of a failed run.
    with pytest.raises(ValueError, match="does not exist"):
        server.submit_agent(prompt_file=str(tmp_path / "nope.md"))

    assert captured == {}


def test_empty_prompt_file_is_refused(captured, tmp_path):
    # The CLI itself errors on an empty prompt file, so catching it here turns a
    # failed run into a rejected call.
    pf = tmp_path / "empty.md"
    pf.write_text("")

    with pytest.raises(ValueError, match="is empty"):
        server.submit_agent(prompt_file=str(pf))

    assert captured == {}


def test_relative_prompt_file_is_refused(captured):
    # The run's cwd is the caller's `cwd` argument, not this server's, so a
    # relative path would resolve against a directory the caller never chose.
    with pytest.raises(ValueError, match="absolute path"):
        server.submit_agent(prompt_file="prompt.md", cwd="/tmp")

    assert captured == {}


def test_stdin_prompt_file_is_refused(captured):
    # The CLI accepts "-" for stdin, but a background run is spawned with stdin at
    # DEVNULL, so "-" would yield an empty prompt and a failed run.
    with pytest.raises(ValueError, match="no stdin"):
        server.submit_agent(prompt_file="-")

    assert captured == {}


def test_a_directory_is_refused_rather_than_passed_on(captured, tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        server.submit_agent(prompt_file=str(tmp_path))

    assert captured == {}


def test_plain_prompt_still_goes_through_untouched(captured):
    server.submit_agent(prompt="hello")

    assert captured["prompt"] == "hello"
    assert "--prompt-file" not in captured["flags"]
