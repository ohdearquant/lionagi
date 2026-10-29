# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Registration and replay contracts for durable terminal consumers."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import (
    ConsumerCompatibilityError,
    ConsumerNotRegisteredError,
    ConsumerRetiredError,
    ConsumerSchemaError,
    ack_delivery,
    eligibility_counts,
    is_acknowledged,
    reconcile_unacknowledged,
    register_consumer,
    retire_consumer,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    state = StateDB(tmp_path / "consumer.db")
    await state.open()
    try:
        yield state
    finally:
        await state.close()


async def _fact(db, ident, *, eligible=True, created_at=10.0):
    await db.execute(
        "INSERT INTO status_transitions "
        "(id, entity_type, entity_id, previous_status, status, reason_code, source, created_at, "
        "delivery_eligible) VALUES (:id, 'session', :id, 'running', 'failed', "
        "'session.stale.no_heartbeat', 'system', :created_at, :eligible)",
        {"id": ident, "created_at": created_at, "eligible": eligible},
    )


def _ids(rows):
    return [row["transition_id"] for row in rows]


@pytest.mark.asyncio
async def test_enrollment_baselines_only_visible_eligible_history(db):
    await _fact(db, "existing")
    await _fact(db, "excluded", eligible=False)
    await _fact(db, "unknown", eligible=None)
    now = await register_consumer(db, "now", start="now")
    assert now.created and not now.reactivated
    assert now.counts.current_null_count == 1
    assert now.counts.post_migration_null_count == 1
    assert await is_acknowledged(db, "existing", "now")
    assert not await is_acknowledged(db, "excluded", "now")
    assert not await is_acknowledged(db, "unknown", "now")
    assert _ids(await reconcile_unacknowledged(db, "now")) == []

    # The earlier timestamp models a transition prepared before enrollment but
    # committed after it; no timestamp cutoff may hide it.
    await _fact(db, "late", created_at=1.0)
    assert _ids(await reconcile_unacknowledged(db, "now")) == ["late"]
    before = await register_consumer(db, "all", start="all")
    assert before.created and _ids(await reconcile_unacknowledged(db, "all")) == [
        "late",
        "existing",
    ]
    existing = await register_consumer(db, "all", start="now")
    assert not existing.created and not existing.reactivated
    assert existing.registered_at == before.registered_at
    assert _ids(await reconcile_unacknowledged(db, "all")) == ["late", "existing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["now", "all"])
async def test_retirement_late_ack_and_reactivation_keep_one_identity(db, restart):
    await _fact(db, "first", created_at=1.0)
    await _fact(db, "second", created_at=2.0)
    first = await register_consumer(db, "client", start="all")
    await ack_delivery(db, "first", "client")
    assert await retire_consumer(db, "client")
    assert not await retire_consumer(db, "client")
    assert await reconcile_unacknowledged(db, "client") == []
    with pytest.raises(ConsumerRetiredError):
        await register_consumer(db, "client", start=restart)
    # A worker that completed after retirement can still record its effect.
    await ack_delivery(db, "second", "client")
    await _fact(db, "third", created_at=3.0)
    active = await register_consumer(db, "client", start=restart, reactivate=True)
    assert active.reactivated and not active.created
    assert active.registered_at == first.registered_at
    assert await is_acknowledged(db, "first", "client")
    assert await is_acknowledged(db, "second", "client")
    assert _ids(await reconcile_unacknowledged(db, "client")) == (
        [] if restart == "now" else ["third"]
    )


@pytest.mark.asyncio
async def test_concurrent_ack_is_idempotent_and_unregistered_ack_is_refused(db):
    await _fact(db, "event")
    with pytest.raises(ConsumerNotRegisteredError):
        await ack_delivery(db, "event", "unknown")
    await register_consumer(db, "client", start="all")
    await asyncio.gather(*(ack_delivery(db, "event", "client") for _ in range(4)))
    rows = await db.fetch_all(
        "SELECT transition_id FROM terminal_deliveries WHERE consumer = :consumer",
        {"consumer": "client"},
    )
    assert rows == [{"transition_id": "event"}]


@pytest.mark.asyncio
async def test_keyset_exclusions_filter_before_limit_and_ties_are_stable(db):
    for ident in ("a", "b", "c", "d"):
        await _fact(db, ident, created_at=5.0)
    await register_consumer(db, "client", start="all")
    assert _ids(await reconcile_unacknowledged(db, "client", limit=2, exclude_ids=["a", "b"])) == [
        "c",
        "d",
    ]
    assert _ids(await reconcile_unacknowledged(db, "client", limit=1, after=(5.0, "b"))) == ["c"]
    assert _ids(await reconcile_unacknowledged(db, "client", after=(5.0, "d"))) == []
    assert _ids(await reconcile_unacknowledged(db, "client", limit=10)) == ["a", "b", "c", "d"]
    assert await reconcile_unacknowledged(db, "missing") == []


@pytest.mark.asyncio
async def test_null_count_reports_difference_without_reclassification(db):
    baseline = await eligibility_counts(db)
    await _fact(db, "after-migration", eligible=None, created_at=0.0)
    counts = await eligibility_counts(db)
    assert counts.initial_null_count == baseline.initial_null_count
    assert counts.current_null_count == baseline.current_null_count + 1
    assert counts.post_migration_null_count == baseline.post_migration_null_count + 1
    assert (await register_consumer(db, "client", start="all")).counts == counts
    assert await reconcile_unacknowledged(db, "client") == []


@pytest.mark.asyncio
async def test_readonly_old_schema_refuses_all_consumer_apis_with_version(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES ('version', '5')")
    state = StateDB(path, readonly=True)
    await state.open()
    try:
        operations = (
            lambda: register_consumer(state, "c", start="all"),
            lambda: retire_consumer(state, "c"),
            lambda: ack_delivery(state, "t", "c"),
            lambda: is_acknowledged(state, "t", "c"),
            lambda: reconcile_unacknowledged(state, "c"),
            lambda: eligibility_counts(state),
        )
        for operation in operations:
            with pytest.raises(ConsumerSchemaError, match="schema version 5"):
                await operation()
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_compatibility_refusal_precedes_any_store_access():
    class CompatibilityOnly:
        mode = "compatibility"

    state = CompatibilityOnly()
    operations = (
        lambda: register_consumer(state, "c", start="all"),
        lambda: retire_consumer(state, "c"),
        lambda: ack_delivery(state, "t", "c"),
        lambda: is_acknowledged(state, "t", "c"),
        lambda: reconcile_unacknowledged(state, "c"),
        lambda: eligibility_counts(state),
    )
    for operation in operations:
        with pytest.raises(ConsumerCompatibilityError, match="compatibility"):
            await operation()


@pytest.mark.asyncio
async def test_registration_baseline_and_identity_roll_back_together(db):
    await _fact(db, "existing")
    await db.execute(
        "CREATE TRIGGER refuse_baseline BEFORE INSERT ON terminal_deliveries "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(Exception, match="injected"):
        await register_consumer(db, "client", start="now")
    assert await db.fetch_one("SELECT name FROM terminal_consumers WHERE name = 'client'") is None


@pytest.mark.asyncio
async def test_registration_does_not_ack_writer_waiting_to_commit(db, monkeypatch):
    from lionagi.state.lifecycle import deliveries

    other = StateDB(db.path)
    await other.open()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_counts = deliveries._counts

    async def hold_registration(conn, marker):
        entered.set()
        await release.wait()
        return await original_counts(conn, marker)

    monkeypatch.setattr(deliveries, "_counts", hold_registration)
    registration = asyncio.create_task(register_consumer(db, "client", start="now"))
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # The event's timestamp and intent predate enrollment, but this
        # independent writer cannot commit while enrollment holds SQLite's lock.
        writer = asyncio.create_task(_fact(other, "delayed", created_at=1.0))
        await asyncio.sleep(0)
        assert not writer.done()
        release.set()
        await asyncio.wait_for(registration, timeout=5)
        await asyncio.wait_for(writer, timeout=5)
        assert _ids(await reconcile_unacknowledged(db, "client")) == ["delayed"]
    finally:
        release.set()
        if not registration.done():
            registration.cancel()
            await asyncio.gather(registration, return_exceptions=True)
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await other.close()
