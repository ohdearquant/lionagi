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
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from .harness import SeededDaemon


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    daemon = SeededDaemon.start(host=args.host, port=args.port, ready_timeout=args.ready_timeout)

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

    stop_requested = False

    def _handle_signal(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stop_requested and daemon.process.poll() is None:
            time.sleep(0.25)
    finally:
        daemon.stop()

    if stop_requested:
        return 0

    return _report_child_exit(daemon.process.returncode)


def _report_child_exit(returncode: int | None) -> int:
    """Print one line naming how the daemon child left, and return a non-zero status.

    Reached only when the child exited on its own: the wrapper's own stop
    returns 0 above. A child that exited 0 by itself still ended the run
    without a stop request, so it reports 1 -- "exited cleanly" and "the
    daemon is gone" are different things to a caller.
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
