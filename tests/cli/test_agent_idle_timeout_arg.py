# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``li agent --idle-timeout``: what the parser accepts, and what it refuses.

The value reaches the worker watchdog that stops a run after a stretch of
silence, so its bound is part of the command rather than left to the run.
"""

from __future__ import annotations

import argparse

import pytest

from lionagi.cli._argtypes import BoundedInt
from lionhub.serve.mcp.projection import build_parser_for, project_parser


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = build_parser_for("agent")
    return parser.parse_args(argv)


def test_the_flag_parses_a_whole_number_of_seconds():
    assert _parse(["m/p", "hi", "--idle-timeout", "90"]).idle_timeout == 90


def test_the_flag_is_absent_by_default_and_the_setting_decides():
    assert _parse(["m/p", "hi"]).idle_timeout is None


def _refusal(capsys, raw: str) -> str:
    """What the parser writes when it refuses *raw* for the flag."""
    with pytest.raises(SystemExit) as exc:
        _parse(["m/p", "hi", "--idle-timeout", raw])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "argument --idle-timeout" in err, err
    return err


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("0", "must be from 1 to"),
        ("-1", "must be from 1 to"),
        ("not-a-number", "must be a whole number"),
        ("1.5", "must be a whole number"),
    ],
)
def test_the_parser_refuses_what_cannot_name_a_positive_whole_second(capsys, raw, reason):
    assert reason in _refusal(capsys, raw)


def test_the_parser_refuses_a_value_past_the_greatest_whole_second(capsys):
    assert f"must be from 1 to {2**53 - 1}" in _refusal(capsys, str(2**53))


def test_the_bounded_type_states_its_range():
    kind = BoundedInt(1, 60)
    assert kind.minimum == 1 and kind.maximum == 60
    assert kind("60") == 60
    with pytest.raises(argparse.ArgumentTypeError, match="from 1 to 60"):
        kind("61")
    with pytest.raises(argparse.ArgumentTypeError, match="whole number"):
        kind("x")


def test_the_projector_carries_the_bound_into_the_schema():
    """What the parser refuses, the advertised schema has to say, or a caller
    learns the bound only from a rejected call."""
    schema = project_parser(build_parser_for("agent"), path="agent")
    idle = schema["properties"]["idle_timeout"]
    assert idle["type"] == "integer"
    assert idle["minimum"] == 1
    assert idle["maximum"] == 2**53 - 1
    assert idle["x-flag"] == "--idle-timeout"
