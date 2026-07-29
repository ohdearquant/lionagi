# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""What a failed terminal notice records, and what it must never record.

The delivery command's output can carry a credential it obtained anywhere, so
none of it is stored. Discarding it at the pipe kept that promise and cost the
record any way to tell one failure from another. These tests pin both halves:
the failure is classifiable, and the stored field stays a bounded enum.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lionagi.mcp._notify_hook import (
    _FAILURE_CLASSES,
    _FAILURE_UNKNOWN,
    _classify_failure,
    _classify_quietly,
    _deliver,
    _unverifiable_reason,
)

_NAMES = {name for name, _ in _FAILURE_CLASSES} | {_FAILURE_UNKNOWN}


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


# ---------------------------------------------------------------------------
# The invariant: the stored field is a bounded enum, not free text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        "sending to https://hooks.example.com/services/T00/B00/XXXXXXXXXXXX failed",
        "psql: password authentication failed for user 'admin' with password hunter2",
        "",
        "   \n\t  ",
        "\x00\x01 binary garbage \xff",
    ],
)
def test_no_input_can_make_the_stored_value_leave_the_closed_set(text):
    """Fail-closed is the whole design: an unmatched case yields a name, never a fragment.

    The moment an unrecognised failure is allowed to contribute its own words,
    the field is free text again and the no-secrets invariant is gone. These
    inputs are chosen to be exactly the ones a helpful implementation would want
    to quote.
    """
    result = _classify_failure(text)
    assert result in _NAMES
    # Nothing from the input survives into the stored value.
    for token in ("wJalrXUtn", "eyJhbGci", "hooks.example.com", "hunter2", "admin"):
        assert token not in result


def test_a_credential_bearing_line_that_also_matches_a_class_still_stores_only_the_name():
    """Matching a class must not tempt the classifier into returning context."""
    result = _classify_failure("401 unauthorized: token sk-live-AAAABBBBCCCCDDDD rejected")
    assert result == "authentication_failed"
    assert "sk-live" not in result


def test_classifier_failure_yields_unknown_and_carries_nothing_out(monkeypatch):
    """An exception mid-classification must not carry the text out with it.

    Python exceptions routinely embed the value that caused them, so the
    swallow is deliberate: the exception object is dropped rather than logged.
    """

    def _explode(text):
        raise RuntimeError(f"boom while reading {text}")

    monkeypatch.setattr("lionagi.mcp._notify_hook._classify_failure", _explode)
    out = _classify_quietly("password=hunter2")
    assert out == _FAILURE_UNKNOWN
    assert "hunter2" not in out


# ---------------------------------------------------------------------------
# The point of the change: a failure is now classifiable
# ---------------------------------------------------------------------------


def test_a_missing_notifier_is_distinguishable_from_a_refused_message():
    """The two failures that used to record identically."""
    missing = _deliver(_py("import sys; sys.stderr.write('command not found'); sys.exit(127)"), {})
    refused = _deliver(_py("import sys; sys.stderr.write('403 forbidden'); sys.exit(1)"), {})

    assert missing["ok"] is False and refused["ok"] is False
    assert missing["failure_class"] == "command_not_found"
    assert refused["failure_class"] == "refused_by_policy"
    assert missing["failure_class"] != refused["failure_class"]


def test_a_network_refusal_is_not_read_as_a_policy_refusal():
    """First match wins, so a broad needle silently reclassifies a narrower case.

    "connection refused" contains "refused". With the policy class matching the
    bare word and listed first, every connection failure recorded as a policy
    refusal — a wrong answer that looks exactly as confident as a right one.
    """
    assert _classify_failure("connection refused to vault.internal") == "connection_failed"
    assert _classify_failure("dial tcp: no route to host") == "connection_failed"
    assert _classify_failure("request refused by the delivery gate") == "refused_by_policy"


def test_a_delivery_that_says_nothing_useful_is_unknown_not_a_quote():
    out = _deliver(_py("import sys; sys.stderr.write('weird internal burble'); sys.exit(9)"), {})
    assert out["ok"] is False
    assert out["exit_code"] == 9
    assert out["failure_class"] == _FAILURE_UNKNOWN
    assert "burble" not in str(out)


def test_a_successful_delivery_records_no_failure_class():
    out = _deliver(_py("import sys; sys.exit(0)"), {})
    assert out["ok"] is True
    assert out["failure_class"] is None


def test_the_child_output_never_appears_anywhere_in_the_recorded_outcome():
    """The outcome dict is what gets persisted; it is the surface that matters."""
    out = _deliver(
        _py(
            "import sys; sys.stdout.write('token=sk-live-SECRETVALUE'); "
            "sys.stderr.write('connection refused to vault.internal'); sys.exit(1)"
        ),
        {},
    )
    blob = repr(out)
    assert out["failure_class"] == "connection_failed"
    for token in ("sk-live", "SECRETVALUE", "vault.internal"):
        assert token not in blob


def test_a_timeout_is_classified_without_touching_the_exceptions_captured_output():
    """subprocess.TimeoutExpired carries the child's output on the exception itself.

    Storing str(exc), or logging it, would put back exactly the free text this
    function exists to keep out.
    """
    out = _deliver(
        _py(
            "import sys,time; sys.stderr.write('password=hunter2'); sys.stderr.flush(); time.sleep(60)"
        ),
        {},
    )
    assert out["ok"] is False
    assert out["error"] == "TimeoutExpired"
    assert out["failure_class"] == "timeout"
    assert "hunter2" not in repr(out)


# ---------------------------------------------------------------------------
# Degraded trust: a zero exit that does not mean delivered
# ---------------------------------------------------------------------------


def test_kkernel_exec_without_strict_is_marked_unverified():
    """Exit code is the only evidence, and for this shape it is known to be weak.

    kkernel exec exits 0 when any op succeeded, so a multi-op notify whose send
    was refused still exits 0 and would otherwise record as delivered.
    """
    assert _unverifiable_reason(["kkernel", "exec", "comm.send(...)"]) is not None
    assert _unverifiable_reason(["/opt/homebrew/bin/kkernel", "exec", "x"]) is not None


def test_strict_clears_the_marker_and_unrelated_commands_are_never_marked():
    assert _unverifiable_reason(["kkernel", "exec", "--strict", "x"]) is None
    # Not this hook's business to opine on notifiers it knows nothing about.
    assert _unverifiable_reason(["curl", "-X", "POST", "https://example.com"]) is None
    assert _unverifiable_reason(["kkernel", "status"]) is None
    assert _unverifiable_reason([]) is None


def test_an_unverified_delivery_is_its_own_state_not_a_failure():
    """Neither 'delivered' nor 'failed' is honest here, so it is marked instead."""
    out = _deliver(["kkernel", "exec", "comm.send(...)"], {}, program="kkernel")
    if out["exit_code"] is None:
        pytest.skip("kkernel not installed on this machine")
    if out["exit_code"] == 0:
        assert out["ok"] is True, "a run that exited 0 is not reported as failed"
        assert out["delivery_verified"] is False
        assert out["unverified_reason"]


def test_a_verified_zero_exit_carries_no_unverified_marker():
    out = _deliver(_py("import sys; sys.exit(0)"), {})
    assert out["ok"] is True
    assert "delivery_verified" not in out
    assert "unverified_reason" not in out


def test_the_marker_is_not_applied_to_a_failed_delivery():
    """A non-zero exit is already a failure; unverified is about a zero exit."""
    out = _deliver(_py("import sys; sys.exit(1)"), {})
    assert out["ok"] is False
    assert "delivery_verified" not in out


def test_subprocess_is_invoked_without_devnull_so_there_is_something_to_classify():
    """Guards the mechanism: capture_output must stay on for any of this to work."""
    out = _deliver(_py("import sys; sys.stderr.write('permission denied'); sys.exit(13)"), {})
    assert out["failure_class"] == "permission_denied", (
        "classification silently degrades to unknown if the output is discarded again"
    )
    assert subprocess.DEVNULL is not None  # sanity: the constant still exists
