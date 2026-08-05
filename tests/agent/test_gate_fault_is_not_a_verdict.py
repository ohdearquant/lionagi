# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A control that cannot answer must not be reported as having decided.

``GateResult.errored`` exists to separate an evaluator FAULT from a policy
VERDICT. Both refuse the call, which is correct, and an operator does entirely
different things about them: a denial means the policy is working, a fault
means the policy is not running.

The case that made this concrete: ``PermissionPolicy.to_pre_hook`` raised a
plain ``PermissionError`` both for a deny rule and for an escalate decision
with no handler configured. The second is a configuration mistake, and it
arrived at every consumer as a clean policy verdict, recoverable only by
reading the message text.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.agent.gate import (
    ControlUnavailableError,
    GateDeniedError,
    GateResult,
    adapt_legacy_hook,
    run_gate_pass,
)
from lionagi.agent.permissions import PermissionPolicy


def _policy(**kw) -> PermissionPolicy:
    return PermissionPolicy(mode="rules", **kw)


# --------------------------------------------------------------------------
# the escalate decision, which is where the two were conflated
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_without_a_handler_is_a_fault():
    hook = _policy(escalate={"bash": ["*"]}).to_pre_hook()

    with pytest.raises(ControlUnavailableError) as exc:
        await hook("bash", "run", {"command": "ls"})

    assert "No escalation handler configured" in str(exc.value)


@pytest.mark.asyncio
async def test_a_fault_is_still_a_refusal():
    """Subclassing PermissionError is what keeps every existing consumer
    failing closed; a caller that only knows the base class sees no change."""
    hook = _policy(escalate={"bash": ["*"]}).to_pre_hook()

    with pytest.raises(PermissionError):
        await hook("bash", "run", {"command": "ls"})


@pytest.mark.asyncio
async def test_escalate_declined_by_a_handler_is_a_verdict():
    """A handler that ran and said no is an answer, and reads as one."""

    async def decline(decision, args):
        return False

    hook = _policy(escalate={"bash": ["*"]}, on_escalate=decline).to_pre_hook()

    with pytest.raises(PermissionError) as exc:
        await hook("bash", "run", {"command": "ls"})

    assert not isinstance(exc.value, ControlUnavailableError)
    assert "declined" in str(exc.value)
    assert "No escalation handler configured" not in str(exc.value), (
        "a handler was configured and ran, so saying otherwise is false"
    )


@pytest.mark.asyncio
async def test_a_deny_rule_is_a_verdict():
    hook = _policy(deny={"bash": ["rm *"]}).to_pre_hook()

    with pytest.raises(PermissionError) as exc:
        await hook("bash", "run", {"command": "rm -rf /"})

    assert not isinstance(exc.value, ControlUnavailableError)


@pytest.mark.asyncio
async def test_an_allowed_call_is_untouched():
    hook = _policy(allow={"bash": ["ls*"]}).to_pre_hook()
    assert await hook("bash", "run", {"command": "ls -la"}) is None


# --------------------------------------------------------------------------
# what the gate layer does with each
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_marks_a_declared_fault_errored():
    hook = _policy(escalate={"bash": ["*"]}).to_pre_hook()
    evaluate = adapt_legacy_hook("permission_check", hook)

    result = await evaluate("bash", "run", {"command": "ls"})

    assert result.allowed is False
    assert result.errored is True


@pytest.mark.asyncio
async def test_the_gate_does_not_mark_a_denial_errored():
    hook = _policy(deny={"bash": ["*"]}).to_pre_hook()
    evaluate = adapt_legacy_hook("permission_check", hook)

    result = await evaluate("bash", "run", {"command": "ls"})

    assert result.allowed is False
    assert result.errored is False


@pytest.mark.asyncio
async def test_an_unexpected_exception_stays_errored():
    """The pre-existing fail-closed path keeps its behaviour and its wording."""

    async def broken(tool_name, action, args):
        raise RuntimeError("boom")

    result = await adapt_legacy_hook("broken", broken)("bash", "run", {})

    assert result.allowed is False
    assert result.errored is True
    assert "evaluator error" in result.reason


# --------------------------------------------------------------------------
# the flag has readers, which is the other half of the row
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pass_logs_a_fault_and_says_nothing_about_a_denial(caplog):
    async def faulty(tool_name, action, args):
        raise ControlUnavailableError("backend unreachable")

    async def denying(tool_name, action, args):
        raise PermissionError("denied by rule: *")

    with caplog.at_level(logging.ERROR, logger="lionagi.agent.gate"):
        await run_gate_pass([adapt_legacy_hook("denies", denying)], "bash", "run", {})
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="lionagi.agent.gate"):
        await run_gate_pass([adapt_legacy_hook("faulty", faulty)], "bash", "run", {})
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, errors
    assert "faulty" in errors[0]
    assert "backend unreachable" in errors[0]


def test_the_raised_error_says_which_of_the_two_it_was():
    denial = GateResult(False, "policy", "bash", "run", "denied by rule: rm *")
    fault = GateResult(False, "policy", "bash", "run", "backend unreachable", errored=True)

    assert str(GateDeniedError(denial)) == "policy: denied by rule: rm *"
    assert "could not evaluate" in str(GateDeniedError(fault))
    assert GateDeniedError(fault).result.errored is True


@pytest.mark.asyncio
async def test_a_fault_still_stops_the_pass():
    """Whatever it is called, the call does not proceed."""
    ran = []

    async def faulty(tool_name, action, args):
        raise ControlUnavailableError("no handler")

    async def later(tool_name, action, args):
        ran.append(tool_name)
        return None

    args, deny = await run_gate_pass(
        [adapt_legacy_hook("faulty", faulty), adapt_legacy_hook("later", later)],
        "bash",
        "run",
        {},
    )

    assert deny is not None and deny.allowed is False
    assert ran == [], "a control that could not answer must not let the pass continue"
