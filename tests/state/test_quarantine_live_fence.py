"""A verified-live observation fences older concurrent unknown probes."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB


async def _create_running(db, kind):
    metadata = {"pid": 123, "other": {"keep": ["value"]}}
    if kind == "session":
        await db.create_progression("p")
        await db.create_session(
            {"id": "s", "progression_id": "p", "status": "running", "node_metadata": metadata}
        )
        return "s", "sessions"
    await db.create_invocation(
        {"id": "i", "skill": "s", "started_at": 1.0, "node_metadata": metadata}
    )
    return "i", "invocations"


async def _get(db, kind, entity_id):
    if kind == "session":
        return await db.get_session(entity_id)
    return await db.get_invocation(entity_id)


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_verified_live_fences_three_pending_unknown_probes(tmp_path, kind):
    path = tmp_path / "state.db"
    async with (
        StateDB(path) as live,
        StateDB(path) as first,
        StateDB(path) as second,
        StateDB(path) as third,
    ):
        entity_id, _ = await _create_running(live, kind)
        snapshot = await _get(live, kind, entity_id)
        ready = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event()

        async def finish_unknown(db, index):
            ready[index].set()
            await release.wait()
            return await db.observe_unverifiable_pid(
                kind,
                entity_id,
                snapshot=snapshot,
                pid=123,
                create_time=None,
                observed_at=10 + index,
            )

        pending = [
            asyncio.create_task(finish_unknown(db, index))
            for index, db in enumerate((first, second, third))
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), timeout=5)
            cleared = await live.clear_unverifiable_pid(kind, entity_id, snapshot=snapshot)
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*pending), timeout=10)

        assert cleared["applied"] is True
        assert [result["applied"] for result in results] == [False, False, False]
        row = await _get(live, kind, entity_id)
        metadata = row["node_metadata"]
        assert metadata["unverifiable_epoch"] == 1
        assert "unverifiable_count" not in metadata
        assert "unverifiable_quarantined" not in metadata
        assert metadata["other"] == {"keep": ["value"]}

        fresh = await live.observe_unverifiable_pid(
            kind, entity_id, snapshot=row, pid=123, create_time=None, observed_at=42.5
        )
        assert fresh["applied"] is True
        assert fresh["unverifiable_count"] == 1
        assert fresh["unverifiable_since"] == 42.5
        assert fresh["unverifiable_quarantined"] is False


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_healthy_live_fence_updates_timestamp_and_preserves_metadata(tmp_path, kind):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, table = await _create_running(db, kind)
        async with db._tx() as conn:
            await conn.execute(
                text(f"UPDATE {table} SET updated_at = 1 WHERE id = :id"), {"id": entity_id}
            )
        snapshot = await _get(db, kind, entity_id)
        assert snapshot["updated_at"] == 1
        cleared = await db.clear_unverifiable_pid(kind, entity_id, snapshot=snapshot)
        assert cleared["applied"] is True
        row = await _get(db, kind, entity_id)
        assert row["updated_at"] > snapshot["updated_at"]
        assert row["node_metadata"] == {
            "pid": 123,
            "other": {"keep": ["value"]},
            "unverifiable_epoch": 1,
        }
