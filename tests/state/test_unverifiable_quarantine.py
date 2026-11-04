"""Atomic unverifiable-PID epoch accounting."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB


async def _session(db, metadata=None):
    await db.create_progression("p")
    await db.create_session(
        {
            "id": "s",
            "progression_id": "p",
            "status": "running",
            "node_metadata": metadata or {"pid": 123, "pid_create_time": 12.0},
        }
    )
    return await db.get_session("s")


async def test_atomic_observations_race_and_live_reset_refuses_old_probe(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        row = await _session(first)
        results = await asyncio.gather(
            first.observe_unverifiable_pid(
                "session", "s", snapshot=row, pid=123, create_time=12.0, observed_at=10
            ),
            second.observe_unverifiable_pid(
                "session", "s", snapshot=row, pid=123, create_time=12.0, observed_at=11
            ),
        )
        assert all(result["applied"] for result in results)
        row = await first.get_session("s")
        assert row["node_metadata"]["unverifiable_count"] == 2
        assert row["node_metadata"]["unverifiable_quarantined"] is False
        old = row
        assert (await first.clear_unverifiable_pid("session", "s", snapshot=row))["applied"]
        stale = await second.observe_unverifiable_pid(
            "session", "s", snapshot=old, pid=123, create_time=12.0, observed_at=12
        )
        assert not stale["applied"]
        row = await first.get_session("s")
        result = await first.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=12.0, observed_at=13
        )
        assert result["unverifiable_count"] == 1
        assert result["unverifiable_since"] == 13


async def test_observation_refuses_changed_identity(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        row = await _session(db)
        await db.merge_session_node_metadata("s", {"pid": 124})
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=12.0, observed_at=10
        )
        assert result["applied"] is False
        assert "unverifiable_count" not in (await db.get_session("s"))["node_metadata"]


async def test_threshold_and_racing_sweeps_preserve_unrelated_metadata(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        row = await _session(first, {"pid": 123, "other": {"keep": True}})
        initial = await first.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=None, observed_at=10
        )
        assert initial["unverifiable_count"] == 1
        row = await first.get_session("s")
        results = await asyncio.gather(
            first.observe_unverifiable_pid(
                "session", "s", snapshot=row, pid=123, create_time=None, observed_at=11
            ),
            second.observe_unverifiable_pid(
                "session", "s", snapshot=row, pid=123, create_time=None, observed_at=12
            ),
        )
        assert all(result["applied"] for result in results)
        state = (await first.get_session("s"))["node_metadata"]
        assert state["unverifiable_count"] == 3
        assert state["unverifiable_since"] == 10
        assert state["unverifiable_quarantined"] is True
        assert state["other"] == {"keep": True}


async def test_fallback_pid_change_fences_old_observation(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        row = await _session(db, {"unrelated": "keep"})
        await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=None, observed_at=10
        )
        row = await db.get_session("s")
        replacement = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=124, create_time=None, observed_at=11
        )
        assert replacement["unverifiable_count"] == 1
        stale = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=None, observed_at=12
        )
        assert not stale["applied"]


async def test_session_generation_change_fences_old_observation(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        row = await _session(db)
        await db.update_session("s", cli_worker_generation=str(uuid.uuid4()))
        stale = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=12.0, observed_at=10
        )
        assert not stale["applied"]


async def test_invocation_observations_use_same_atomic_rules(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_invocation(
            {"id": "i", "skill": "s", "started_at": 1.0, "node_metadata": {"pid": 42}}
        )
        row = await db.get_invocation("i")
        for number in range(3):
            result = await db.observe_unverifiable_pid(
                "invocation", "i", snapshot=row, pid=42, create_time=None, observed_at=10 + number
            )
            assert result["unverifiable_count"] == number + 1
            row = await db.get_invocation("i")
        assert row["node_metadata"]["unverifiable_quarantined"] is True


async def test_postgres_two_connections_increment_atomically(pg_url):
    sid = str(uuid.uuid4())
    progression = str(uuid.uuid4())
    async with StateDB(url=pg_url) as first, StateDB(url=pg_url) as second:
        await first.create_progression(progression)
        await first.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "node_metadata": {"pid": 123},
            }
        )
        row = await first.get_session(sid)
        first_result, second_result = await asyncio.gather(
            first.observe_unverifiable_pid(
                "session", sid, snapshot=row, pid=123, create_time=None, observed_at=10
            ),
            second.observe_unverifiable_pid(
                "session", sid, snapshot=row, pid=123, create_time=None, observed_at=11
            ),
        )
        assert first_result["applied"] and second_result["applied"]
        row = await first.get_session(sid)
        assert row["node_metadata"]["unverifiable_count"] == 2


async def test_malformed_stored_json_does_not_abort_the_sweep(tmp_path, caplog):
    async with StateDB(tmp_path / "state.db") as db:
        await _session(db)
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :bad WHERE id = :id"),
                {"bad": "{broken", "id": "s"},
            )
        row = await db.get_session("s")
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=None, observed_at=10
        )
        assert result["applied"] is False
        assert (await db.get_session("s"))["node_metadata"] == "{broken"
        assert "unreadable" in caplog.text


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e309"])
async def test_nonfinite_history_preserves_pid_and_unrelated_metadata(tmp_path, literal, caplog):
    async with StateDB(tmp_path / "state.db") as db:
        await _session(db)
        raw = '{"pid":123,"tag":{"keep":"yes"},"unverifiable_since":' + literal + "}"
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :raw WHERE id = :id"),
                {"raw": raw, "id": "s"},
            )
        snapshot = await db.get_session("s")
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=snapshot, pid=123, create_time=None, observed_at=10
        )
        assert result["applied"] and result["unverifiable_count"] == 1
        assert result["pid"] == 123 and result["tag"] == {"keep": "yes"}
        assert result["unverifiable_since"] == 10
        assert "malformed" in caplog.text


async def test_nonfinite_history_racing_sweeps_preserve_unrelated_changes(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        await _session(first)
        async with first._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :raw WHERE id = :id"),
                {"raw": '{"pid":123,"tag":"old","unverifiable_since":NaN}', "id": "s"},
            )
        snapshot = await first.get_session("s")
        # An unrelated writer wins before the observations commit.
        async with second._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :raw WHERE id = :id"),
                {"raw": '{"pid":123,"tag":"new","unverifiable_since":NaN}', "id": "s"},
            )
        a, b = await asyncio.gather(
            first.observe_unverifiable_pid(
                "session", "s", snapshot=snapshot, pid=123, create_time=None, observed_at=10
            ),
            second.observe_unverifiable_pid(
                "session", "s", snapshot=snapshot, pid=123, create_time=None, observed_at=11
            ),
        )
        assert a["applied"] and b["applied"]
        row = await first.get_session("s")
        assert row["node_metadata"]["unverifiable_count"] == 2
        assert row["node_metadata"]["tag"] == "new"
        assert row["node_metadata"]["pid"] == 123


async def test_nonfinite_unrelated_metadata_is_never_destroyed(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await _session(db)
        raw = '{"pid":123,"other":NaN,"unverifiable_since":NaN}'
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET node_metadata = :raw WHERE id = :id"),
                {"raw": raw, "id": "s"},
            )
        snapshot = await db.get_session("s")
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=snapshot, pid=123, create_time=None, observed_at=10
        )
        assert not result["applied"]
        async with db._read() as conn:
            current = (
                await conn.execute(text("SELECT node_metadata FROM sessions WHERE id = 's'"))
            ).scalar_one()
        assert current == raw


@pytest.mark.parametrize(
    "history",
    [
        {"unverifiable_count": "bad"},
        {"unverifiable_since": -1},
        {"unverifiable_since": 10**400},
        {"unverifiable_quarantined": "yes"},
    ],
)
async def test_malformed_history_starts_fresh(tmp_path, history, caplog):
    async with StateDB(tmp_path / "state.db") as db:
        row = await _session(db, {"pid": 123, **history})
        result = await db.observe_unverifiable_pid(
            "session", "s", snapshot=row, pid=123, create_time=None, observed_at=10
        )
        assert result["applied"] and result["unverifiable_count"] == 1
        assert "malformed" in caplog.text
