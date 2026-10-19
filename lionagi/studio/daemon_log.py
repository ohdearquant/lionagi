# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Studio daemon's own log lines: the scheduler's INFO records on stderr while the app serves."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ("DAEMON_LOG_FORMAT", "SCHEDULER_LOGGER", "daemon_log_handler")

DAEMON_LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s"
SCHEDULER_LOGGER = "lionagi.studio.scheduler"
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
    """Print the scheduler's INFO and above to stderr while the app serves.

    The app's lifespan enters this, so every way of serving gets it: ``li studio``,
    ``python -m lionagi.studio`` and ``uvicorn lionagi.studio.app:app``. Without it the scheduler's
    INFO records (``Firing schedule ...``) reach only Python's last-resort handler, which prints
    WARNING and above, and ``li`` also sets the ``lionagi`` logger to WARNING outside verbose mode.

    Only the ``lionagi.studio.scheduler`` logger changes. Its level is lifted to INFO when it would
    drop INFO; a DEBUG level is kept. ``propagate`` is off while serving, so a root handler some
    import installed does not print each line a second time; root handlers therefore do not see
    scheduler records while the app serves, and a handler meant to is added to
    ``lionagi.studio.scheduler`` itself. An entry while another is active reuses its handler and
    changes nothing; the outermost exit restores the handler, level and ``propagate``.
    """
    logger = logging.getLogger(SCHEDULER_LOGGER)
    for existing in logger.handlers:
        if isinstance(existing, _StderrHandler):
            yield existing
            return
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
