# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A monitor that cannot read its store must not report success."""

from __future__ import annotations

import asyncio
import signal
import sqlite3
from contextlib import contextmanager

import pytest

from lionagi.cli import monitor
from lionagi.cli.main import main
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.state import db as state_db


@pytest.fixture
def newer_store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    monkeypatch.setattr(state_db, "DEFAULT_DB_PATH", path)

    async def initialize():
        async with state_db.StateDB(path=path):
            pass

    asyncio.run(initialize())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'",
            (str(int(state_db.SCHEMA_VERSION) + 1),),
        )
    return path


@pytest.mark.parametrize("watch", [False, True], ids=["once", "watch"])
@pytest.mark.parametrize("entity", [None, "example"], ids=["table", "detail"])
def test_unreadable_store_fails_on_stderr(newer_store, monkeypatch, capsys, watch, entity):
    """Exercise a real refusal through the CLI, including its watch loop."""
    if watch:
        import lionagi.ln.concurrency as concurrency

        handlers = []

        @contextmanager
        def fake_handlers(handler):
            handlers.append(handler)
            yield

        monkeypatch.setattr(concurrency, "temporary_signal_handlers", fake_handlers)
        # The old implementation prints a bogus success and would keep looping;
        # stop that old path deterministically without signalling a process.
        monkeypatch.setattr(monitor, "_clear_screen", lambda: handlers[0](signal.SIGINT, None))
        monkeypatch.setattr(monitor.time, "sleep", lambda _secs: None)

    argv = ["monitor", *([entity] if entity else []), *(["--watch"] if watch else [])]
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert "schema version" in captured.out + captured.err
    assert exit_code == EXIT_UNKNOWN
    assert "schema version" in captured.err
    assert "schema version" not in captured.out
    # A refusing store is retried, then the watch gives up: one message per attempt.
    attempts = monitor._WATCH_READ_ATTEMPTS if watch else 1
    prefix = "error reading monitor data" if entity else "error reading state.db"
    assert captured.err.count(prefix) == attempts


@pytest.mark.parametrize("watch", [False, True], ids=["once", "watch"])
@pytest.mark.parametrize("entity", [None, "example"], ids=["table", "detail"])
def test_store_error_does_not_disclose_credentials(monkeypatch, capsys, watch, entity):
    class BrokenStore:
        async def __aenter__(self):
            raise RuntimeError("postgresql://reader:fake-password@db.example/test")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(state_db, "StateDB", BrokenStore)
    monkeypatch.setattr(monitor.time, "sleep", lambda _secs: None)
    argv = ["monitor", *([entity] if entity else []), *(["--watch"] if watch else [])]
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code == EXIT_UNKNOWN
    assert ("error reading monitor data" if entity else "error reading state.db") in captured.err
    assert "fake-password" not in captured.err + captured.out
    assert "db.example" in captured.err


class _Credentialed(RuntimeError):
    def __init__(self):
        super().__init__("postgresql://reader:fake-password@db.example/test")


def _watch_until(monkeypatch, stop_after_calls, outcomes):
    """Drive `li monitor --watch` through a fake table read, one outcome per refresh.

    The loop is stopped through its own signal handler once the fake has been
    called ``stop_after_calls`` times, from inside the loop's sleep.
    """
    import lionagi.ln.concurrency as concurrency

    handlers = []
    calls = []

    @contextmanager
    def fake_handlers(handler):
        handlers.append(handler)
        yield

    async def fake_table(**_kwargs):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def fake_sleep(_secs):
        if len(calls) >= stop_after_calls:
            handlers[0](signal.SIGINT, None)

    monkeypatch.setattr(concurrency, "temporary_signal_handlers", fake_handlers)
    monkeypatch.setattr(monitor, "_run_table", fake_table)
    monkeypatch.setattr(monitor.time, "sleep", fake_sleep)
    return calls


def test_one_failed_refresh_does_not_end_the_watch(monkeypatch, capsys):
    locked = monitor._MonitorReadError("error reading state.db: database is locked")
    calls = _watch_until(monkeypatch, 2, [locked, "TABLE-AFTER-RECOVERY"])

    exit_code = main(["monitor", "--watch"])
    captured = capsys.readouterr()

    assert calls == [locked, "TABLE-AFTER-RECOVERY"]
    assert exit_code == 0
    assert captured.err.count("database is locked") == 1
    assert "TABLE-AFTER-RECOVERY" in captured.out


def test_a_watch_stopped_while_the_store_fails_exits_nonzero(monkeypatch, capsys):
    locked = monitor._MonitorReadError("error reading state.db: database is locked")
    calls = _watch_until(monkeypatch, 1, [locked])

    exit_code = main(["monitor", "--watch"])
    captured = capsys.readouterr()

    # Stopped after one failure, below the bound: the exit still says the store was unreadable.
    assert len(calls) == 1 < monitor._WATCH_READ_ATTEMPTS
    assert exit_code == EXIT_UNKNOWN
    assert captured.err.count("database is locked") == 1


@pytest.mark.parametrize("argv", [["monitor", "run", "abc"], ["monitor", "--run", "abc"]])
def test_a_wait_that_cannot_open_the_store_fails_masked(monkeypatch, capsys, argv):
    class BrokenStore:
        async def __aenter__(self):
            raise _Credentialed()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(state_db, "StateDB", BrokenStore)

    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code == EXIT_UNKNOWN
    assert "error reading state.db" in captured.err
    assert "fake-password" not in captured.err + captured.out
    assert "db.example" in captured.err


def test_a_wait_that_loses_the_store_after_resolving_fails_masked(monkeypatch, capsys):
    opened = []

    class StoreThatFailsOnSecondOpen:
        async def __aenter__(self):
            opened.append(True)
            if len(opened) > 1:
                raise _Credentialed()
            return object()

        async def __aexit__(self, *_args):
            return False

    async def resolved(_db, ids):
        return {ids[0]: {"id": ids[0]}}, {}, []

    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(state_db, "StateDB", StoreThatFailsOnSecondOpen)
    monkeypatch.setattr(monitor, "_resolve_watched_runs", resolved)
    monkeypatch.setattr(monitor.time, "sleep", lambda _secs: None)

    exit_code = main(["monitor", "run", "abc"])
    captured = capsys.readouterr()

    # The first open resolved the id; the failure is the poll's, not the lookup's.
    assert len(opened) == 2
    assert exit_code == EXIT_UNKNOWN
    assert "fake-password" not in captured.err + captured.out
    assert "db.example" in captured.err
