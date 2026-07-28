# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o flow` / `li o fanout` given neither a model nor an agent.

Setup already reads that as a request to orchestrate and answers it with the
default orchestrator profile, so the command has no missing argument to report.
It reported one anyway, ahead of setup, and returned 1 — the run the caller
asked for was refused by the layer that had nothing to decide.

The prompt is a different matter: nothing downstream can supply one, so a
submission without it is still refused, with the message it always used.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import lionagi.cli.orchestrate as orch_mod
from lionagi.cli.main import main


def _run_with_flow_mock(argv: list[str]):
    with patch.object(
        orch_mod, "_run_flow", AsyncMock(return_value=("flow output", "completed"))
    ) as run_flow:
        code = main(argv)
    return code, run_flow


def _run_with_fanout_mock(argv: list[str]):
    with patch.object(
        orch_mod, "_run_fanout", AsyncMock(return_value=("fanout output", "completed"))
    ) as run_fanout:
        code = main(argv)
    return code, run_fanout


class TestFlow:
    def test_naming_neither_carries_the_prompt_into_the_flow(self):
        code, run_flow = _run_with_flow_mock(["o", "flow", "summarize this"])
        assert code == 0
        kwargs = run_flow.call_args.kwargs
        assert kwargs["model_spec"] == ""
        assert kwargs["agent_name"] is None
        assert kwargs["prompt"] == "summarize this"

    def test_two_positionals_still_separate_the_model_from_the_prompt(self):
        """The regression that would matter: admitting the bare form by
        misreading the pair is worse than refusing the bare form."""
        code, run_flow = _run_with_flow_mock(
            ["o", "flow", "claude_code/claude-opus-5", "summarize this"]
        )
        assert code == 0
        kwargs = run_flow.call_args.kwargs
        assert kwargs["model_spec"] == "claude_code/claude-opus-5"
        assert kwargs["prompt"] == "summarize this"

    def test_a_named_agent_still_arrives_with_the_prompt(self):
        code, run_flow = _run_with_flow_mock(["o", "flow", "--agent", "researcher", "do it"])
        assert code == 0
        kwargs = run_flow.call_args.kwargs
        assert kwargs["agent_name"] == "researcher"
        assert kwargs["prompt"] == "do it"

    def test_no_prompt_is_still_refused(self, capsys):
        code, run_flow = _run_with_flow_mock(["o", "flow"])
        assert code == 1
        assert "prompt is required" in capsys.readouterr().err
        run_flow.assert_not_called()


class TestFanout:
    def test_naming_neither_carries_the_prompt_into_the_fanout(self):
        code, run_fanout = _run_with_fanout_mock(["o", "fanout", "summarize this"])
        assert code == 0
        kwargs = run_fanout.call_args.kwargs
        assert kwargs["model_spec"] == ""
        assert kwargs["agent_name"] is None
        assert kwargs["prompt"] == "summarize this"

    def test_two_positionals_still_separate_the_model_from_the_prompt(self):
        code, run_fanout = _run_with_fanout_mock(
            ["o", "fanout", "claude_code/claude-opus-5", "summarize this"]
        )
        assert code == 0
        kwargs = run_fanout.call_args.kwargs
        assert kwargs["model_spec"] == "claude_code/claude-opus-5"
        assert kwargs["prompt"] == "summarize this"

    def test_no_prompt_is_still_refused(self, capsys):
        code, run_fanout = _run_with_fanout_mock(["o", "fanout"])
        assert code == 1
        assert "prompt is required" in capsys.readouterr().err
        run_fanout.assert_not_called()
