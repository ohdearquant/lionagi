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

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import lionagi.ln.concurrency.utils as cu
from lionagi.cli._runs import resolve_run_reason
from lionagi.ln.concurrency.utils import SigtermInterrupt
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


def test_sigterm_latch_consumed_once_plain_later_cancel_stays_system():
    # A latched SIGTERM must label exactly one resolution, not every
    # subsequent cancel in the same process — otherwise an unrelated later
    # run/test inherits an earlier run's external-SIGTERM verdict.
    cu._SIGTERM_RECEIVED.set()
    first_code, _, _ = resolve_run_reason(status="cancelled", exception=None)
    assert first_code == RunReasons.CANCELLED_SIGTERM

    second_code, second_summary, _ = resolve_run_reason(status="cancelled", exception=None)
    assert second_code == RunReasons.CANCELLED_SYSTEM
    assert "sigterm" not in second_summary.lower()


def test_sigterm_interrupt_path_still_consumes_latch_so_next_run_stays_system():
    # When the explicit SigtermInterrupt reaches teardown while the handler has
    # also set the latch, the interrupt classifies this run — but the latch must
    # still be consumed, or the next unrelated cancel inherits a stale SIGTERM.
    cu._SIGTERM_RECEIVED.set()
    first_code, _, _ = resolve_run_reason(
        status="cancelled",
        exception=SigtermInterrupt("process received SIGTERM"),
    )
    assert first_code == RunReasons.CANCELLED_SIGTERM

    second_code, second_summary, _ = resolve_run_reason(status="cancelled", exception=None)
    assert second_code == RunReasons.CANCELLED_SYSTEM
    assert "sigterm" not in second_summary.lower()


def test_cancelled_without_sigterm_stays_cancelled_system():
    code, summary, _ = resolve_run_reason(status="cancelled", exception=None)
    assert code == RunReasons.CANCELLED_SYSTEM
    assert "sigterm" not in summary.lower()


def test_reason_code_shape():
    assert RunReasons.CANCELLED_SIGTERM == "run.cancelled.sigterm"


# In-process signal delivery is racy under pytest-xdist (worker packing
# changes what shares the process), so the end-to-end check runs in a
# subprocess, mirroring tests/libs/concurrency/test_sigterm_teardown.py:
# an external SIGTERM cancels the coroutine, and the teardown-time view
# (flag latched, resolve_run_reason verdict) is written to a sentinel file.
_SUBPROCESS_SCRIPT = textwrap.dedent("""\
    import sys
    import anyio as _anyio
    from lionagi.cli._runs import resolve_run_reason
    from lionagi.ln.concurrency.utils import SigtermInterrupt, run_async, sigterm_received

    SENTINEL = sys.argv[1]
    READY    = sys.argv[2]

    async def long_running():
        with open(READY, "w") as f:
            f.write("ready")
        try:
            await _anyio.sleep(30)
        finally:
            # The persist teardown's view: the exception in flight is a plain
            # cancellation, but the handler has already latched the flag, so
            # the resolved reason must be the external-SIGTERM one. The latch
            # is consumed by resolve_run_reason, so it must read back False
            # afterward (a later run/test must not inherit this SIGTERM).
            with _anyio.CancelScope(shield=True):
                flag_before = sigterm_received()
                code, summary, _ = resolve_run_reason(status="cancelled", exception=None)
                flag_after = sigterm_received()
                with open(SENTINEL, "w") as f:
                    f.write(f"{flag_before}|{flag_after}|{code}|{summary}")

    try:
        run_async(long_running())
        sys.exit(0)
    except SigtermInterrupt:
        sys.exit(143)
""")


def test_external_sigterm_resolves_sigterm_reason_at_teardown(tmp_path):
    sentinel = tmp_path / "sentinel"
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(sentinel), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("subprocess never reached the event loop")
            time.sleep(0.02)
        time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode == 143
    flag_before, flag_after, code, summary = sentinel.read_text().split("|", 3)
    assert flag_before == "True"
    assert flag_after == "False"
    assert code == "run.cancelled.sigterm"
    assert "sigterm_external" in summary
