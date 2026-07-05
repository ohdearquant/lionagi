# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""External SIGTERM must produce a distinct reason in the run record.

An externally delivered SIGTERM cancels the inner task, so the exception the
teardown path sees is a plain CancelledError — indistinguishable from an
internal runtime cancel. run_async's SIGTERM handler latches a process-wide
flag; resolve_run_reason consults it (or an explicit SigtermInterrupt) and
stamps run.cancelled.sigterm / "sigterm_external" instead of the generic
runtime-cancel summary.
"""

from __future__ import annotations

import signal
import threading

import pytest

import lionagi.ln.concurrency.utils as cu
from lionagi.cli._runs import resolve_run_reason
from lionagi.ln.concurrency.utils import SigtermInterrupt, run_async, sigterm_received
from lionagi.state.reasons import RunReasons


@pytest.fixture(autouse=True)
def _fresh_sigterm_latch(monkeypatch):
    """Isolate the process-wide latch so tests neither see nor leak state."""
    monkeypatch.setattr(cu, "_SIGTERM_RECEIVED", threading.Event())


def test_cancelled_with_sigterm_interrupt_maps_to_cancelled_sigterm():
    code, summary, evidence = resolve_run_reason(
        status="cancelled",
        exception=SigtermInterrupt("process received SIGTERM"),
    )
    assert code == RunReasons.CANCELLED_SIGTERM
    assert "sigterm_external" in summary
    assert evidence is None


def test_cancelled_with_plain_cancel_but_latched_flag_maps_to_cancelled_sigterm():
    # The realistic path: the handler latched the flag, but the exception that
    # reaches teardown is a plain cancellation (or None), not SigtermInterrupt.
    cu._SIGTERM_RECEIVED.set()
    code, summary, _ = resolve_run_reason(status="cancelled", exception=None)
    assert code == RunReasons.CANCELLED_SIGTERM
    assert "sigterm_external" in summary


def test_cancelled_without_sigterm_stays_cancelled_system():
    code, summary, _ = resolve_run_reason(status="cancelled", exception=None)
    assert code == RunReasons.CANCELLED_SYSTEM
    assert "sigterm" not in summary.lower()


def test_reason_code_shape():
    assert RunReasons.CANCELLED_SIGTERM == "run.cancelled.sigterm"


def test_run_async_sigterm_handler_latches_flag():
    """Delivering SIGTERM mid-run latches the flag before teardown code runs."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers require the main thread")

    import anyio

    observed_at_teardown: list[bool] = []

    async def long_running():
        try:
            signal.raise_signal(signal.SIGTERM)
            await anyio.sleep(30)
        finally:
            # This mimics the persist teardown: at this point the exception in
            # flight is a plain cancellation, but the flag is already latched.
            observed_at_teardown.append(sigterm_received())

    with pytest.raises(SigtermInterrupt):
        run_async(long_running())

    assert observed_at_teardown == [True]
    assert sigterm_received()
