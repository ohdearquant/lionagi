"""CLI bridge invoked by the Playwright global setup.

Run as a module (so the relative import below resolves) from the repo root:

    uv run python -m tests.e2e_studio.run_seeded_daemon --port <port>

Starts a seeded Studio daemon subprocess, prints one JSON line to stdout once
it is healthy, then blocks until SIGTERM/SIGINT, tearing the daemon and its
temp dir down on exit.
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
