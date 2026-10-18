# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The daemon prints lionagi's INFO records (a schedule firing) on stderr while it serves."""

from __future__ import annotations

import calendar
import logging
import re
import sys
import time

import pytest

from lionagi.studio.daemon_log import daemon_log_handler

FIRE = "Firing schedule nightly (run r1, chain_depth=0)"
LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO:lionagi\.studio\.scheduler\.engine:"
    + re.escape(FIRE)
    + "$"
)


@pytest.fixture()
def lionagi_logger():
    logger = logging.getLogger("lionagi")
    saved = (logger.level, logger.propagate, list(logger.handlers))
    yield logger
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    logger.handlers[:] = saved[2]


@pytest.fixture()
def root_handler(capsys):
    # A root handler some import installed: without propagate=False each record prints twice.
    # Built after capsys swaps sys.stderr, so what it prints is captured too.
    handler = logging.StreamHandler(sys.stderr)
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)


def _fire():
    logging.getLogger("lionagi.studio.scheduler.engine").info(FIRE)


def _fire_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if FIRE in line]


def test_a_fire_record_prints_once_with_a_utc_timestamp(lionagi_logger, root_handler, capsys):
    lionagi_logger.setLevel(logging.WARNING)  # what the CLI sets outside verbose mode
    with daemon_log_handler():
        _fire()
    lines = _fire_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert LINE.match(lines[0]), lines[0]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no time.tzset")
def test_the_timestamp_is_utc_whatever_the_local_zone(lionagi_logger, capsys, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kathmandu")  # UTC+05:45: a local-time stamp is far off
    time.tzset()
    try:
        with daemon_log_handler():
            _fire()
    finally:
        monkeypatch.undo()
        time.tzset()
    [line] = _fire_lines(capsys.readouterr().err)
    stamp = calendar.timegm(time.strptime(line.split(".", 1)[0], "%Y-%m-%dT%H:%M:%S"))
    assert abs(stamp - time.time()) < 120, line


def test_serving_ends_with_the_logger_as_it_was(lionagi_logger, capsys):
    lionagi_logger.setLevel(logging.WARNING)
    lionagi_logger.propagate = True
    handlers = list(lionagi_logger.handlers)
    with daemon_log_handler():
        pass
    assert lionagi_logger.level == logging.WARNING
    assert lionagi_logger.propagate is True
    assert lionagi_logger.handlers == handlers
    _fire()
    assert _fire_lines(capsys.readouterr().err) == []


def test_a_debug_level_is_kept_while_serving(lionagi_logger):
    lionagi_logger.setLevel(logging.DEBUG)
    with daemon_log_handler():
        assert lionagi_logger.level == logging.DEBUG
    assert lionagi_logger.level == logging.DEBUG


def test_python_m_lionagi_studio_serves_inside_the_handler(
    lionagi_logger, root_handler, capsys, monkeypatch
):
    import runpy

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: _fire())
    lionagi_logger.setLevel(logging.WARNING)
    runpy.run_module("lionagi.studio", run_name="__main__")
    lines = _fire_lines(capsys.readouterr().err)
    assert len(lines) == 1 and LINE.match(lines[0]), lines
