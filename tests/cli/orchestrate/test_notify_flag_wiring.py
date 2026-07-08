# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`--notify` CLI flag: parses on `li o flow` / `li play`, defaults to None, and
threads through to _run_flow / _resume_flow, overriding whatever settings resolve."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

from lionagi.cli.orchestrate import (
    add_orchestrate_subparser,
    inject_playbook_schema_into_parser,
    run_orchestrate,
)


def _parse_flow_args(argv: list[str]) -> argparse.Namespace:
    """Mimic the real CLI pipeline: pre-scan for playbook → inject flags → parse."""
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    orch_parsers = add_orchestrate_subparser(subparsers)
    full_argv = ["o", "flow", *argv]
    inject_playbook_schema_into_parser(orch_parsers["flow"], full_argv)
    return parser.parse_args(full_argv)


def test_notify_flag_parses_and_defaults_to_none():
    args = _parse_flow_args(["claude", "do the thing"])
    assert args.notify is None

    args = _parse_flow_args(["--notify", "curl -X POST {payload}", "claude", "do the thing"])
    assert args.notify == "curl -X POST {payload}"


def test_notify_flag_threads_into_run_flow_call(capsys):
    args = _parse_flow_args(["--notify", "my-hook {status}", "claude", "do the thing"])

    with patch(
        "lionagi.cli.orchestrate._run_flow",
        AsyncMock(return_value=("done", "completed")),
    ) as run_flow_mock:
        code = run_orchestrate(args)

    assert code == 0
    assert run_flow_mock.call_args.kwargs["notify"] == "my-hook {status}"


def test_notify_flag_defaults_to_none_when_absent():
    args = _parse_flow_args(["claude", "do the thing"])

    with patch(
        "lionagi.cli.orchestrate._run_flow",
        AsyncMock(return_value=("done", "completed")),
    ) as run_flow_mock:
        code = run_orchestrate(args)

    assert code == 0
    assert run_flow_mock.call_args.kwargs["notify"] is None


def test_notify_flag_threads_into_resume_flow_call():
    args = _parse_flow_args(["--resume", "abc123", "--notify", "resume-hook {invocation_id}"])

    with patch(
        "lionagi.cli.orchestrate._resume_flow",
        AsyncMock(return_value=("resumed output", "completed")),
    ) as resume_mock:
        code = run_orchestrate(args)

    assert code == 0
    assert resume_mock.call_args.kwargs["notify"] == "resume-hook {invocation_id}"
