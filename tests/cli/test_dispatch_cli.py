# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li dispatch` — direct-DB read/ack, no daemon required (ADR-0092)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.cli.main import main


def _redirect_state_db(monkeypatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_dispatch(db_path: Path, **kwargs) -> str:
    from lionagi.dispatch import enqueue_dispatch
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        return await enqueue_dispatch(db, **kwargs)


def test_dispatch_ls_empty(monkeypatch, tmp_path, capsys):
    _redirect_state_db(monkeypatch, tmp_path)
    rc = main(["dispatch", "ls"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no dispatches" in captured.out


def test_dispatch_ls_lists_rows(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    asyncio.run(_seed_dispatch(db_path, kind="terminal_notify", deliver_to="seat-1"))

    rc = main(["dispatch", "ls"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "terminal_notify" in captured.out
    assert "seat-1" in captured.out


def test_dispatch_show_prints_payload(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    dispatch_id = asyncio.run(
        _seed_dispatch(db_path, kind="terminal_notify", deliver_to="seat-1", body={"a": 1})
    )

    rc = main(["dispatch", "show", dispatch_id])
    captured = capsys.readouterr()
    assert rc == 0
    assert dispatch_id in captured.out
    assert '"a": 1' in captured.out


def test_dispatch_show_missing_id(monkeypatch, tmp_path, capsys):
    _redirect_state_db(monkeypatch, tmp_path)
    rc = main(["dispatch", "show", "does-not-exist"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.out


def test_dispatch_ack_no_daemon_running(monkeypatch, tmp_path, capsys):
    """`li dispatch ack` is a direct-DB write; it works with no scheduler daemon involved."""
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    from lionagi.dispatch import get_dispatch
    from lionagi.state.db import StateDB

    dispatch_id = asyncio.run(
        _seed_dispatch(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )

    async def _read_token():
        async with StateDB(db_path) as db:
            row = await get_dispatch(db, dispatch_id)
            return row["ack_token"]

    token = asyncio.run(_read_token())

    rc = main(["dispatch", "ack", dispatch_id, token])
    captured = capsys.readouterr()
    assert rc == 0
    assert "acked" in captured.out


def test_dispatch_ack_wrong_token(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    dispatch_id = asyncio.run(
        _seed_dispatch(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )

    with pytest.raises(ValueError, match="ack_token mismatch"):
        main(["dispatch", "ack", dispatch_id, "wrong"])


def test_dispatch_purge_removes_row(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    dispatch_id = asyncio.run(_seed_dispatch(db_path, kind="terminal_notify", deliver_to="seat-1"))

    rc = main(["dispatch", "purge", dispatch_id])
    captured = capsys.readouterr()
    assert rc == 0
    assert "purged" in captured.out

    rc = main(["dispatch", "purge", dispatch_id])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.out


def test_dispatch_purge_bare_invocation_requires_criteria(monkeypatch, tmp_path, capsys):
    """A bare `li dispatch purge` (no id, no criteria) must not mass-delete."""
    _redirect_state_db(monkeypatch, tmp_path)

    rc = main(["dispatch", "purge"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "id" in captured.out or "status" in captured.out


def test_dispatch_purge_bulk_by_status(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    from lionagi.state.db import StateDB

    async def seed():
        async with StateDB(db_path) as db:
            from sqlalchemy import text

            from lionagi.dispatch import enqueue_dispatch

            delivered_id = await enqueue_dispatch(db, kind="k", deliver_to="seat-1")
            pending_id = await enqueue_dispatch(db, kind="k", deliver_to="seat-2")
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE dispatch_outbox SET status = 'delivered' WHERE id = :id"),
                    {"id": delivered_id},
                )
            return delivered_id, pending_id

    delivered_id, pending_id = asyncio.run(seed())

    rc = main(["dispatch", "purge", "--status", "delivered"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "purged 1 dispatch" in captured.out
    assert "delivered=1" in captured.out

    async def check():
        from lionagi.dispatch import get_dispatch

        async with StateDB(db_path) as db:
            return await get_dispatch(db, delivered_id), await get_dispatch(db, pending_id)

    remaining_delivered, remaining_pending = asyncio.run(check())
    assert remaining_delivered is None
    assert remaining_pending is not None


def test_dispatch_purge_bulk_dry_run_deletes_nothing(monkeypatch, tmp_path, capsys):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    from lionagi.state.db import StateDB

    async def seed():
        async with StateDB(db_path) as db:
            from sqlalchemy import text

            from lionagi.dispatch import enqueue_dispatch

            dispatch_id = await enqueue_dispatch(db, kind="k", deliver_to="seat-1")
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE dispatch_outbox SET status = 'delivered' WHERE id = :id"),
                    {"id": dispatch_id},
                )
            return dispatch_id

    dispatch_id = asyncio.run(seed())

    rc = main(["dispatch", "purge", "--status", "delivered", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "would purge 1 dispatch" in captured.out

    async def check():
        from lionagi.dispatch import get_dispatch

        async with StateDB(db_path) as db:
            return await get_dispatch(db, dispatch_id)

    assert asyncio.run(check()) is not None


def test_dispatch_purge_single_row_honors_dry_run(monkeypatch, tmp_path, capsys):
    """`purge <id> --dry-run` previews the row and must not delete it."""
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio

    from lionagi.state.db import StateDB

    async def seed():
        async with StateDB(db_path) as db:
            from lionagi.dispatch import enqueue_dispatch

            return await enqueue_dispatch(db, kind="k", deliver_to="seat-1")

    dispatch_id = asyncio.run(seed())

    rc = main(["dispatch", "purge", dispatch_id, "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert f"would purge {dispatch_id}" in captured.out
    assert "status=pending" in captured.out

    async def check():
        from lionagi.dispatch import get_dispatch

        async with StateDB(db_path) as db:
            return await get_dispatch(db, dispatch_id)

    assert asyncio.run(check()) is not None


def test_dispatch_purge_bare_before_leaves_pending_row_alone(monkeypatch, tmp_path, capsys):
    """A bare --before (no --status) defaults to terminal statuses only."""
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    import asyncio
    import time

    from lionagi.state.db import StateDB

    async def seed():
        async with StateDB(db_path) as db:
            from sqlalchemy import text

            from lionagi.dispatch import enqueue_dispatch

            old_ts = time.time() - 100_000
            pending_id = await enqueue_dispatch(db, kind="k", deliver_to="seat-1")
            delivered_id = await enqueue_dispatch(db, kind="k", deliver_to="seat-2")
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE dispatch_outbox SET updated_at = :ts WHERE id = :id"),
                    {"ts": old_ts, "id": pending_id},
                )
                await conn.execute(
                    text(
                        "UPDATE dispatch_outbox SET status = 'delivered', updated_at = :ts"
                        " WHERE id = :id"
                    ),
                    {"ts": old_ts, "id": delivered_id},
                )
            return pending_id, delivered_id

    pending_id, delivered_id = asyncio.run(seed())

    rc = main(["dispatch", "purge", "--before", str(time.time() - 50_000)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "purged 1 dispatch" in captured.out
    assert "delivered=1" in captured.out

    async def check():
        from lionagi.dispatch import get_dispatch

        async with StateDB(db_path) as db:
            return await get_dispatch(db, pending_id), await get_dispatch(db, delivered_id)

    remaining_pending, remaining_delivered = asyncio.run(check())
    assert remaining_pending is not None
    assert remaining_delivered is None
