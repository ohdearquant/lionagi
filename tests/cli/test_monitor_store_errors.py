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

    argv = ["monitor", *([entity] if entity else []), *(["--watch"] if watch else [])]
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert "schema version" in captured.out + captured.err
    assert exit_code != 0
    assert "schema version" in captured.err
    assert "schema version" not in captured.out


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
    argv = ["monitor", *([entity] if entity else []), *(["--watch"] if watch else [])]
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code != 0
    assert ("error reading monitor data" if entity else "error reading state.db") in captured.err
    assert "fake-password" not in captured.err + captured.out
    assert "db.example" in captured.err
