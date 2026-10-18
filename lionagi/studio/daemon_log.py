# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Studio daemon's own log lines: lionagi's INFO records on stderr while the server runs."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ("DAEMON_LOG_FORMAT", "daemon_log_handler")

DAEMON_LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time, so a swapped stream (a test's capture,
    a redirect set up after start) receives the record rather than a closed file."""

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


@contextmanager
def daemon_log_handler() -> Iterator[logging.Handler]:
    """Print the ``lionagi`` logger's INFO and above to stderr while the daemon serves.

    Without a handler of its own, the scheduler's INFO records (``Firing schedule ...``) reach only
    Python's last-resort handler, which prints WARNING and above, so a daemon firing schedules
    normally logs nothing about it. The CLI also sets the ``lionagi`` level to WARNING outside
    verbose mode; serving lifts it to INFO. ``propagate`` is off while serving, so a root handler
    installed by some import does not print each record a second time. The handler, level and
    ``propagate`` are restored when the server stops. A level already at INFO or below (DEBUG) is
    kept. Uvicorn's loggers are not touched.
    """
    logger = logging.getLogger("lionagi")
    handler = _StderrHandler()
    formatter = logging.Formatter(DAEMON_LOG_FORMAT, datefmt=_DATE_FORMAT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
