# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Malformed legacy verdicts must not become permission to execute."""

from __future__ import annotations

import logging
from dataclasses import fields, replace

import pytest

from lionagi.protocols.action.gate import GateResult, adapt_legacy_hook, run_gate_pass
from tests._gate_results import built_around_construction

_SECRET = "sensitive-return-content-7195"


@pytest.mark.parametrize("field", ["allowed", "errored"])
def test_gate_result_requires_boolean_verdict_fields(field):
    with pytest.raises(TypeError, match=f"{field} must be a bool"):
        GateResult(
            **{
                "allowed": 1 if field == "allowed" else True,
                "errored": 1 if field == "errored" else False,
                "control": "policy",
                "tool_name": "bash",
                "action": "run",
                "reason": "invalid",
            }
        )


def test_gate_result_cannot_allow_an_errored_verdict():
    with pytest.raises(ValueError, match="cannot allow"):
        GateResult(True, "policy", "bash", "run", "invalid", errored=True)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"mutated_args": ["a"]}, "mutated_args must be a dict or None, not list"),
        ({"control": None}, "control must be a str, not NoneType"),
        ({"tool_name": 3}, "tool_name must be a str, not int"),
        ({"action": b"run"}, "action must be a str, not bytes"),
        ({"reason": None}, "reason must be a str, not NoneType"),
    ],
)
def test_gate_result_construction_enforces_every_field_rule(changes, message):
    fields = {"allowed": False, "control": "policy", "tool_name": "bash", "action": "run"}
    with pytest.raises(TypeError, match=message):
        GateResult(**{**fields, "reason": "denied", **changes})


class _ClaimsValid(GateResult):
    __slots__ = ()

    def _broken_rule(self):
        return None


@pytest.mark.asyncio
async def test_a_subclass_cannot_waive_the_rules_for_the_adapter():
    base = GateResult(True, "policy", "bash", "run", "allowed")
    claims_valid = object.__new__(_ClaimsValid)
    for field in fields(GateResult):
        object.__setattr__(claims_valid, field.name, getattr(base, field.name))
    object.__setattr__(claims_valid, "allowed", 1)

    async def hook(_tool_name, _action, _args):
        return claims_valid

    denied = await adapt_legacy_hook("policy", hook)("bash", "run", {})

    assert denied.allowed is False and denied.errored is True
    assert denied.reason == "invalid GateResult: allowed must be a bool, not int"


class _UnreadableField:
    def __repr__(self):
        return _SECRET

    def __str__(self):
        return _SECRET


def _verdict(**changes):
    """An allowing verdict with *changes*, built by one of two paths: through the constructor
    when the changes keep it valid, and around it (``built_around_construction``) when they
    break a rule the constructor enforces, as a malformed third-party instance does."""
    result = GateResult(True, "policy", "bash", "run", "allowed")
    try:
        return replace(result, **changes)
    except (TypeError, ValueError):
        return built_around_construction(result, **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        _verdict(allowed=1),
        _verdict(allowed=0),
        _verdict(allowed=_UnreadableField()),
        _verdict(errored=0),
        _verdict(errored=_UnreadableField()),
        _verdict(allowed=True, errored=True),
        _verdict(mutated_args=[_SECRET]),
        _verdict(control=_UnreadableField()),
        _verdict(tool_name=_UnreadableField()),
        _verdict(action=_UnreadableField()),
        _verdict(reason=_UnreadableField()),
        _verdict(reason=None),
        _verdict(allowed=1, mutated_args={"secret": _SECRET}),
    ],
    ids=[
        "allowed-int-one",
        "allowed-int-zero",
        "allowed-unreadable",
        "errored-int-zero",
        "errored-unreadable",
        "allow-and-error",
        "rewrite-not-dict",
        "control-unreadable",
        "tool-name-unreadable",
        "action-unreadable",
        "reason-unreadable",
        "reason-null",
        "invalid-with-sensitive-rewrite",
    ],
)
async def test_invalid_gate_result_is_an_errored_denial_without_disclosing_content(invalid, caplog):
    args = {"command": "echo ok"}
    later_calls = []

    async def hook(_tool_name, _action, _args):
        return invalid

    async def later(_tool_name, _action, _args):
        later_calls.append(True)
        return GateResult(True, "later", "bash", "run", "allowed")

    with caplog.at_level(logging.DEBUG, logger="lionagi.agent.gate"):
        final_args, denied = await run_gate_pass(
            [adapt_legacy_hook("policy", hook), later], "bash", "run", args
        )

    assert final_args == args
    assert denied is not None
    assert denied.allowed is False
    assert denied.errored is True
    assert denied.mutated_args is None
    assert _SECRET not in denied.reason
    assert _SECRET not in caplog.text
    assert later_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_result", "expected_args"),
    [
        (None, {"command": "original"}),
        ({}, {}),
        ({"command": "rewritten"}, {"command": "rewritten"}),
    ],
    ids=["none", "empty-rewrite", "rewrite"],
)
async def test_valid_legacy_results_preserve_allow_and_rewrite(legacy_result, expected_args):
    evaluate = adapt_legacy_hook("policy", lambda *_args: legacy_result)
    args, denied = await run_gate_pass([evaluate], "bash", "run", {"command": "original"})
    assert denied is None
    assert args == expected_args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        GateResult(True, "policy", "bash", "run", "allowed"),
        GateResult(True, "policy", "bash", "run", "rewritten", mutated_args={"command": "new"}),
        GateResult(False, "policy", "bash", "run", "denied"),
        GateResult(False, "policy", "bash", "run", "unavailable", errored=True),
    ],
    ids=["allow", "allow-rewrite", "deny", "fault"],
)
async def test_valid_gate_results_keep_their_verdict(verdict):
    result = await adapt_legacy_hook("policy", lambda *_args: verdict)(
        "bash", "run", {"command": "original"}
    )
    assert result == verdict
