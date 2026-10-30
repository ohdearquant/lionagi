# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Malformed legacy verdicts must not become permission to execute."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from lionagi.protocols.action.gate import GateResult, adapt_legacy_hook, run_gate_pass

_SECRET = "sensitive-return-content-7195"


class _UnreadableField:
    def __repr__(self):
        return _SECRET

    def __str__(self):
        return _SECRET


def _verdict(**changes):
    result = GateResult(True, "policy", "bash", "run", "allowed")
    return replace(result, **changes)


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
