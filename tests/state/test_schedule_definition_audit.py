# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Definition changes and their audit must commit or roll back together."""

from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import text

from lionagi.state.db import ScheduleOwnershipConflictError, StateDB


def schedule(schedule_id="schedule", *, enabled=1):
    return {
        "id": schedule_id,
        "name": schedule_id,
        "enabled": enabled,
        "trigger_type": "interval",
        "interval_sec": 60,
        "action_kind": "agent",
    }


async def events(db, schedule_id="schedule"):
    async with db._read() as conn:
        return [
            dict(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT * FROM status_transitions WHERE entity_type='schedule' "
                        "AND entity_id=:id ORDER BY created_at, id"
                    ),
                    {"id": schedule_id},
                )
            ).mappings()
        ]


@pytest.mark.parametrize("enabled", [0, 1])
async def test_creation_records_the_actual_initial_state(tmp_path, enabled):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule(enabled=enabled))
        history = await events(db)
        assert len(history) == 1
        assert history[0]["previous_status"] is None
        assert history[0]["status"] == ("enabled" if enabled else "disabled")
        assert history[0]["actor"] is None
        assert history[0]["reason_code"] == "schedule_definition.created.observed"


async def test_reenable_and_delete_retain_every_prior_event(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        await db.update_schedule("schedule", enabled=0)
        disabled = await events(db)
        assert [row["status"] for row in disabled] == ["enabled", "disabled"]
        await db.update_schedule("schedule", enabled=1)
        assert await db.delete_schedule("schedule")
        history = await events(db)
        assert [row["status"] for row in history] == ["enabled", "disabled", "enabled", "deleted"]
        assert history[:2] == disabled
        assert await db.get_schedule("schedule") is None


async def test_same_state_and_bookkeeping_updates_do_not_invent_events(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        initial = await events(db)
        assert len(initial) == 1
        assert await db.update_schedule("schedule", enabled=1)
        assert await db.update_schedule("schedule", next_fire_at=123, name="renamed")
        assert await events(db) == initial


@pytest.mark.parametrize("operation", ["create", "disable", "enable", "delete"])
async def test_history_failure_rolls_back_its_definition_write(tmp_path, operation):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        if operation != "create":
            await db.create_schedule(schedule(enabled=0 if operation == "enable" else 1))
        before = await db.get_schedule("schedule")
        history = await events(db)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TRIGGER refuse_schedule_history BEFORE INSERT ON status_transitions "
                "WHEN NEW.entity_type = 'schedule' "
                "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
            )
        with pytest.raises(Exception, match="injected audit failure"):
            if operation == "create":
                await db.create_schedule(schedule())
            elif operation == "delete":
                await db.delete_schedule("schedule")
            else:
                await db.update_schedule("schedule", enabled=1 if operation == "enable" else 0)
        assert await db.get_schedule("schedule") == before
        assert await events(db) == history


async def test_batch_audits_both_disable_causes_and_disabled_creation(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        for member in ("declared", "omitted"):
            await db.create_schedule({**schedule(member), "owner_key": "set-a"})
        await db.apply_schedule_set(
            creates=[{**schedule("new", enabled=0), "owner_key": "set-a"}],
            updates=[("declared", {"enabled": 0})],
            disables=["omitted"],
            expect_owner_keys={"declared": "set-a", "omitted": "set-a"},
        )
        for member, cause in (("declared", "declared"), ("omitted", "omitted")):
            rows = await events(db, member)
            assert len(rows) == 2
            assert rows[-1]["reason_code"] == f"schedule_definition.disabled.{cause}"
            metadata = json.loads(rows[-1]["metadata"])
            assert metadata["declaration"] == {
                "owner_key": "set-a",
                "member_id": member,
                "cause": cause,
            }
            assert metadata["provenance"]["origin"] == "declaration"
            assert rows[-1]["actor"] is None
        created = await events(db, "new")
        assert len(created) == 1
        assert created[0]["status"] == "disabled"
        assert created[0]["reason_code"] == "schedule_definition.created.observed"


@pytest.mark.parametrize("failure", ["late_claim", "audit_insert"])
async def test_failed_batch_rolls_back_every_prior_row_and_event(tmp_path, failure):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        for member in ("updated", "disabled"):
            await db.create_schedule({**schedule(member), "owner_key": "set-a"})
        before = [await db.get_schedule(member) for member in ("updated", "disabled")]
        history = [await events(db, member) for member in ("updated", "disabled")]
        assert all(len(rows) == 1 for rows in history)
        if failure == "audit_insert":
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TRIGGER refuse_batch_audit BEFORE INSERT ON status_transitions "
                    "WHEN NEW.entity_type='schedule' AND NEW.entity_id='disabled' "
                    "BEGIN SELECT RAISE(ABORT, 'injected late audit failure'); END"
                )
        error = ScheduleOwnershipConflictError if failure == "late_claim" else Exception
        with pytest.raises(error):
            await db.apply_schedule_set(
                creates=[schedule("new")],
                updates=[("updated", {"enabled": 0})],
                disables=["disabled"],
                expect_owner_keys={"updated": "set-a", "disabled": "set-a"},
                expect_updated_at={"disabled": -1} if failure == "late_claim" else None,
            )
        assert await db.get_schedule("new") is None
        assert await events(db, "new") == []
        assert [await db.get_schedule(member) for member in ("updated", "disabled")] == before
        assert [await events(db, member) for member in ("updated", "disabled")] == history


def occurrence(run_id="occurrence"):
    return {
        "id": run_id,
        "schedule_id": "schedule",
        "trigger_context": {},
        "action_kind": "agent",
        "action_args": {},
        "fired_at": 123,
    }


@pytest.mark.parametrize("disable", [False, True])
async def test_occurrence_advance_records_only_a_real_definition_edge(tmp_path, disable):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule({**schedule(), "next_fire_at": 123})
        initial = await events(db)
        assert len(initial) == 1
        fields = {"next_fire_at": 456, **({"enabled": 0} if disable else {})}
        assert await db.create_schedule_run_and_advance(
            occurrence(),
            schedule_id="schedule",
            schedule_fields=fields,
            expect_next_fire_at=123,
        )
        history = await events(db)
        assert len(history) == (2 if disable else 1)
        assert history[:1] == initial
        assert len(await db.list_schedule_runs("schedule")) == 1


@pytest.mark.parametrize("failure", ["stale_claim", "audit_insert", "occurrence_insert"])
async def test_failed_occurrence_advance_leaves_no_partial_definition_or_history(tmp_path, failure):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        await db.create_schedule({**schedule(), "next_fire_at": 123})
        before = await db.get_schedule("schedule")
        history = await events(db)
        assert len(history) == 1
        if failure != "stale_claim":
            with sqlite3.connect(path) as conn:
                if failure == "audit_insert":
                    conn.execute(
                        "CREATE TRIGGER refuse_advance_audit BEFORE INSERT ON status_transitions "
                        "WHEN NEW.entity_type='schedule' "
                        "BEGIN SELECT RAISE(ABORT, 'injected advance failure'); END"
                    )
                else:
                    conn.execute(
                        "CREATE TRIGGER refuse_occurrence BEFORE INSERT ON schedule_runs "
                        "BEGIN SELECT RAISE(ABORT, 'injected advance failure'); END"
                    )
        kwargs = dict(
            schedule_id="schedule",
            schedule_fields={"enabled": 0, "next_fire_at": 456},
            expect_next_fire_at=-1 if failure == "stale_claim" else 123,
        )
        if failure == "stale_claim":
            assert not await db.create_schedule_run_and_advance(occurrence(), **kwargs)
        else:
            with pytest.raises(Exception, match="injected advance failure"):
                await db.create_schedule_run_and_advance(occurrence(), **kwargs)
        assert await db.get_schedule("schedule") == before
        assert await events(db) == history
        assert await db.list_schedule_runs("schedule") == []


async def test_missing_rows_and_lost_claims_add_no_event(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule({**schedule(), "owner_key": "set-a", "next_fire_at": 123})
        history = await events(db)
        assert len(history) == 1
        assert not await db.update_schedule("schedule", enabled=0, expect_next_fire_at=-1)
        assert not await db.update_schedule("schedule", enabled=0, expect_owner_key="other")
        assert not await db.update_schedule("missing", enabled=0)
        assert not await db.delete_schedule("missing")
        await db.apply_schedule_set(
            creates=[], updates=[("missing", {"enabled": 0})], disables=["missing"]
        )
        assert await events(db) == history
        assert await events(db, "missing") == []


async def test_history_remains_readable_after_deletion_and_is_bounded(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        await db.update_schedule("schedule", enabled=0)
        await db.delete_schedule("schedule")
        first = await db.get_schedule_history("schedule", limit=2)
        second = await db.get_schedule_history("schedule", limit=2, offset=2)
        assert first["available"] and second["available"]
        assert [row["status"] for row in first["events"] + second["events"]] == [
            "deleted",
            "disabled",
            "enabled",
        ]
        assert await db.get_schedule("schedule") is None
        for bad in (0, -1, 1001):
            with pytest.raises(ValueError):
                await db.get_schedule_history("schedule", limit=bad)


async def test_readonly_missing_ledger_is_unavailable_without_migrating(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        await db.create_schedule(schedule())
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE status_transitions")
    async with StateDB(path, readonly=True) as db:
        assert await db.get_schedule_history("schedule") == {"available": False, "events": []}
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='status_transitions'"
            ).fetchall()
            == []
        )


async def test_untouched_legacy_row_gets_no_invented_initial_event(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        stmt, params = db._build_schedule_insert_stmt(schedule())
        async with db._tx() as conn:
            await conn.execute(stmt, params)
    async with StateDB(path) as db:
        assert await db.get_schedule("schedule")
        assert await db.get_schedule_history("schedule") == {"available": True, "events": []}
        await db.update_schedule("schedule", enabled=0)
        history = await events(db)
        assert len(history) == 1
        assert history[0]["previous_status"] == "enabled"
        assert history[0]["status"] == "disabled"


async def test_deleted_id_cannot_be_reused_but_occurrence_id_is_a_separate_identity(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        await db.create_schedule_run(occurrence("schedule"))
        assert len(await events(db)) == 1
        await db.delete_schedule("schedule")
        history = await events(db)
        with pytest.raises(ValueError):
            await db.create_schedule(schedule())
        assert await db.get_schedule("schedule") is None
        assert await events(db) == history
        reader = await db.get_schedule_history("schedule")
        assert len(reader["events"]) == 2
        assert {row["entity_type"] for row in reader["events"]} == {"schedule"}


@pytest.mark.parametrize("operation", ["update", "delete", "batch", "advance"])
async def test_each_mutation_boundary_keeps_submitted_actor_cwd_and_explanation(
    tmp_path, operation
):
    from lionagi.state.lifecycle import ReasonRecord, ScheduleAuditContext

    context = ScheduleAuditContext(
        actor="actual-submitter",
        submitting_cwd="/submitted/from/here",
        origin="cli",
        source="cli",
        assurance="client_resolved",
        actor_status="available",
        cwd_status="available",
        failure_category=None,
    )
    reason = ReasonRecord(
        code="schedule_definition.deleted.observed"
        if operation == "delete"
        else "schedule_definition.disabled.manual",
        summary="Maintenance window",
    )
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(
            {**schedule(), "next_fire_at": 123, "action_cwd": "/execution/only"}
        )
        kwargs = {"audit_context": context, "reason": reason}
        if operation == "update":
            await db.update_schedule("schedule", enabled=0, **kwargs)
        elif operation == "delete":
            await db.delete_schedule("schedule", **kwargs)
        elif operation == "batch":
            await db.apply_schedule_set(creates=[], updates=[], disables=["schedule"], **kwargs)
        else:
            await db.create_schedule_run_and_advance(
                occurrence(),
                schedule_id="schedule",
                schedule_fields={"enabled": 0},
                expect_next_fire_at=123,
                **kwargs,
            )
        event = (await events(db))[-1]
        assert event["actor"] == "actual-submitter"
        assert event["source"] == "cli"
        assert event["reason_summary"] == "Maintenance window"
        metadata = json.loads(event["metadata"])
        assert metadata["provenance"]["submitting_cwd"] == "/submitted/from/here"
        assert "/execution/only" not in event["metadata"]
        if operation == "batch":
            assert event["reason_code"] == "schedule_definition.disabled.omitted"


async def test_equal_time_history_uses_id_as_stable_tie_breaker(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        await db.update_schedule("schedule", enabled=0)
        await db.update_schedule("schedule", enabled=1)
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE status_transitions SET created_at=123 WHERE entity_type='schedule'")
            )
        expected = sorted((row["id"] for row in await events(db)), reverse=True)
        pages = [await db.get_schedule_history("schedule", limit=1, offset=n) for n in range(3)]
        assert [page["events"][0]["id"] for page in pages] == expected


@pytest.mark.parametrize("invalid_context", [{}, [], "", False])
@pytest.mark.parametrize("operation", ["create", "update", "disable"])
async def test_batch_refuses_falsy_supplied_context_before_writing(
    tmp_path, invalid_context, operation
):
    async with StateDB(tmp_path / "state.db") as db:
        await db.apply_schedule_set(
            creates=[schedule()], updates=[], disables=[], audit_context=None
        )
        before, history = await db.get_schedule("schedule"), await events(db)
        assert len(history) == 1
        with pytest.raises(ValueError, match="ScheduleAuditContext"):
            await db.apply_schedule_set(
                creates=[schedule("new")] if operation == "create" else [],
                updates=[("schedule", {"enabled": 0})] if operation == "update" else [],
                disables=["schedule"] if operation == "disable" else [],
                audit_context=invalid_context,
            )
        assert await db.get_schedule("schedule") == before
        assert await events(db) == history
        assert await db.get_schedule("new") is None
        assert await events(db, "new") == []


@pytest.mark.parametrize("boundary", ["update", "initialize"])
async def test_occurrence_cannot_use_definition_reason_through_legacy_boundaries(
    tmp_path, boundary
):
    from lionagi.state.lifecycle import ActorRecord, InitialStateCommand, ReasonRecord

    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule())
        await db.create_schedule_run({**occurrence("control"), "status": "queued"})
        assert await db.update_status(
            "schedule_run",
            "control",
            new_status="running",
            reason_code="schedule.fired.due",
            source="system",
            actor="test-scheduler",
        )
        if boundary == "update":
            await db.create_schedule_run({**occurrence(), "status": "queued"})
        else:
            stmt, params = db._build_schedule_run_insert_stmt({**occurrence(), "status": "queued"})
            async with db._tx() as conn:
                await conn.execute(stmt, params)
        before = await db.get_schedule_run("occurrence")
        async with db._read() as conn:
            history = [
                dict(row)
                for row in (
                    await conn.execute(text("SELECT * FROM status_transitions ORDER BY id"))
                ).mappings()
            ]
        with pytest.raises(
            ValueError, match="schedule_definition reasons belong only to definitions"
        ):
            if boundary == "update":
                await db.update_status(
                    "schedule_run",
                    "occurrence",
                    new_status="running",
                    reason_code="schedule_definition.disabled.manual",
                    source="system",
                    actor="test-scheduler",
                )
            else:
                async with db._tx() as conn:
                    await db._lifecycle_service().initialize_in_transaction(
                        conn,
                        InitialStateCommand(
                            entity_type="schedule_run",
                            entity_id="occurrence",
                            status="queued",
                            reason=ReasonRecord(code="schedule_definition.created.observed"),
                            actor=ActorRecord(type="scheduler", id="test-scheduler"),
                        ),
                    )
        assert await db.get_schedule_run("occurrence") == before
        async with db._read() as conn:
            assert [
                dict(row)
                for row in (
                    await conn.execute(text("SELECT * FROM status_transitions ORDER BY id"))
                ).mappings()
            ] == history


@pytest.mark.parametrize(
    "origin,actor,cwd,assurance,failure",
    [
        ("http", "reported-actor", "/reported/cwd", "client_reported", None),
        ("browser", None, None, "unavailable", "browser_unavailable"),
        ("cli", None, "/known/cwd", "unavailable", "resolver_failed"),
        ("mcp", "resolved-actor", None, "client_resolved", "cwd_unavailable"),
    ],
)
async def test_valid_provenance_cases_preserve_independent_availability(
    tmp_path, origin, actor, cwd, assurance, failure
):
    from lionagi.state.lifecycle import ScheduleAuditContext

    context = ScheduleAuditContext(
        actor=actor,
        submitting_cwd=cwd,
        origin=origin,
        source=origin,
        assurance=assurance,
        actor_status="available" if actor else "unavailable",
        cwd_status="available" if cwd else "unavailable",
        failure_category=failure,
    )
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(schedule(), audit_context=context)
        event = (await db.get_schedule_history("schedule"))["events"][0]
        assert event["actor"] == actor
        assert event["source"] == origin
        assert event["metadata"]["provenance"] == {
            "origin": origin,
            "submitting_cwd": cwd,
            "assurance": assurance,
            "actor_status": context.actor_status,
            "cwd_status": context.cwd_status,
            "failure_category": failure,
        }


async def test_apply_takes_every_definition_lock_in_sorted_order_first(tmp_path, monkeypatch):
    from lionagi.state.lifecycle.schedules import ScheduleLifecycleAdapter

    locked = []
    original = ScheduleLifecycleAdapter._lock_id

    async def recording(self, conn, schedule_id):
        locked.append(schedule_id)
        await original(self, conn, schedule_id)

    async with StateDB(tmp_path / "state.db") as db:
        for member in ("a", "b", "c", "d"):
            await db.create_schedule({**schedule(member), "owner_key": "set-a"})
        monkeypatch.setattr(ScheduleLifecycleAdapter, "_lock_id", recording)
        # Plan order runs against id order in every phase, so an apply that locked as it
        # went would take e, d, b, c, a.
        await db.apply_schedule_set(
            creates=[{**schedule("e"), "owner_key": "set-a"}],
            updates=[("d", {"enabled": 0}), ("b", {"enabled": 0})],
            disables=["c", "a"],
            expect_owner_keys={member: "set-a" for member in ("a", "b", "c", "d")},
        )
    assert locked[:5] == ["a", "b", "c", "d", "e"]
