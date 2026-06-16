# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for SIGINT-shielded teardown in run_async."""

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
# 4. The parent test waits for the ready file, then sends SIGINT.
# 5. Expected outcome: sentinel file written (teardown ran) + exit code 130.
_SUBPROCESS_SCRIPT = textwrap.dedent("""\
    import sys
    import anyio as _anyio
    from lionagi.ln.concurrency import run_async

    SENTINEL = sys.argv[1]
    READY    = sys.argv[2]

    async def long_running():
        # Signal to the parent that we are inside the event loop and sleeping.
        with open(READY, "w") as f:
            f.write("ready")
        try:
            # Sleep long enough that SIGINT will arrive before we finish.
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
    except KeyboardInterrupt:
        # run_async re-raises KeyboardInterrupt after teardown completes.
        # Exit 130 = 128 + SIGINT (Unix convention for signal-terminated processes).
        sys.exit(130)
""")


def _run_subprocess_with_sigint(
    script: str,
    sentinel_path: str,
    ready_path: str,
    *,
    startup_timeout_s: float = 10.0,
    join_timeout_s: float = 15.0,
) -> int:
    """Run script in a subprocess, wait for ready signal, send SIGINT, return exit code."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, sentinel_path, ready_path],
        # Do NOT use PIPE for stdout/stderr — reading them with proc.wait()
        # (not communicate()) can deadlock when the pipe buffer fills.
        # Suppress output so tests are quiet.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # New process group so we can send SIGINT only to the child,
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

    # Send SIGINT to the subprocess (not our own process group).
    os.kill(proc.pid, signal.SIGINT)

    # Wait for the subprocess to exit.  It should finish within join_timeout_s
    # (teardown is ~50ms, then the process exits).
    try:
        proc.wait(timeout=join_timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(
            f"Subprocess did not exit within {join_timeout_s}s after SIGINT — "
            "possible orphaned child thread / deadlock."
        )

    return proc.returncode


def test_sigint_runs_shielded_teardown():
    """Teardown (finally block) executes even when SIGINT cancels run_async."""
    with tempfile.NamedTemporaryFile(suffix=".sentinel", delete=False) as f:
        sentinel = f.name
    with tempfile.NamedTemporaryFile(suffix=".ready", delete=False) as f:
        ready = f.name
    # Remove both files so we can detect them being created.
    os.unlink(sentinel)
    os.unlink(ready)

    try:
        exit_code = _run_subprocess_with_sigint(_SUBPROCESS_SCRIPT, sentinel, ready)

        # 1. Teardown ran: the sentinel file must exist.
        assert os.path.exists(sentinel), (
            "Sentinel file was NOT written — shielded teardown did not run.  "
            "The inner coroutine's finally block was skipped, indicating the child "
            "thread was orphaned by SIGINT (phantom-session regression)."
        )
        with open(sentinel) as fh:
            content = fh.read()
        assert content == "teardown_ran", f"Unexpected sentinel content: {content!r}"

        # 2. Process exited with 130 (SIGINT convention).
        assert exit_code == 130, (
            f"Expected exit code 130 (SIGINT), got {exit_code}.  "
            "run_async should re-raise KeyboardInterrupt after teardown completes."
        )
    finally:
        for path in (sentinel, ready):
            if os.path.exists(path):
                os.unlink(path)


def test_sigint_does_not_orphan_thread():
    """Subprocess exits within the grace period (no orphaned background thread)."""
    with tempfile.NamedTemporaryFile(suffix=".sentinel2", delete=False) as f:
        sentinel = f.name
    with tempfile.NamedTemporaryFile(suffix=".ready2", delete=False) as f:
        ready = f.name
    os.unlink(sentinel)
    os.unlink(ready)
    try:
        # If the thread is orphaned, proc.wait() would time out.
        exit_code = _run_subprocess_with_sigint(
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


def test_run_async_normal_completion_unaffected():
    """Regression guard: normal run_async usage (no SIGINT) still works."""
    import anyio

    from lionagi.ln.concurrency import run_async

    async def simple():
        await anyio.sleep(0)
        return 42

    result = run_async(simple())
    assert result == 42


def test_run_async_exception_still_propagates():
    """Exceptions from the coroutine still bubble up through run_async."""
    import anyio
    import pytest

    from lionagi.ln.concurrency import run_async

    async def raises():
        await anyio.sleep(0)
        raise ValueError("expected error")

    with pytest.raises(ValueError, match="expected error"):
        run_async(raises())


def test_run_async_in_non_main_thread_no_signal_handler():
    """run_async called from a non-main thread: no signal handler, still works."""
    import threading

    import anyio

    from lionagi.ln.concurrency import run_async

    results: list = []
    errors: list = []

    def worker():
        try:

            async def coro():
                await anyio.sleep(0)
                return "from_thread"

            results.append(run_async(coro()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert not errors, f"run_async in non-main thread raised: {errors}"
    assert results == ["from_thread"]
