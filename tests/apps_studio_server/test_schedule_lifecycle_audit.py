# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule lifecycle provenance contracts."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def _spec(name: str) -> dict[str, object]:
    return {
        "name": name,
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_prompt": "check the fleet",
    }


def test_disable_requires_reason_and_detail_preserves_disable_after_reenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    identity = {
        "x-lionagi-actor": "operator:alice",
        "x-lionagi-cwd": "/projects/monitoring",
    }

    created = client.post("/api/schedules/", json=_spec("audit-one"), headers=identity)
    assert created.status_code == 201
    schedule_id = created.json()["id"]

    missing_reason = client.post(f"/api/schedules/{schedule_id}/disable", headers=identity)
    assert missing_reason.status_code == 400
    assert "reason" in missing_reason.json()["detail"].lower()

    disabled = client.post(
        f"/api/schedules/{schedule_id}/disable",
        json={"reason": "Pause while rotating the provider credential"},
        headers=identity,
    )
    assert disabled.status_code == 200

    enabled = client.post(f"/api/schedules/{schedule_id}/enable", headers=identity)
    assert enabled.status_code == 200

    detail = client.get(f"/api/schedules/{schedule_id}").json()
    history = detail["lifecycle_history"]
    assert [row["status"] for row in history[:3]] == ["enabled", "disabled", "enabled"]
    disable_row = history[1]
    assert disable_row["actor"] == "operator"
    assert disable_row["metadata"]["claimed_actor_unverified"] == "operator:alice"
    assert disable_row["reason_summary"] == "Pause while rotating the provider credential"
    assert disable_row["metadata"]["request_cwd"] == "/projects/monitoring"
    assert disable_row["created_at"] > 0
    assert detail["last_lifecycle_change"] == history[0]


def test_sequential_disable_sweep_writes_one_row_per_schedule_carrying_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    schedule_ids = [
        client.post("/api/schedules/", json=_spec(f"sweep-{i}")).json()["id"] for i in range(3)
    ]

    for schedule_id in schedule_ids:
        response = client.post(
            f"/api/schedules/{schedule_id}/disable",
            json={"reason": "Incident sweep: stop noisy monitors"},
            headers={
                "x-lionagi-actor": "automation:safety-sweep",
                "x-lionagi-cwd": "/srv/sweep-worker",
            },
        )
        assert response.status_code == 200

    async def _rows() -> list[dict]:
        async with StateDB(readonly=True) as db:
            placeholders = ",".join(f":id{i}" for i in range(len(schedule_ids)))
            return await db.fetch_all(
                "SELECT id, entity_id, status, actor, reason_summary, created_at, metadata "
                "FROM status_transitions WHERE entity_type = 'schedule' "
                f"AND entity_id IN ({placeholders}) AND status = 'disabled' "
                "ORDER BY created_at, id",
                {f"id{i}": value for i, value in enumerate(schedule_ids)},
            )

    import asyncio

    rows = asyncio.run(_rows())
    assert [row["entity_id"] for row in rows] == schedule_ids
    assert len({row["id"] for row in rows}) == 3
    assert all(row["actor"] == "operator" for row in rows)
    assert all(
        json.loads(row["metadata"])["claimed_actor_unverified"] == "automation:safety-sweep"
        for row in rows
    )
    assert all(row["reason_summary"] == "Incident sweep: stop noisy monitors" for row in rows)
    assert all(json.loads(row["metadata"])["request_cwd"] == "/srv/sweep-worker" for row in rows)
    assert [row["created_at"] for row in rows] == sorted(row["created_at"] for row in rows)


async def test_legacy_schedule_has_empty_history_without_a_fabricated_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    schedule_id = uuid.uuid4().hex[:12]
    async with StateDB() as db:
        stmt, params = db._build_schedule_insert_stmt(  # noqa: SLF001 - migration-shape fixture
            {"id": schedule_id, **_spec("legacy-no-audit")}
        )
        async with db.transaction() as conn:
            await conn.execute(stmt, params)

    from lionagi.studio.services.schedules import get_schedule

    detail = await get_schedule(schedule_id)
    assert detail is not None
    assert detail["lifecycle_history"] == []
    assert detail["last_lifecycle_change"] is None


def test_delete_appends_a_durable_tombstone_before_removing_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    schedule_id = client.post("/api/schedules/", json=_spec("delete-audit")).json()["id"]

    deleted = client.delete(
        f"/api/schedules/{schedule_id}", headers={"x-lionagi-actor": "operator:bob"}
    )
    assert deleted.status_code == 200
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404

    async def _row() -> dict | None:
        async with StateDB(readonly=True) as db:
            return await db.fetch_one(
                "SELECT status, actor, reason_code, created_at, metadata FROM status_transitions "
                "WHERE entity_type = 'schedule' AND entity_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                {"id": schedule_id},
            )

    import asyncio

    tombstone = asyncio.run(_row())
    assert tombstone is not None
    assert tombstone["status"] == "deleted"
    assert tombstone["actor"] == "operator"
    assert json.loads(tombstone["metadata"])["claimed_actor_unverified"] == "operator:bob"
    assert tombstone["reason_code"] == "schedule.deleted.request"


def test_a_caller_supplied_name_never_reaches_the_actor_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actor column says only what the server can vouch for.

    Studio authenticates with one shared bearer token, or with none, so there
    is no per-caller principal and any authorized caller can send any
    X-Lionagi-Actor. Writing that name into the column a reader treats as "who
    acted" would let the ledger state as fact something nothing checked. The
    name is still worth keeping, so it travels in metadata under a key that
    says what it is.
    """
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    identity = {"x-lionagi-actor": "operator:carol", "x-lionagi-cwd": "/projects/ops"}

    schedule_id = client.post(
        "/api/schedules/", json=_spec("attribution-claimed"), headers=identity
    ).json()["id"]
    assert (
        client.post(
            f"/api/schedules/{schedule_id}/disable",
            json={"reason": "Stand down for the maintenance window"},
            headers=identity,
        ).status_code
        == 200
    )

    history = client.get(f"/api/schedules/{schedule_id}").json()["lifecycle_history"]
    disable_row, create_row = history[0], history[1]
    for row in (disable_row, create_row):
        assert row["actor"] == "operator"
        assert "operator:carol" not in row["actor"]
        assert row["metadata"]["claimed_actor_unverified"] == "operator:carol"


def test_the_actor_column_does_not_vary_with_the_name_the_caller_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers claiming different names produce the same actor.

    Stated as invariance rather than as a single expected string: if the column
    ever tracks the header again it separates these two rows, whatever value
    the reintroduced code happens to choose.
    """
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()

    def _disable_as(name: str, spec_name: str) -> dict:
        schedule_id = client.post(
            "/api/schedules/", json=_spec(spec_name), headers={"x-lionagi-actor": name}
        ).json()["id"]
        assert (
            client.post(
                f"/api/schedules/{schedule_id}/disable",
                json={"reason": "Stand down for the maintenance window"},
                headers={"x-lionagi-actor": name},
            ).status_code
            == 200
        )
        return client.get(f"/api/schedules/{schedule_id}").json()["lifecycle_history"][0]

    first = _disable_as("automation:sweeper", "invariance-one")
    second = _disable_as("operator:dave", "invariance-two")

    assert first["actor"] == second["actor"]
    assert first["metadata"]["claimed_actor_unverified"] == "automation:sweeper"
    assert second["metadata"]["claimed_actor_unverified"] == "operator:dave"


def test_a_request_that_claims_no_name_records_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with no actor header there is nothing to record as claimed.

    The metadata key has to discriminate. Written unconditionally it would
    present the server's own fallback as something a caller asserted, which is
    the opposite of what happened.
    """
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()

    schedule_id = client.post("/api/schedules/", json=_spec("attribution-default")).json()["id"]
    assert (
        client.post(
            f"/api/schedules/{schedule_id}/disable",
            json={"reason": "Stand down for the maintenance window"},
        ).status_code
        == 200
    )

    history = client.get(f"/api/schedules/{schedule_id}").json()["lifecycle_history"]
    assert history[0]["actor"] == "operator"
    assert "claimed_actor_unverified" not in history[0]["metadata"]


async def _append(db: StateDB, schedule_id: str, n: int) -> None:
    """Write *n* transitions through the one method every write path uses."""
    for i in range(n):
        async with db.transaction() as conn:
            await db._append_schedule_lifecycle_in_tx(  # noqa: SLF001 - the choke point under test
                conn,
                schedule_id=schedule_id,
                previous_status="enabled" if i % 2 else "disabled",
                status="disabled" if i % 2 else "enabled",
                reason_code="schedule.disabled.request" if i % 2 else "schedule.enabled.request",
                reason_summary=f"flip {i}",
                actor="tester",
                source="operator",
            )


async def test_history_stops_growing_at_its_bound_rather_than_at_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toggling is an operator action with no natural end, so the row count
    has to be bounded by policy. Every flip is written in the same tick here,
    which is also what makes this exercise the insertion-order tiebreaker:
    ``created_at`` alone cannot say which three are newest."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    monkeypatch.setattr(state_db_mod, "SCHEDULE_LIFECYCLE_HISTORY_LIMIT", 3)
    schedule_id = uuid.uuid4().hex[:12]

    async with StateDB() as db:
        stmt, params = db._build_schedule_insert_stmt(  # noqa: SLF001 - fixture
            {"id": schedule_id, **_spec("bounded-history")}
        )
        async with db.transaction() as conn:
            await conn.execute(stmt, params)
        await _append(db, schedule_id, 8)

    async with StateDB(readonly=True) as db:
        rows = await db.fetch_all(
            "SELECT reason_summary FROM status_transitions "
            "WHERE entity_type = 'schedule' AND entity_id = :id "
            "ORDER BY created_at DESC, rowid DESC",
            {"id": schedule_id},
        )
    assert [r["reason_summary"] for r in rows] == ["flip 7", "flip 6", "flip 5"]


async def test_the_trim_does_not_orphan_a_delivery_row_that_points_at_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``terminal_deliveries`` carries a foreign key into the table being
    trimmed. With enforcement on, deleting a referenced transition raises, so
    the failure this guards is a write path that works until something has
    acknowledged a transition."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    monkeypatch.setattr(state_db_mod, "SCHEDULE_LIFECYCLE_HISTORY_LIMIT", 2)
    schedule_id = uuid.uuid4().hex[:12]

    async with StateDB() as db:
        stmt, params = db._build_schedule_insert_stmt(  # noqa: SLF001 - fixture
            {"id": schedule_id, **_spec("delivery-fk")}
        )
        async with db.transaction() as conn:
            await conn.execute(stmt, params)
        await _append(db, schedule_id, 1)

        oldest = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = 'schedule' AND entity_id = :id",
            {"id": schedule_id},
        )
        assert oldest is not None
        async with db.transaction() as conn:
            await conn.execute(
                state_db_mod.text(
                    "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                    "VALUES (:tid, 'test-consumer', 1.0)"
                ),
                {"tid": oldest["id"]},
            )

        # Pushes the acknowledged transition out of the retained window.
        await _append(db, schedule_id, 4)

    async with StateDB(readonly=True) as db:
        kept = await db.fetch_all(
            "SELECT id FROM status_transitions WHERE entity_type = 'schedule' AND entity_id = :id",
            {"id": schedule_id},
        )
        deliveries = await db.fetch_all("SELECT transition_id FROM terminal_deliveries", {})
    assert len(kept) == 2
    assert oldest["id"] not in {r["id"] for r in kept}
    assert deliveries == []
