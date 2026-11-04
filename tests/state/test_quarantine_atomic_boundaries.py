"""Quarantine normalization CAS and PostgreSQL numeric boundaries."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi.state.db import StateDB


class _NoRow:
    def scalar_one_or_none(self):
        return None


@pytest.mark.parametrize("change", ["unrelated", "identity", "epoch", "persistent"])
async def test_normalization_retries_without_clobbering_or_bypassing_guards(
    tmp_path, monkeypatch, change
):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("p")
        await db.create_session(
            {"id": "s", "progression_id": "p", "status": "running", "node_metadata": {"pid": 123}}
        )
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :raw WHERE id = 's'"),
                {"raw": '{"pid":123,"unverifiable_since":NaN}'},
            )
        snapshot = await db.get_session("s")
        original_execute = AsyncConnection.execute
        calls = 0

        async def conflict_once(conn, statement, *args, **kwargs):
            nonlocal calls
            if str(statement).startswith("UPDATE sessions SET node_metadata ="):
                calls += 1
                if calls == 1 or change == "persistent":
                    replacement = {"pid": 123, "tag": "concurrent update"}
                    if change == "identity":
                        replacement["pid"] = 124
                    if change == "epoch":
                        replacement["unverifiable_epoch"] = 1
                    await original_execute(
                        conn,
                        text("UPDATE sessions SET node_metadata = :raw WHERE id = 's'"),
                        {"raw": json.dumps(replacement)},
                    )
                    return _NoRow()
            return await original_execute(conn, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncConnection, "execute", conflict_once)
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=snapshot, pid=123, create_time=None, observed_at=10
        )
        metadata = (await db.get_session("s"))["node_metadata"]
        assert metadata["tag"] == "concurrent update"
        if change == "unrelated":
            assert result["applied"] and metadata["unverifiable_count"] == 1
            assert calls == 2
        else:
            assert not result["applied"]
            assert "unverifiable_count" not in metadata
            assert calls == 3


async def test_postgres_sql_checks_full_19_digit_bounds(tmp_path, monkeypatch):
    """Capture the generated PostgreSQL statement even without a local server."""
    db = StateDB(tmp_path / "unused.db")
    db.dialect = "postgresql"
    statements = []

    class Result:
        def scalar_one_or_none(self):
            return {"applied": True}

    class Connection:
        async def execute(self, statement, params):
            statements.append(str(statement))
            return Result()

    @asynccontextmanager
    async def tx():
        yield Connection()

    monkeypatch.setattr(db, "_tx", tx)
    await db._update_unverifiable_pid(
        "session",
        "s",
        snapshot={"node_metadata": {"pid": 123}},
        pid=123,
        create_time=None,
        observed_at=10,
        clear=False,
    )
    sql = statements[0]
    assert "^[0-9]{1,19}$" in sql
    assert "^[1-9][0-9]{0,18}$" in sql
    assert "::numeric < 9223372036854775806" in sql
    assert "^[0-9]{1,18}$" not in sql


@pytest.mark.skipif(
    not os.environ.get("LIONAGI_TEST_PG_URL"),
    reason="no PostgreSQL service configured via LIONAGI_TEST_PG_URL",
)
@pytest.mark.integration
@pytest.mark.parametrize("epoch", [999999999999999999, 1000000000000000000])
async def test_postgres_epoch_and_count_19_digit_boundaries(pg_url, epoch):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(url=pg_url) as db:
        await db.create_progression(progression)
        identity = json.dumps([123, None, None], separators=(",", ":"))
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "node_metadata": {
                    "pid": 123,
                    "unverifiable_identity": identity,
                    "unverifiable_count": 1000000000000000000,
                    "unverifiable_since": 10,
                    "unverifiable_quarantined": True,
                    "unverifiable_epoch": epoch,
                },
            }
        )
        snapshot = await db.get_session(sid)
        observed = await db.observe_unverifiable_pid(
            "session", sid, snapshot=snapshot, pid=123, create_time=None, observed_at=11
        )
        assert observed["applied"]
        assert observed["unverifiable_count"] == 1000000000000000001
        assert observed["unverifiable_quarantined"] is True
        current = await db.get_session(sid)
        cleared = await db.clear_unverifiable_pid("session", sid, snapshot=current)
        assert cleared["applied"] and cleared["unverifiable_epoch"] == epoch + 1
        current = await db.get_session(sid)
        assert (
            await db.observe_unverifiable_pid(
                "session", sid, snapshot=current, pid=123, create_time=None, observed_at=12
            )
        )["applied"]
