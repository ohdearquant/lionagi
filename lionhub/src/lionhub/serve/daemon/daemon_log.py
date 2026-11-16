# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Studio daemon's own log lines: the scheduler's INFO records on stderr while the app serves."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ("DAEMON_LOG_FORMAT", "SCHEDULER_LOGGER", "daemon_log_handler")

DAEMON_LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s"
SCHEDULER_LOGGER = "lionagi.studio.scheduler"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time, so a swapped stream (a test's capture,
    a redirect set up after start) receives the record rather than a closed file.

    It also carries what the first entry found on the logger, and how many entries are active, so
    the last one to leave restores it whatever order they leave in.
    """

    def __init__(self, level: int, propagate: bool) -> None:
        super().__init__(stream=sys.stderr)
        self.entries = 0
        self.saved_level = level
        self.saved_propagate = propagate

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


_entries_lock = threading.Lock()
# The handler of the active entries, kept here rather than looked up on the logger: code that
# removes it from the logger while the app serves must not make the next entry start a second one.
_active: _StderrHandler | None = None


@contextmanager
def daemon_log_handler() -> Iterator[logging.Handler]:
    """Print the scheduler's INFO and above to stderr while the app serves.

    The app's lifespan enters this, so every way of serving gets it: ``li studio``,
    ``python -m lionagi.studio`` and ``uvicorn lionagi.studio.app:app``. Without it the scheduler's
    INFO records (``Firing schedule ...``) reach only Python's last-resort handler, which prints
    WARNING and above, and ``li`` also sets the ``lionagi`` logger to WARNING outside verbose mode.

    Only the ``lionagi.studio.scheduler`` logger changes. Its own level is set to INFO, or kept at
    DEBUG when it was already printing DEBUG, so a later change to the ``lionagi`` logger does not
    drop the lines. ``propagate`` is off while serving, so a root handler some import installed does
    not print each line a second time; root handlers therefore do not see scheduler records while
    the app serves, and a handler meant to is added to ``lionagi.studio.scheduler`` itself. Entries
    while another is active share its handler (putting it back on the logger if something removed
    it) and set the level and ``propagate`` again; the last one to exit restores the handler, level
    and ``propagate`` the first one found.
    """
    global _active
    logger = logging.getLogger(SCHEDULER_LOGGER)
    with _entries_lock:
        handler = _active
        if handler is None:
            handler = _StderrHandler(logger.level, logger.propagate)
            formatter = logging.Formatter(DAEMON_LOG_FORMAT, datefmt=_DATE_FORMAT)
            formatter.converter = time.gmtime
            handler.setFormatter(formatter)
            _active = handler
        if handler not in logger.handlers:
            logger.addHandler(handler)
        handler.entries += 1
        logger.setLevel(min(logger.getEffectiveLevel(), logging.INFO))
        logger.propagate = False
    try:
        yield handler
    finally:
        with _entries_lock:
            handler.entries -= 1
            if handler.entries == 0:
                _active = None
                logger.removeHandler(handler)
                logger.setLevel(handler.saved_level)
                logger.propagate = handler.saved_propagate
