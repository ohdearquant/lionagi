"""CLI bridge invoked by the Playwright global setup.

Run as a module (so the relative import below resolves) from the repo root:

    uv run python -m tests.e2e_studio.run_seeded_daemon --port <port>

Starts a seeded Studio daemon subprocess, prints one JSON line to stdout once
it is healthy, then blocks until SIGTERM/SIGINT, tearing the daemon and its
temp dir down on exit. The daemon's own output goes to the file named by
``LIONAGI_E2E_STUDIO_CHILD_LOG`` (see ``harness.CHILD_LOG_ENV``), which the
Playwright setup points at its log directory so it outlives the temp dir.

A daemon that exits on its own is reported rather than hidden: the process
prints one line naming the exit code or signal and exits non-zero, so a
daemon that died mid-run fails the run instead of looking like a clean stop.
The signal handlers are installed before the daemon starts, and a requested
stop still reads the child's own exit first, so a daemon that had already died
when the teardown signal arrived is reported the same way. A daemon that never
becomes healthy is reported on one line and fails the run, instead of raising
an unhandled traceback from startup.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import subprocess
import sys
import time

from .harness import SeededDaemon


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    stop_requested = False

    def _handle_signal(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    # Installed before startup so a teardown signal that lands while the daemon
    # is still coming up is remembered rather than killing the wrapper outright.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        daemon = SeededDaemon.start(
            host=args.host, port=args.port, ready_timeout=args.ready_timeout
        )
    except Exception as exc:
        return _report_start_failure(exc)

    print(
        json.dumps(
            {
                "event": "studio-e2e-daemon-ready",
                "base_url": daemon.base_url,
                "host": daemon.host,
                "port": daemon.port,
                "tmp_dir": str(daemon.tmp_dir),
            }
        ),
        flush=True,
    )

    own_exit: int | None = None
    try:
        while not stop_requested:
            if daemon.process.poll() is not None:
                break
            time.sleep(0.25)
        # Read the child's own exit once more before stop() can signal it: a
        # stop request that arrives after the daemon already died must still
        # report the death, not a clean stop.
        own_exit = daemon.process.poll()
    finally:
        if stop_requested and own_exit is None:
            _ask_for_a_thread_dump(daemon.process)
        daemon.stop()

    if own_exit is not None:
        return _report_child_exit(own_exit)

    # A stop this wrapper requested, on a child that was still running: the
    # exit code stop() returns belongs to our own signal, not to the daemon.
    return 0


def _ask_for_a_thread_dump(process: subprocess.Popen) -> None:
    """Ask a still-running child to dump every thread's stack to its own log.

    The daemon registers ``faulthandler`` on SIGUSR1 (see
    ``tests.e2e_studio.studio_app``), so a hung daemon's stacks land in the
    child log -- the file the Playwright setup keeps as a failure artifact --
    before stop's SIGTERM/SIGKILL. POSIX only; skipped where the signal does
    not exist.
    """
    dump_signal = getattr(signal, "SIGUSR1", None)
    if dump_signal is None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.send_signal(dump_signal)
        time.sleep(0.5)


def _report_start_failure(exc: Exception) -> int:
    """Print one line naming a startup failure and return a non-zero status.

    ``SeededDaemon.start`` raises when the daemon exits before it answers
    /health; the wrapper reports that on one line instead of an unhandled
    traceback, with the child's exit code or signal when it has one (128 plus
    the signal for a signal death, else 1).
    """
    returncode = getattr(exc, "returncode", None)
    detail = ""
    if isinstance(returncode, int):
        if returncode < 0:
            detail = f" (child signal={-returncode})"
        else:
            detail = f" (child code={returncode})"
    print(f"seeded studio daemon failed before ready: {exc}{detail}", file=sys.stderr)
    if isinstance(returncode, int) and returncode < 0:
        return 128 - returncode
    return 1


def _report_child_exit(returncode: int | None) -> int:
    """Print one line naming how the daemon child left, and return a non-zero status.

    Reached when the child exited on its own, whether before a stop request or
    just as one arrived; only a child that was still running when the wrapper
    stopped it returns 0. A child that exited 0 by itself still ended the run
    without the wrapper asking it to, so it reports 1 -- "exited cleanly" and
    "the daemon is gone" are different things to a caller.
    """
    if returncode is None:
        print("seeded studio daemon child exited on its own: code unknown", file=sys.stderr)
        return 1
    if returncode < 0:
        signum = -returncode
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = "unknown"
        print(
            f"seeded studio daemon child exited on its own: signal={signum} ({name})",
            file=sys.stderr,
        )
        return 128 + signum
    print(f"seeded studio daemon child exited on its own: code={returncode}", file=sys.stderr)
    return returncode or 1


if __name__ == "__main__":
    sys.exit(main())
