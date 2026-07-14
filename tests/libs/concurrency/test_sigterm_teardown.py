# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for SIGTERM-shielded teardown in run_async.

Mirrors test_sigint_teardown.py's subprocess/sentinel/ready-file structure —
SIGTERM must trigger the same clean-cancel-and-teardown path as SIGINT, but
distinguished by SigtermInterrupt (not KeyboardInterrupt) and exit code 143
(128 + SIGTERM) instead of 130.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

# The script run in the subprocess.  It:
# 1. Writes a "ready" file so the parent knows the event loop is running.
# 2. Defines a coroutine with a structured finalizer that writes a sentinel file.
# 3. Calls run_async() which blocks the main thread.
# 4. The parent test waits for the ready file, then sends SIGTERM.
# 5. Expected outcome: sentinel file written (teardown ran) + exit code 143.
_SUBPROCESS_SCRIPT = textwrap.dedent("""\
    import sys
    import anyio as _anyio
    from lionagi.ln.concurrency import SigtermInterrupt, run_async

    SENTINEL = sys.argv[1]
    READY    = sys.argv[2]

    async def long_running():
        # Signal to the parent that we are inside the event loop and sleeping.
        with open(READY, "w") as f:
            f.write("ready")
        try:
            # Sleep long enough that SIGTERM will arrive before we finish.
            await _anyio.sleep(30)
            return "done"
        finally:
            # Shielded teardown: must run even on cancellation.
            with _anyio.CancelScope(shield=True):
                # Write the sentinel file to prove teardown ran.
                with open(SENTINEL, "w") as f:
                    f.write("teardown_ran")
                # Simulate a brief async DB write.
                await _anyio.sleep(0.05)

    try:
        result = run_async(long_running())
        sys.exit(0)
    except SigtermInterrupt:
        # run_async raises SigtermInterrupt (not KeyboardInterrupt) after
        # teardown completes. Exit 143 = 128 + SIGTERM (Unix convention).
        sys.exit(143)
    except KeyboardInterrupt:
        # Would indicate SIGTERM was misclassified as SIGINT's handler.
        sys.exit(130)
""")


def _run_subprocess_with_sigterm(
    script: str,
    sentinel_path: str,
    ready_path: str,
    *,
    startup_timeout_s: float = 10.0,
    join_timeout_s: float = 15.0,
) -> int:
    """Run script in a subprocess, wait for ready signal, send SIGTERM, return exit code."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, sentinel_path, ready_path],
        # Do NOT use PIPE for stdout/stderr — reading them with proc.wait()
        # (not communicate()) can deadlock when the pipe buffer fills.
        # Suppress output so tests are quiet.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # New process group so we can send SIGTERM only to the child,
        # not to the test runner's own process group.
        start_new_session=True,
    )

    # Wait until the subprocess is inside the event loop (ready file appears),
    # instead of using a fixed sleep.  This removes flakiness under load.
    deadline = time.monotonic() + startup_timeout_s
    while not os.path.exists(ready_path):
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise AssertionError(
                f"Subprocess did not write ready file within {startup_timeout_s}s — "
                "startup failed or event loop never started."
            )
        time.sleep(0.02)

    # Give the event loop a brief moment to actually enter the sleep() call
    # after writing the ready file.
    time.sleep(0.05)

    # Send SIGTERM to the subprocess (not our own process group).
    os.kill(proc.pid, signal.SIGTERM)

    # Wait for the subprocess to exit.  It should finish within join_timeout_s
    # (teardown is ~50ms, then the process exits).
    try:
        proc.wait(timeout=join_timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(
            f"Subprocess did not exit within {join_timeout_s}s after SIGTERM — "
            "possible orphaned child thread / deadlock."
        )

    return proc.returncode


def test_sigterm_runs_shielded_teardown():
    """Teardown (finally block) executes even when SIGTERM cancels run_async."""
    with tempfile.NamedTemporaryFile(suffix=".sentinel", delete=False) as f:
        sentinel = f.name
    with tempfile.NamedTemporaryFile(suffix=".ready", delete=False) as f:
        ready = f.name
    # Remove both files so we can detect them being created.
    os.unlink(sentinel)
    os.unlink(ready)

    try:
        exit_code = _run_subprocess_with_sigterm(_SUBPROCESS_SCRIPT, sentinel, ready)

        # 1. Teardown ran: the sentinel file must exist.
        assert os.path.exists(sentinel), (
            "Sentinel file was NOT written — shielded teardown did not run.  "
            "The inner coroutine's finally block was skipped, indicating the child "
            "thread was orphaned by SIGTERM (or SIGTERM was left at its default "
            "disposition and killed the process before any Python code could run)."
        )
        with open(sentinel) as fh:
            content = fh.read()
        assert content == "teardown_ran", f"Unexpected sentinel content: {content!r}"

        # 2. Process exited with 143 (SIGTERM convention), not 130 (SIGINT).
        assert exit_code == 143, (
            f"Expected exit code 143 (SIGTERM), got {exit_code}.  "
            "run_async should raise SigtermInterrupt (not KeyboardInterrupt) "
            "after teardown completes."
        )
    finally:
        for path in (sentinel, ready):
            if os.path.exists(path):
                os.unlink(path)


def test_sigterm_does_not_orphan_thread():
    """Subprocess exits within the grace period (no orphaned background thread)."""
    with tempfile.NamedTemporaryFile(suffix=".sentinel2", delete=False) as f:
        sentinel = f.name
    with tempfile.NamedTemporaryFile(suffix=".ready2", delete=False) as f:
        ready = f.name
    os.unlink(sentinel)
    os.unlink(ready)
    try:
        # If the thread is orphaned, proc.wait() would time out.
        exit_code = _run_subprocess_with_sigterm(
            _SUBPROCESS_SCRIPT,
            sentinel,
            ready,
            join_timeout_s=15.0,
        )
        # Any exit code is acceptable here — the important thing is that
        # the subprocess exited at all (no timeout / orphaned thread).
        assert exit_code is not None  # always true if we got here without timeout
    finally:
        for path in (sentinel, ready):
            if os.path.exists(path):
                os.unlink(path)


def test_run_async_sigterm_raises_distinct_type_not_keyboardinterrupt():
    """SigtermInterrupt must not be (or be caught by) KeyboardInterrupt.

    Callers rely on being able to tell "external termination" (SIGTERM) apart
    from "user pressed Ctrl-C" (SIGINT); if SigtermInterrupt were ever made a
    KeyboardInterrupt subclass, that distinction would silently disappear.
    """
    from lionagi.ln.concurrency import SigtermInterrupt

    assert not issubclass(SigtermInterrupt, KeyboardInterrupt)
    assert issubclass(SigtermInterrupt, BaseException)


def test_run_async_sigterm_before_thread_start_still_cancels(monkeypatch):
    """A SIGTERM latched before the worker thread starts must still cancel.

    Regression test for a race where SIGTERM arriving after the handler is
    installed but before _loop_and_task_future is populated left the signal
    "swallowed": SIGTERM's default disposition (SIG_DFL) isn't callable as a
    fallback the way SIGINT's default_int_handler is, so the handler set
    _term_requested and returned without cancelling anything, and the inner
    coroutine ran to full completion before SigtermInterrupt was ever raised.

    Reproduced deterministically by invoking run_async's installed SIGTERM
    handler from threading.Thread.start, right before the worker starts. At
    that point _loop_and_task_future cannot possibly be ready.
    """
    import threading

    import anyio
    import pytest

    import lionagi.ln.concurrency.utils as concurrency_utils
    from lionagi.ln.concurrency import run_async
    from lionagi.ln.concurrency.utils import SigtermInterrupt

    completed = False

    async def long_running():
        nonlocal completed
        await anyio.sleep(5)
        completed = True

    original_start = threading.Thread.start
    handlers = {}

    def capture_handler(signum, handler):
        handlers[signum] = handler

    def start_after_sigterm(self, *args, **kwargs):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return original_start(self, *args, **kwargs)

    monkeypatch.setattr(concurrency_utils.signal, "signal", capture_handler)
    monkeypatch.setattr(threading.Thread, "start", start_after_sigterm)

    with pytest.raises(SigtermInterrupt):
        run_async(long_running())
    assert completed is False, "the latched signal must cancel rather than complete the coroutine"
