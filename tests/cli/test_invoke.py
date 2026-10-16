# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import logging
from unittest.mock import AsyncMock

import pytest

from lionagi.cli.invoke import InvocationEnd, _end_invocation, run_invoke
from lionagi.state.reasons import RunReasons


def _error_records():
    """Collect `log_error()` output without depending on propagation, which
    `configure_cli_logging` disables process-wide."""
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("lionagi.cli.error")
    handler = _Collect()
    logger.addHandler(handler)
    return records, logger, handler


def _end_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        invoke_command="end",
        invocation_id="invocation-1",
        status="completed",
        metadata=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


async def test_end_invocation_writes_terminal_status_and_ended_at_through_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {"id": "invocation-1", "status": "running", "node_metadata": {"kept": True}}
    updated = {**existing, "status": "completed", "ended_at": 123.0}

    class FakeStateDB:
        instance = None

        def __init__(self):
            self.get_invocation = AsyncMock(side_effect=[existing, updated])
            self.update_invocation = AsyncMock()
            self.update_status = AsyncMock(return_value=True)
            type(self).instance = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("lionagi.state.db.StateDB", FakeStateDB)
    monkeypatch.setattr("lionagi.cli.invoke.time.time", lambda: 123.0)

    result = await _end_invocation(
        "invocation-1",
        status="completed",
        metadata={"added": "value"},
    )

    db = FakeStateDB.instance
    db.update_invocation.assert_awaited_once_with(
        "invocation-1", node_metadata={"kept": True, "added": "value"}
    )
    db.update_status.assert_awaited_once_with(
        "invocation",
        "invocation-1",
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_summary="Invocation completed.",
        source="executor",
        actor="invocation-1",
        expected_statuses={"running"},
        extra_fields={"ended_at": 123.0},
    )
    assert result.applied is True
    assert result.row == updated


async def test_end_invocation_reports_a_refused_guard_and_keeps_metadata_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused guarded end must say so and must not merge the metadata it
    could not record alongside a status write it never made."""
    stored = {"id": "invocation-1", "status": "cancelled", "node_metadata": {"kept": True}}

    class FakeStateDB:
        instance = None

        def __init__(self):
            self.get_invocation = AsyncMock(return_value=stored)
            self.update_invocation = AsyncMock()
            self.update_status = AsyncMock(return_value=False)
            type(self).instance = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("lionagi.state.db.StateDB", FakeStateDB)

    result = await _end_invocation("invocation-1", status="completed", metadata={"late": True})

    assert result.applied is False
    assert result.row == stored
    FakeStateDB.instance.update_invocation.assert_not_awaited()


def test_run_invoke_end_exits_non_zero_and_reports_a_refused_end(monkeypatch) -> None:
    """A refused end leaves the invocation and its metadata as another writer
    recorded them; the command must report that instead of exiting 0."""

    async def refused(invocation_id, *, status, metadata):
        return InvocationEnd(row={"status": "cancelled", "session_count": 2}, applied=False)

    monkeypatch.setattr("lionagi.cli.invoke._end_invocation", refused)
    records, logger, handler = _error_records()
    try:
        rc = run_invoke(_end_args(metadata='{"late": true}'))
    finally:
        logger.removeHandler(handler)

    assert rc == 1
    message = " ".join(records)
    assert "invocation-1" in message
    assert "'cancelled'" in message
    assert "--metadata" in message
    assert "not recorded" in message


def test_run_invoke_end_prints_the_closed_status_and_exits_zero(monkeypatch, capsys) -> None:
    async def applied(invocation_id, *, status, metadata):
        return InvocationEnd(row={"status": "completed", "session_count": 1}, applied=True)

    monkeypatch.setattr("lionagi.cli.invoke._end_invocation", applied)

    rc = run_invoke(_end_args())

    assert rc == 0
    assert "invocation-1: completed (1 session(s))" in capsys.readouterr().out


def test_run_invoke_end_reports_a_refused_end_on_the_real_store(tmp_path, monkeypatch) -> None:
    """Ending an invocation another writer already closed must not read as a
    successful close: the status and --metadata this call carried are not
    recorded, and the command has to say so instead of exiting 0."""
    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "closed",
                    "skill": "example",
                    "started_at": 100.0,
                    "status": "cancelled",
                    "node_metadata": {"kept": True},
                }
            )

    import asyncio

    asyncio.run(seed())

    records, logger, handler = _error_records()
    try:
        rc = run_invoke(_end_args(invocation_id="closed", metadata='{"late": true}'))
    finally:
        logger.removeHandler(handler)

    assert rc != 0, "a refused end must not exit 0"
    message = " ".join(records)
    assert "closed" in message
    assert "--metadata" in message


def test_run_invoke_end_rejects_an_already_terminal_row_without_rewriting_it(
    tmp_path, monkeypatch
) -> None:
    """The stored row and metadata survive a refused end untouched."""
    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "closed",
                    "skill": "example",
                    "started_at": 100.0,
                    "status": "failed",
                    "node_metadata": {"kept": True},
                }
            )

    import asyncio

    asyncio.run(seed())

    records, logger, handler = _error_records()
    try:
        run_invoke(_end_args(invocation_id="closed", metadata='{"late": true}'))
    finally:
        logger.removeHandler(handler)

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("closed")

    row = asyncio.run(read())
    assert row["status"] == "failed"
    assert row["node_metadata"] == {"kept": True}


def test_run_invoke_end_records_metadata_on_a_same_status_close(tmp_path, monkeypatch) -> None:
    """Ending an invocation that is already in the requested terminal status
    still saves the --metadata, without moving the stored end time: the status
    write is refused, but the closer's payload is merged onto the row it names."""
    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "closed",
                    "skill": "example",
                    "started_at": 100.0,
                    "ended_at": 200.0,
                    "status": "completed",
                    "node_metadata": {"kept": True},
                }
            )

    import asyncio

    asyncio.run(seed())

    rc = run_invoke(
        _end_args(invocation_id="closed", status="completed", metadata='{"result": "pr-url"}')
    )

    assert rc == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("closed")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["ended_at"] == 200.0
    assert row["node_metadata"] == {"kept": True, "result": "pr-url"}


def test_run_invoke_end_still_refuses_a_different_terminal_status(tmp_path, monkeypatch) -> None:
    """A close naming a different terminal status than the stored one is still
    refused: it does not overwrite the row another writer closed, and it does
    not merge the metadata it could not record."""
    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "closed",
                    "skill": "example",
                    "started_at": 100.0,
                    "ended_at": 200.0,
                    "status": "cancelled",
                    "node_metadata": {"kept": True},
                }
            )

    import asyncio

    asyncio.run(seed())

    records, logger, handler = _error_records()
    try:
        rc = run_invoke(
            _end_args(invocation_id="closed", status="failed", metadata='{"late": true}')
        )
    finally:
        logger.removeHandler(handler)

    assert rc == 1
    message = " ".join(records)
    assert "'cancelled'" in message
    assert "--metadata" in message

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("closed")

    row = asyncio.run(read())
    assert row["status"] == "cancelled"
    assert row["ended_at"] == 200.0
    assert row["node_metadata"] == {"kept": True}
