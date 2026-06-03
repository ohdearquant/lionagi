# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for issue #1118: SIGINT cancellation path must record CANCELLED_SIGINT.

The live SIGINT handler sets terminal status to "aborted"; _resolve_run_reason
must map that to RunReasons.CANCELLED_SIGINT (not ABORTED_USER).
"""

from __future__ import annotations

import pytest


def test_resolve_run_reason_aborted_returns_cancelled_sigint():
    """Status "aborted" (KeyboardInterrupt / SIGINT) → CANCELLED_SIGINT."""
    from lionagi.cli.agent import _resolve_run_reason
    from lionagi.state.reasons import RunReasons

    code, summary, evidence = _resolve_run_reason(status="aborted", exception=None)
    assert code == RunReasons.CANCELLED_SIGINT, (
        f"SIGINT path must record CANCELLED_SIGINT, got {code!r}"
    )
    assert evidence is None


def test_resolve_run_reason_aborted_not_aborted_user():
    """ABORTED_USER must NOT be written for SIGINT — it is reserved for other user aborts."""
    from lionagi.cli.agent import _resolve_run_reason
    from lionagi.state.reasons import RunReasons

    code, _, _ = _resolve_run_reason(status="aborted", exception=None)
    assert code != RunReasons.ABORTED_USER, (
        "ABORTED_USER must not be emitted for the SIGINT (KeyboardInterrupt) path"
    )


def test_resolve_run_reason_cancelled_sigint_is_defined():
    """Sanity: CANCELLED_SIGINT must be defined in RunReasons."""
    from lionagi.state.reasons import RunReasons

    assert hasattr(RunReasons, "CANCELLED_SIGINT")
    assert RunReasons.CANCELLED_SIGINT == "run.cancelled.sigint"


def test_resolve_run_reason_other_statuses_unaffected():
    """Non-aborted statuses must still resolve to their existing reason codes."""
    from lionagi.cli.agent import _resolve_run_reason
    from lionagi.state.reasons import RunReasons

    code, _, _ = _resolve_run_reason(status="completed", exception=None)
    assert code == RunReasons.COMPLETED_OK

    code, _, _ = _resolve_run_reason(status="timed_out", exception=None)
    assert code == RunReasons.TIMED_OUT_DEADLINE

    code, _, _ = _resolve_run_reason(status="cancelled", exception=None)
    assert code == RunReasons.CANCELLED_SYSTEM
