"""CLI bridge invoked by the Playwright global setup.

Starts a seeded Studio daemon subprocess, prints one JSON line to stdout once
it is healthy, then blocks until SIGTERM/SIGINT, tearing the daemon and its
temp dir down on exit. The daemon's own output goes to the file named by
``LIONAGI_E2E_STUDIO_CHILD_LOG`` (see ``harness.CHILD_LOG_ENV``), which the
Playwright setup points at its log directory so it outlives the temp dir.

A daemon that exits on its own is reported rather than hidden: the process
prints one line naming the exit code or signal and exits non-zero, so a daemon
that died mid-run fails the run instead of looking like a clean stop. A stop
the caller requests decides the outcome: the whole process group receives the
teardown signal, so the child's own exit is part of the stop it was asked for
and is not reported as the daemon leaving on its own. The signal handlers are
installed before the daemon starts, so a teardown signal that lands while the
daemon is still coming up stops startup instead of being waited out, and the
daemon is stopped the same way as any other. A daemon that never becomes
healthy is reported with the child's own code or signal on its first line and
fails the run, instead of raising an unhandled traceback from startup. A stop
that had to kill a daemon which ignored SIGTERM fails the run with one line
saying so.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from .harness import DaemonStopRequested, SeededDaemon

# Written once, to stderr, whenever a requested stop could not make the daemon
# leave on its own and had to kill it.
KILLED_MESSAGE = "seeded studio daemon ignored SIGTERM; had to kill it"


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
            host=args.host,
            port=args.port,
            ready_timeout=args.ready_timeout,
            stop_requested=lambda: stop_requested,
        )
    except DaemonStopRequested as exc:
        return _report_requested_startup_stop(exc)
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

    try:
        while not stop_requested:
            if daemon.process.poll() is not None:
                break
            time.sleep(0.25)
    finally:
        had_to_kill = daemon.stop()

    # A requested stop decides the outcome. Teardown signals the whole process
    # group, so the child's exit code belongs to the stop we were asked for,
    # not to the daemon leaving on its own: report a clean requested stop. Only
    # a stop that could not make the child leave has something to report.
    if stop_requested:
        if had_to_kill:
            print(KILLED_MESSAGE, file=sys.stderr)
            return 1
        return 0

    # No stop was requested, so this death is the daemon's own and is reported.
    return _report_child_exit(daemon.process.poll())


def _report_requested_startup_stop(exc: DaemonStopRequested) -> int:
    """Report a stop that arrived while the daemon was still starting.

    The caller is stopping the run, so the child was stopped through the usual
    path. A child that left on its own is a clean stop; one that had to be
    killed is reported and fails the run.
    """
    if exc.had_to_kill:
        print(KILLED_MESSAGE, file=sys.stderr)
        return 1
    return 0


def _report_start_failure(exc: Exception) -> int:
    """Print one line naming a startup failure and return a non-zero status.

    ``SeededDaemon.start`` raises when the daemon exits before it answers
    /health or when a stop arrives during startup; the wrapper reports the
    child's exit code or signal on the first line, then the failure's own
    message, instead of an unhandled traceback. The status is the child's
    positive code, or 128 plus the signal for a signal death, or 1 when the
    child reports neither.
    """
    returncode = getattr(exc, "returncode", None)
    if isinstance(returncode, int) and returncode < 0:
        headline = f"child signal={-returncode}"
    elif isinstance(returncode, int):
        headline = f"child code={returncode}"
    else:
        headline = "child code unknown"
    print(f"seeded studio daemon failed before ready: {headline}", file=sys.stderr)
    print(f"{exc}", file=sys.stderr)
    if getattr(exc, "had_to_kill", False):
        print(KILLED_MESSAGE, file=sys.stderr)
    if isinstance(returncode, int):
        if returncode < 0:
            return 128 - returncode
        if returncode > 0:
            return returncode
    return 1


def _report_child_exit(returncode: int | None) -> int:
    """Print one line naming how the daemon child left, and return a non-zero status.

    Reached when the child exited on its own, with no stop requested; only a
    child that was still running when the wrapper stopped it returns 0. A child
    that exited 0 by itself still ended the run without the wrapper asking it
    to, so it reports 1 -- "exited cleanly" and "the daemon is gone" are
    different things to a caller.
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
