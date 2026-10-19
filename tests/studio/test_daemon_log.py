# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The daemon prints the scheduler's INFO records (a schedule firing) on stderr while it serves."""

from __future__ import annotations

import calendar
import io
import logging
import re
import stat
import sys
import time

import pytest

from lionagi.studio.daemon_log import SCHEDULER_LOGGER, daemon_log_handler

FIRE = "Firing schedule nightly (run r1, chain_depth=0)"
LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO:lionagi\.studio\.scheduler\.engine:"
    + re.escape(FIRE)
    + "$"
)


@pytest.fixture()
def loggers():
    """The ``lionagi`` and scheduler loggers, restored after the test whatever it changed."""
    saved = []
    for name in ("lionagi", SCHEDULER_LOGGER):
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.propagate, list(logger.handlers)))
    yield logging.getLogger("lionagi"), logging.getLogger(SCHEDULER_LOGGER)
    for logger, level, propagate, handlers in saved:
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers[:] = handlers


class _RootStderrHandler(logging.Handler):
    """Stands in for the root stderr handler an import's ``logging.basicConfig`` installs; it
    writes to the ``sys.stderr`` of the moment, so the test's capture receives what it prints."""

    def emit(self, record: logging.LogRecord) -> None:
        sys.stderr.write(self.format(record) + "\n")


@pytest.fixture()
def root_handler(capsys):
    # Without propagate=False each scheduler record would print twice: once here, once timestamped.
    handler = _RootStderrHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)


def _fire(level: int = logging.INFO, message: str = FIRE) -> None:
    logging.getLogger("lionagi.studio.scheduler.engine").log(level, message)


def _lines(err: str, message: str = FIRE) -> list[str]:
    return [line for line in err.splitlines() if message in line]


def test_a_fire_record_prints_once_with_a_utc_timestamp(loggers, root_handler, capsys):
    lionagi, _ = loggers
    lionagi.setLevel(logging.WARNING)  # what the CLI sets outside verbose mode
    with daemon_log_handler():
        _fire()
    lines = _lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert LINE.match(lines[0]), lines[0]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no time.tzset")
def test_the_timestamp_is_utc_whatever_the_local_zone(loggers, capsys, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kathmandu")  # UTC+05:45: a local-time stamp is far off
    time.tzset()
    try:
        with daemon_log_handler():
            _fire()
    finally:
        monkeypatch.undo()
        time.tzset()
    [line] = _lines(capsys.readouterr().err)
    stamp = calendar.timegm(time.strptime(line.split(".", 1)[0], "%Y-%m-%dT%H:%M:%S"))
    assert abs(stamp - time.time()) < 120, line


def test_a_stream_swapped_after_serving_starts_receives_the_record(loggers, capsys, monkeypatch):
    with daemon_log_handler():
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)
        _fire()
    assert len(_lines(swapped.getvalue())) == 1, swapped.getvalue()
    assert _lines(capsys.readouterr().err) == []


def test_serving_ends_with_the_logger_as_it_was(loggers, capsys):
    lionagi, scheduler = loggers
    lionagi.setLevel(logging.WARNING)
    scheduler.setLevel(logging.NOTSET)
    scheduler.propagate = True
    handlers = list(scheduler.handlers)
    with daemon_log_handler():
        assert scheduler.level == logging.INFO
        assert scheduler.propagate is False
    assert scheduler.level == logging.NOTSET
    assert scheduler.propagate is True
    assert scheduler.handlers == handlers
    _fire()
    assert _lines(capsys.readouterr().err) == []


def test_a_debug_level_is_kept_and_its_records_print(loggers, capsys):
    lionagi, scheduler = loggers
    lionagi.setLevel(logging.DEBUG)
    scheduler.setLevel(logging.NOTSET)
    with daemon_log_handler():
        assert scheduler.getEffectiveLevel() == logging.DEBUG
        _fire(logging.DEBUG, "Spawning: li agent")
    assert scheduler.level == logging.NOTSET
    [line] = _lines(capsys.readouterr().err, "Spawning: li agent")
    assert " DEBUG:lionagi.studio.scheduler.engine:" in line, line


def test_a_nested_entry_reuses_the_handler_and_the_outer_exit_restores(loggers, capsys):
    _, scheduler = loggers
    handlers = list(scheduler.handlers)
    with daemon_log_handler() as outer:
        with daemon_log_handler() as inner:
            assert inner is outer
            _fire()
        assert outer in scheduler.handlers
        assert scheduler.propagate is False
        _fire()
    assert scheduler.handlers == handlers
    assert len(_lines(capsys.readouterr().err)) == 2


def test_a_lionagi_level_set_while_serving_does_not_drop_the_lines(loggers, capsys):
    lionagi, scheduler = loggers
    lionagi.setLevel(logging.INFO)
    scheduler.setLevel(logging.NOTSET)
    with daemon_log_handler():
        lionagi.setLevel(logging.WARNING)  # the CLI's logging setup, run after serving began
        _fire()
        with daemon_log_handler():
            _fire()
    assert scheduler.level == logging.NOTSET
    assert len(_lines(capsys.readouterr().err)) == 2


def test_entries_leaving_out_of_order_keep_printing_until_the_last_leaves(loggers, capsys):
    _, scheduler = loggers
    scheduler.setLevel(logging.NOTSET)
    scheduler.propagate = True
    handlers = list(scheduler.handlers)
    first, second = daemon_log_handler(), daemon_log_handler()
    handler = first.__enter__()
    assert second.__enter__() is handler
    first.__exit__(None, None, None)
    assert handler in scheduler.handlers
    assert scheduler.propagate is False
    _fire()
    second.__exit__(None, None, None)
    assert scheduler.handlers == handlers
    assert scheduler.level == logging.NOTSET
    assert scheduler.propagate is True
    assert len(_lines(capsys.readouterr().err)) == 1


@pytest.mark.parametrize("first_leaves_first", [False, True], ids=["nested", "overlapping"])
def test_a_handler_removed_while_serving_comes_back_and_one_restore_runs(
    loggers, capsys, first_leaves_first
):
    _, scheduler = loggers
    scheduler.setLevel(logging.NOTSET)
    scheduler.propagate = True
    handlers = list(scheduler.handlers)
    first, second = daemon_log_handler(), daemon_log_handler()
    handler = first.__enter__()
    scheduler.removeHandler(handler)  # some other code, while the app serves
    assert second.__enter__() is handler
    assert handler in scheduler.handlers
    (first if first_leaves_first else second).__exit__(None, None, None)
    _fire()
    (second if first_leaves_first else first).__exit__(None, None, None)
    assert scheduler.handlers == handlers
    assert scheduler.level == logging.NOTSET
    assert scheduler.propagate is True
    assert len(_lines(capsys.readouterr().err)) == 1


def test_a_second_entry_pins_the_level_and_propagate_again(loggers, root_handler, capsys):
    lionagi, scheduler = loggers
    lionagi.setLevel(logging.WARNING)
    with daemon_log_handler():
        # Changed by other code while serving: INFO would be dropped, or printed by the root too.
        scheduler.setLevel(logging.WARNING)
        scheduler.propagate = True
        with daemon_log_handler():
            assert scheduler.level == logging.INFO
            assert scheduler.propagate is False
            _fire()
    assert len(_lines(capsys.readouterr().err, FIRE)) == 1


def test_records_outside_the_scheduler_are_left_to_the_root_handlers(loggers, root_handler, capsys):
    lionagi, _ = loggers
    lionagi.setLevel(logging.INFO)
    message = "Studio route registered"
    with daemon_log_handler():
        assert lionagi.propagate is True
        logging.getLogger("lionagi.studio.app").info(message)
    # Printed once, by the root handler in its own format, never by the daemon's handler.
    assert _lines(capsys.readouterr().err, message) == [message]


@pytest.mark.asyncio
async def test_a_spawn_prints_none_of_its_arguments_while_serving(loggers, capsys, tmp_path):
    from lionagi.studio.scheduler.subprocess import spawn_and_wait

    child = tmp_path / "quiet-child"
    child.write_text("#!/bin/sh\nexit 0\n")
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    prompt = "Review the nightly diff\nsecond line of the prompt"
    with daemon_log_handler():
        exit_code, _ = await spawn_and_wait([str(child), prompt], "inv-quiet", deadline_seconds=30)
    assert exit_code == 0
    err = capsys.readouterr().err
    # The same run's exit line printed, so the handler was live while the arguments were not.
    assert "INFO:lionagi.studio.scheduler.subprocess:Process exited with code 0" in err, err
    assert "nightly diff" not in err and "second line" not in err, err
