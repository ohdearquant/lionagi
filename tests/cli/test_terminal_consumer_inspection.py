# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read-only inspection of terminal consumers, including change previews."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess

import pytest
from sqlalchemy import event, text

from lionagi.cli import state as state_cli
from lionagi.state.db import StateDB
from lionagi.state.lifecycle.consumer_inspection import (
    ConsumerInspectionBackendError,
    ConsumerPreviewAlreadyActiveError,
    ConsumerPreviewAlreadyRetiredError,
    list_consumers,
    preview_consumer_change,
)
from lionagi.state.lifecycle.deliveries import (
    ConsumerNotRegisteredError,
    ack_delivery,
    register_consumer,
    retire_consumer,
)


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline inspection test attempted a process, signal or socket operation")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)


async def _seed(db):
    async with db.transaction() as conn:
        for ident, eligible in (("one", True), ("two", True), ("no", False), ("unknown", None)):
            await conn.execute(
                text(
                    "INSERT INTO status_transitions "
                    "(id, entity_type, entity_id, status, reason_code, source, created_at, "
                    "delivery_eligible) VALUES "
                    "(:id, 'session', :id, 'completed', 'test', 'system', 1, :eligible)"
                ),
                {"id": ident, "eligible": eligible},
            )
    await register_consumer(db, "active", start="all")
    await register_consumer(db, "retired", start="all")
    await ack_delivery(db, "one", "active")
    await ack_delivery(db, "one", "retired")
    await retire_consumer(db, "retired")


@pytest.mark.asyncio
async def test_list_and_previews_read_consistent_state_without_writes(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await _seed(db)
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(db._engine.sync_engine, "before_cursor_execute", capture)
        before_consumers = await db.fetch_all("SELECT * FROM terminal_consumers ORDER BY name")
        before_acks = await db.fetch_all("SELECT * FROM terminal_deliveries ORDER BY consumer")
        rows = await list_consumers(db)
        assert [(r.name, r.owed, r.candidate) for r in rows] == [
            ("active", 1, 1),
            ("retired", 0, 1),
        ]
        retire = await preview_consumer_change(db, "active", action="retire")
        assert retire["preview"] is True and retire["would_change"] is True
        assert retire["owed_before"] == 1 and retire["projected_owed"] == 0
        now = await preview_consumer_change(db, "retired", action="reactivate", start="now")
        assert now["owed_before"] == 0 and now["candidate_before"] == 1
        assert now["projected_owed"] == 0 and now["projected_baselined"] == 1
        all_ = await preview_consumer_change(db, "retired", action="reactivate", start="all")
        assert all_["projected_owed"] == 1 and all_["projected_baselined"] == 0
        with pytest.raises(ConsumerPreviewAlreadyRetiredError):
            await preview_consumer_change(db, "retired", action="retire")
        with pytest.raises(ConsumerPreviewAlreadyActiveError):
            await preview_consumer_change(db, "active", action="reactivate", start="now")
        with pytest.raises(ConsumerNotRegisteredError):
            await preview_consumer_change(db, "missing", action="retire")
        with pytest.raises(ValueError, match="requires start"):
            await preview_consumer_change(db, "retired", action="reactivate")
        assert (
            await db.fetch_all("SELECT * FROM terminal_consumers ORDER BY name") == before_consumers
        )
        assert (
            await db.fetch_all("SELECT * FROM terminal_deliveries ORDER BY consumer") == before_acks
        )
        assert not set(statements) & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP"}


def _parser():
    parser = argparse.ArgumentParser()
    state_cli.add_state_subparser(parser.add_subparsers(dest="command", required=True))
    return parser


def test_only_list_is_exposed_before_atomic_mutation_support():
    parser = _parser()
    args = parser.parse_args(["state", "terminal-consumer", "list", "--machine"])
    assert args.terminal_consumer_command == "list" and args.machine
    with pytest.raises(SystemExit):
        parser.parse_args(["state", "terminal-consumer", "retire", "active"])
    with pytest.raises(SystemExit):
        parser.parse_args(["state", "terminal-consumer", "reactivate", "active", "--start", "all"])


def test_human_and_machine_list_share_data(monkeypatch, capsys):
    row = {"name": "a", "registered_at": 1.0, "retired_at": None, "owed": 2, "candidate": 2}

    async def fake_data():
        return {
            "consumers": {"available": True, "value": [row], "reason_code": None, "detail": None}
        }

    monkeypatch.setattr(state_cli, "_terminal_consumer_list_data", fake_data)
    args = _parser().parse_args(["state", "terminal-consumer", "list"])
    assert state_cli.run_state(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "name\tregistered_at\tretired_at\towed",
        "a\t1.0\t-\t2",
    ]
    result = state_cli.machine_result(["terminal-consumer", "list"])
    assert result["consumers"]["value"] == [row]


@pytest.mark.asyncio
async def test_non_readonly_backend_is_refused_without_opening_store(monkeypatch):
    from lionagi.cli import machine
    from lionagi.state import db as db_module

    class FakePostgres:
        dialect = "postgresql"

        async def open(self):
            pytest.fail("refusal must occur before opening a connection")

        async def __aenter__(self):
            pytest.fail("refusal must occur before opening a connection")

    monkeypatch.setattr(db_module, "StateDB", FakePostgres)
    monkeypatch.setattr(db_module, "read_only_open_supported", lambda: False)
    monkeypatch.setattr(machine, "readonly_state_db", _forbid)
    with pytest.raises(ConsumerInspectionBackendError) as caught:
        await state_cli._terminal_consumer_list_data()
    assert caught.value.backend == "postgresql"
    assert "SQLite-only" in str(caught.value) and "dry-run" in str(caught.value)
    assert "postgresql" in str(caught.value)
    with pytest.raises(ConsumerInspectionBackendError):
        await preview_consumer_change(FakePostgres(), "consumer", action="retire")


def test_non_sqlite_refusal_is_visible_in_human_and_machine_modes(monkeypatch, capsys):
    from lionagi.cli.machine import MachineError

    async def fake_data():
        raise ConsumerInspectionBackendError("postgresql")

    errors = []
    monkeypatch.setattr(state_cli, "_terminal_consumer_list_data", fake_data)
    monkeypatch.setattr("lionagi.cli._logging.log_error", errors.append)
    args = _parser().parse_args(["state", "terminal-consumer", "list"])
    assert state_cli.run_state(args) == 1
    assert capsys.readouterr().out == ""
    assert "postgresql" in errors[0] and "dry-run" in errors[0]
    with pytest.raises(MachineError) as caught:
        state_cli.machine_result(["terminal-consumer", "list"])
    assert caught.value.kind == "unavailable"
    assert "postgresql" in str(caught.value) and "SQLite-only" in str(caught.value)
