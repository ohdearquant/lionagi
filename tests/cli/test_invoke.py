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
            self.merge_invocation_node_metadata = AsyncMock()
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
    db.merge_invocation_node_metadata.assert_awaited_once_with("invocation-1", {"added": "value"})
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


def test_run_invoke_end_same_status_without_metadata_exits_zero(tmp_path, monkeypatch) -> None:
    """A same-status close with no ``--metadata`` has nothing left to record, so
    it reports the stored status and exits 0 rather than refusing."""
    import asyncio

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

    asyncio.run(seed())

    rc = run_invoke(_end_args(invocation_id="closed", status="completed", metadata=None))
    assert rc == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("closed")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["ended_at"] == 200.0
    assert row["node_metadata"] == {"kept": True}


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


def test_same_status_close_merges_past_a_racing_closer(tmp_path, monkeypatch) -> None:
    """A same-status close must not drop a payload another closer landed first.

    The row is already ``completed``, so the close takes the same-status path
    and merges ``{"b": 2}``. Between that close's read of the row and its write,
    another closer lands ``{"a": 1}`` -- the window a whole-column
    read-modify-write loses to. The merge runs inside the store's own UPDATE, so
    the racing payload is merged with instead of overwritten and both keys
    survive.
    """
    import asyncio

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "race",
                    "skill": "example",
                    "started_at": 100.0,
                    "ended_at": 200.0,
                    "status": "completed",
                    "node_metadata": {"kept": True},
                }
            )

    asyncio.run(seed())

    real_merge = StateDB.merge_invocation_node_metadata
    injected = False

    async def merge_with_a_racing_landing(db, invocation_id, patch):
        nonlocal injected
        if not injected:
            injected = True
            # The other closer's payload lands here, after this close read the
            # row and before its own merge runs.
            async with StateDB(db_path) as other:
                await other.merge_invocation_node_metadata(invocation_id, {"a": 1})
        return await real_merge(db, invocation_id, patch)

    monkeypatch.setattr(
        StateDB,
        "merge_invocation_node_metadata",
        merge_with_a_racing_landing,
    )

    result = asyncio.run(_end_invocation("race", status="completed", metadata={"b": 2}))
    assert injected
    assert result.applied is True

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("race")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["ended_at"] == 200.0
    assert row["node_metadata"] == {"kept": True, "a": 1, "b": 2}


def test_run_invoke_end_merges_metadata_a_start_wrote_as_escapes(tmp_path, monkeypatch) -> None:
    """A close records its ``--metadata`` on a row whose stored text spells a
    value with ``\\uXXXX`` escapes.

    ``li invoke start --metadata`` binds through the store's JSON column, which
    escapes a non-ASCII character, while the close's merge reads the value back
    as text. A close that compares stored text instead of merging in the
    database never matches this row and reports the payload lost after the
    status already landed.
    """
    import asyncio

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    start_args = argparse.Namespace(
        invoke_command="start",
        skill="example",
        plugin=None,
        prompt=None,
        metadata='{"summary": "caf\u00e9"}',
    )
    assert run_invoke(start_args) == 0

    async def read_id():
        async with StateDB(db_path) as db:
            rows = await db.list_invocations(skill="example", limit=1)
            return rows[0]["id"]

    invocation_id = asyncio.run(read_id())

    rc = run_invoke(
        _end_args(invocation_id=invocation_id, status="completed", metadata='{"result": "pr-url"}')
    )
    assert rc == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation(invocation_id)

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["node_metadata"] == {"summary": "caf\u00e9", "result": "pr-url"}


def test_run_invoke_end_keeps_a_stored_scalar_metadata_under_discarded(
    tmp_path, monkeypatch
) -> None:
    """A close merges its ``--metadata`` onto a row whose stored value is a JSON
    string, not an object.

    The stored scalar has nowhere to merge into, so the store keeps it verbatim
    under ``_discarded_node_metadata`` and the close still records its payload.
    A close that compares the decoded string against the value it read treats
    the row as unmoved and reports the payload lost.
    """
    import asyncio

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "legacy",
                    "skill": "example",
                    "started_at": 100.0,
                    "ended_at": 200.0,
                    "status": "completed",
                    "node_metadata": "legacy-tag",
                }
            )

    asyncio.run(seed())

    rc = run_invoke(
        _end_args(invocation_id="legacy", status="completed", metadata='{"result": "pr-url"}')
    )
    assert rc == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("legacy")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    discarded = row["node_metadata"].pop("_discarded_node_metadata")
    assert discarded == "legacy-tag"
    assert isinstance(row["node_metadata"].pop("_discarded_at"), float)
    assert row["node_metadata"] == {"result": "pr-url"}


def test_run_invoke_end_two_same_status_closes_keep_both_keys(tmp_path, monkeypatch) -> None:
    """Two closes of one already-terminal invocation each record their own key.

    Neither close moves the stored status or end time, so both take the
    same-status path; the second must merge onto the first's payload rather than
    replace it.
    """
    import asyncio

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

    asyncio.run(seed())

    # The first close stores a non-ASCII value, which a text comparison against
    # the decoded value it reads back never matches for the second close.
    assert (
        run_invoke(
            _end_args(invocation_id="closed", status="completed", metadata='{"note": "caf\u00e9"}')
        )
        == 0
    )
    assert (
        run_invoke(
            _end_args(invocation_id="closed", status="completed", metadata='{"result": "pr-url"}')
        )
        == 0
    )

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("closed")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["ended_at"] == 200.0
    assert row["node_metadata"] == {"kept": True, "note": "caf\u00e9", "result": "pr-url"}


def test_run_invoke_end_records_metadata_when_the_row_has_no_node_metadata(
    tmp_path, monkeypatch
) -> None:
    """A row recorded without ``node_metadata`` stores JSON null, which reads
    back as ``None``. The close still saves its payload: the merge starts from
    an object, so an absent value is merged into rather than reported lost."""
    import asyncio

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def seed():
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "fresh",
                    "skill": "example",
                    "started_at": 100.0,
                    "status": "running",
                }
            )

    asyncio.run(seed())

    rc = run_invoke(
        _end_args(invocation_id="fresh", status="completed", metadata='{"result": "pr-url"}')
    )
    assert rc == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_invocation("fresh")

    row = asyncio.run(read())
    assert row["status"] == "completed"
    assert row["node_metadata"] == {"result": "pr-url"}


def test_run_invoke_end_same_status_close_merges_onto_a_row_without_node_metadata(
    tmp_path, monkeypatch
) -> None:
    """The same-status close of a row recorded without ``node_metadata`` merges
    its payload onto the absent value instead of exiting 1 as if it were lost.

    The row is already ``completed``, so the close takes the same-status path
    and only merges; the merge treats JSON null as an absent object to start
    from."""
    import asyncio

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
                }
            )

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
    assert row["node_metadata"] == {"result": "pr-url"}
