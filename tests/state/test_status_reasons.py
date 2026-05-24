# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0028 status reason model tests."""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.reasons import (
    ENTITY_ROUTE_ALIASES,
    ENTITY_TABLE_ALIASES,
    ENTITY_TYPE_TO_TABLE,
    LEGACY_IMPORTED,
    VALID_ENTITY_TYPES,
    VALID_REASON_CODES,
    PlayReasons,
    RunReasons,
    ScheduleReasons,
    SessionReasons,
    ShowReasons,
    entity_table,
    validate_entity_type,
    validate_reason_code,
)

# ── reasons.py — namespace and validators ────────────────────────────


class TestReasonNamespace:
    def test_run_reasons_are_three_segment(self):
        for name, value in vars(RunReasons).items():
            if name.startswith("_") or not isinstance(value, str):
                continue
            segments = value.split(".")
            assert len(segments) == 3, (
                f"RunReasons.{name} ({value!r}) must be three segments, got {segments}"
            )
            assert all(s for s in segments), f"RunReasons.{name} ({value!r}) has empty segments"

    def test_all_reason_classes_are_three_segment_except_legacy(self):
        for cls in (RunReasons, SessionReasons, PlayReasons, ShowReasons, ScheduleReasons):
            for name, value in vars(cls).items():
                if name.startswith("_") or not isinstance(value, str):
                    continue
                segments = value.split(".")
                assert len(segments) == 3, (
                    f"{cls.__name__}.{name} ({value!r}) must be three segments"
                )

    def test_legacy_imported_is_two_segments(self):
        assert LEGACY_IMPORTED.split(".") == ["legacy", "imported"]
        assert LEGACY_IMPORTED in VALID_REASON_CODES

    def test_valid_reason_codes_excludes_dunders(self):
        # The collector filters __module__, __dict__, __weakref__, etc.
        # If it were broken, VALID_REASON_CODES would include strings
        # like 'lionagi.state.reasons'.
        for code in VALID_REASON_CODES:
            assert not code.startswith("_"), (
                f"reason code {code!r} looks like a dunder/private attr"
            )
            # Module names contain "lionagi" or "state" — codes should not.
            assert "lionagi" not in code
            assert code.count(".") in (1, 2), f"reason code {code!r} has unexpected segment count"

    def test_valid_reason_codes_has_expected_membership(self):
        assert RunReasons.COMPLETED_OK in VALID_REASON_CODES
        assert RunReasons.FAILED_MISSING_ARTIFACT in VALID_REASON_CODES
        assert SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD in VALID_REASON_CODES
        assert PlayReasons.PENDING_WAITING_DEPS in VALID_REASON_CODES
        assert ShowReasons.BLOCKED_NO_READY_PLAYS in VALID_REASON_CODES
        assert ScheduleReasons.FIRED_DUE in VALID_REASON_CODES


class TestValidators:
    def test_validate_reason_code_accepts_legal(self):
        assert validate_reason_code(RunReasons.COMPLETED_OK) == RunReasons.COMPLETED_OK
        assert validate_reason_code(LEGACY_IMPORTED) == LEGACY_IMPORTED

    def test_validate_reason_code_rejects_unknown(self):
        with pytest.raises(ValueError, match="invalid reason_code"):
            validate_reason_code("not.a.real.code")
        with pytest.raises(ValueError, match="invalid reason_code"):
            validate_reason_code("")

    def test_validate_entity_type_accepts_canonical(self):
        for et in VALID_ENTITY_TYPES:
            assert validate_entity_type(et) == et

    def test_validate_entity_type_resolves_route_aliases(self):
        for alias, canonical in ENTITY_ROUTE_ALIASES.items():
            assert validate_entity_type(alias) == canonical

    def test_validate_entity_type_resolves_table_aliases(self):
        for plural, singular in ENTITY_TABLE_ALIASES.items():
            assert validate_entity_type(plural) == singular

    def test_validate_entity_type_rejects_unknown(self):
        with pytest.raises(ValueError, match="invalid entity_type"):
            validate_entity_type("foobar")

    def test_entity_table_lookup(self):
        for et, table in ENTITY_TYPE_TO_TABLE.items():
            assert entity_table(et) == table
        # Aliases resolve correctly too.
        assert entity_table("run") == "sessions"
        assert entity_table("sessions") == "sessions"


# ── StateDB.update_status() — schema migration + atomic writes ───────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _create_session(db: StateDB, *, status: str = "running") -> str:
    sid = uuid.uuid4().hex
    pid = uuid.uuid4().hex
    await db.db.execute(
        "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
        (pid, time.time(), "[]"),
    )
    await db.db.execute(
        "INSERT INTO sessions (id, created_at, progression_id, updated_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, time.time(), pid, time.time(), status),
    )
    await db.db.commit()
    return sid


class TestMigration:
    async def test_status_transitions_table_created(self, db: StateDB):
        cur = await db.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='status_transitions'"
        )
        row = await cur.fetchone()
        assert row is not None, "status_transitions table missing"

    @pytest.mark.parametrize(
        "table",
        ["sessions", "shows", "plays", "invocations", "teams", "schedule_runs"],
    )
    async def test_reason_columns_present(self, db: StateDB, table: str):
        cur = await db.db.execute(f"PRAGMA table_info({table})")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "status_reason_code" in cols, f"{table}.status_reason_code missing"
        assert "status_reason_summary" in cols, f"{table}.status_reason_summary missing"
        assert "status_evidence_refs" in cols, f"{table}.status_evidence_refs missing"

    async def test_schedule_runs_has_updated_at(self, db: StateDB):
        cur = await db.db.execute("PRAGMA table_info(schedule_runs)")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "updated_at" in cols, "ADR-0028 requires schedule_runs.updated_at"

    async def test_status_transitions_indexes(self, db: StateDB):
        cur = await db.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='status_transitions'"
        )
        names = {row["name"] for row in await cur.fetchall()}
        assert "idx_status_transitions_entity" in names
        assert "idx_status_transitions_reason" in names
        assert "idx_status_transitions_created" in names

    async def test_sessions_status_updated_index_for_failed_queries(self, db: StateDB):
        # ADR-0030's attention queue needs this for failed/timed_out lookups.
        cur = await db.db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_sessions_status_updated'"
        )
        assert (await cur.fetchone()) is not None


class TestUpdateStatusAtomic:
    async def test_writes_denormalized_and_transition_row(self, db: StateDB):
        sid = await _create_session(db)
        await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="Run completed successfully.",
            evidence_refs=[{"kind": "session", "id": sid}],
        )

        cur = await db.db.execute(
            "SELECT status, status_reason_code, status_reason_summary, "
            "       status_evidence_refs FROM sessions WHERE id = ?",
            (sid,),
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "completed"
        assert row["status_reason_code"] == RunReasons.COMPLETED_OK
        assert row["status_reason_summary"] == "Run completed successfully."
        # JSON column comes back as a string under aiosqlite Row.
        import json

        assert json.loads(row["status_evidence_refs"]) == [{"kind": "session", "id": sid}]

        cur = await db.db.execute(
            "SELECT entity_type, entity_id, previous_status, status, "
            "       reason_code, source FROM status_transitions WHERE entity_id = ?",
            (sid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1
        assert rows[0] == {
            "entity_type": "session",
            "entity_id": sid,
            "previous_status": "running",
            "status": "completed",
            "reason_code": RunReasons.COMPLETED_OK,
            "source": "executor",
        }

    async def test_appends_multiple_transitions(self, db: StateDB):
        sid = await _create_session(db)
        await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
        )
        await db.update_status(
            "session",
            sid,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="RuntimeError: x",
        )
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?",
            (sid,),
        )
        assert (await cur.fetchone())["n"] == 2

    async def test_alias_route_and_table_resolved(self, db: StateDB):
        sid = await _create_session(db)
        # "run" → "session", "sessions" → "session"
        await db.update_status(
            "run",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
        )
        await db.update_status(
            "sessions",
            sid,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
        )
        cur = await db.db.execute(
            "SELECT entity_type, COUNT(*) AS n FROM status_transitions "
            "WHERE entity_id = ? GROUP BY entity_type",
            (sid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        # Both rows recorded under canonical "session" entity_type.
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "session"
        assert rows[0]["n"] == 2

    async def test_legacy_imported_is_accepted(self, db: StateDB):
        sid = await _create_session(db)
        await db.update_status(
            "session",
            sid,
            new_status="failed",
            reason_code=LEGACY_IMPORTED,
            reason_summary="Pre-ADR-0028 row.",
        )
        cur = await db.db.execute("SELECT status_reason_code FROM sessions WHERE id = ?", (sid,))
        assert (await cur.fetchone())["status_reason_code"] == LEGACY_IMPORTED


class TestUpdateStatusValidation:
    async def test_invalid_reason_code_raises_valueerror(self, db: StateDB):
        sid = await _create_session(db)
        with pytest.raises(ValueError, match="invalid reason_code"):
            await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code="not.a.real.code",
            )
        # And the entity row + transition log remain untouched.
        cur = await db.db.execute(
            "SELECT status, status_reason_code FROM sessions WHERE id = ?", (sid,)
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "running"
        assert row["status_reason_code"] is None

    async def test_invalid_entity_type_raises_valueerror(self, db: StateDB):
        with pytest.raises(ValueError, match="invalid entity_type"):
            await db.update_status(
                "garbage",
                "id",
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
            )

    async def test_missing_entity_raises_lookuperror(self, db: StateDB):
        with pytest.raises(LookupError, match="not found"):
            await db.update_status(
                "session",
                "nonexistent",
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
            )

    async def test_atomic_rollback_on_failure(self, db: StateDB):
        """If the transition INSERT fails, the entity UPDATE must roll back."""
        sid = await _create_session(db)
        # Force a failure by inserting a duplicate id into status_transitions
        # via a deterministic patch. We monkey-patch uuid.uuid4() temporarily.
        # Easier: pre-insert a status_transitions row with a known id, then
        # ensure subsequent attempts that collide raise and rollback.
        # We'll simulate by patching db.db.execute to raise on the second
        # call within the transaction.
        original_execute = db.db.execute
        calls: list[str] = []

        async def flaky_execute(sql: str, *args, **kwargs):
            calls.append(sql)
            if "INSERT INTO status_transitions" in sql:
                raise RuntimeError("simulated transition INSERT failure")
            return await original_execute(sql, *args, **kwargs)

        db.db.execute = flaky_execute  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                await db.update_status(
                    "session",
                    sid,
                    new_status="completed",
                    reason_code=RunReasons.COMPLETED_OK,
                )
        finally:
            db.db.execute = original_execute  # type: ignore[assignment]
        # Both writes were rolled back together.
        cur = await db.db.execute(
            "SELECT status, status_reason_code FROM sessions WHERE id = ?", (sid,)
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "running", "entity UPDATE must roll back on failure"
        assert row["status_reason_code"] is None

        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert (await cur.fetchone())["n"] == 0


# ── Integration: CLI teardown writes a real reason ───────────────────


class TestTeardownReasonResolution:
    """Smoke tests for the cli/agent.py _resolve_run_reason() helper.

    The full CLI path is tested in tests/cli/test_agent_*; here we only
    verify the mapping each terminal status produces is in the namespace.
    """

    def test_each_terminal_status_maps_to_a_valid_reason_code(self):
        from lionagi.cli.agent import _resolve_run_reason

        cases = [
            ("completed", None),
            ("failed", RuntimeError("boom")),
            ("failed", None),  # bare failed with no exception
            ("timed_out", None),
            ("aborted", None),
            ("cancelled", None),
        ]
        for status, exc in cases:
            code, summary, evidence = _resolve_run_reason(
                status=status,
                exception=exc,
            )
            assert code in VALID_REASON_CODES, (
                f"({status}, {type(exc).__name__ if exc else 'None'}) -> "
                f"{code!r} not in VALID_REASON_CODES"
            )
            assert isinstance(summary, str) and summary, "summary must be non-empty"

    def test_failed_with_exception_embeds_class_name(self):
        from lionagi.cli.agent import _resolve_run_reason

        _, summary, _ = _resolve_run_reason(
            status="failed",
            exception=ValueError("bad input"),
        )
        assert "ValueError" in summary
        assert "bad input" in summary


# ── ADR-0028 Phase 2: invocation transition writes reason ────────────


async def _create_invocation(db: StateDB, *, status: str = "running") -> str:
    inv_id = uuid.uuid4().hex[:12]
    now = time.time()
    await db.db.execute(
        "INSERT INTO invocations (id, skill, status, created_at, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (inv_id, "test:skill", status, now, now, now),
    )
    await db.db.commit()
    return inv_id


class TestInvocationTransition:
    async def test_update_status_direct(self, db: StateDB):
        """Direct update_status() on entity_type='invocation' works."""
        inv_id = await _create_invocation(db)
        await db.update_status(
            "invocation",
            inv_id,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="All child sessions completed successfully.",
            evidence_refs=[{"kind": "session", "id": "abc"}],
            source="executor",
            actor=inv_id,
        )

        cur = await db.db.execute(
            "SELECT status, status_reason_code, status_reason_summary "
            "FROM invocations WHERE id = ?",
            (inv_id,),
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "completed"
        assert row["status_reason_code"] == RunReasons.COMPLETED_OK
        assert "child sessions" in row["status_reason_summary"]

        cur = await db.db.execute(
            "SELECT entity_type, previous_status, status, reason_code "
            "FROM status_transitions WHERE entity_id = ?",
            (inv_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "invocation"
        assert rows[0]["previous_status"] == "running"
        assert rows[0]["status"] == "completed"
        assert rows[0]["reason_code"] == RunReasons.COMPLETED_OK

    async def test_update_invocation_routes_status_through_update_status(self, db: StateDB):
        """ADR-0028 Phase 2: update_invocation(status=...) writes reason atomically."""
        inv_id = await _create_invocation(db)
        await db.update_invocation(
            inv_id,
            status="completed",
            ended_at=time.time(),
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="All children passed.",
        )

        cur = await db.db.execute(
            "SELECT status, status_reason_code, ended_at IS NOT NULL AS has_end "
            "FROM invocations WHERE id = ?",
            (inv_id,),
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "completed"
        assert row["status_reason_code"] == RunReasons.COMPLETED_OK
        assert row["has_end"] == 1

        # Transition row was written through update_status().
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?",
            (inv_id,),
        )
        assert (await cur.fetchone())["n"] == 1

    async def test_update_invocation_compat_warns_when_reason_omitted(self, db: StateDB):
        """Legacy callers that pass status without reason_code get a deprecation
        warning + a default code so they keep working through the migration."""
        import warnings

        inv_id = await _create_invocation(db)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await db.update_invocation(inv_id, status="failed")
            deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecations) == 1, "missing-reason_code call must emit one DeprecationWarning"
        assert "reason_code" in str(deprecations[0].message)

        cur = await db.db.execute(
            "SELECT status_reason_code FROM invocations WHERE id = ?",
            (inv_id,),
        )
        # Compat shim defaults to the generic exception reason for 'failed'.
        row = dict(await cur.fetchone())
        assert row["status_reason_code"] == RunReasons.FAILED_EXCEPTION

    async def test_update_invocation_no_status_skips_reason_path(self, db: StateDB):
        """Updates that don't touch status don't write to status_transitions."""
        inv_id = await _create_invocation(db)
        await db.update_invocation(inv_id, ended_at=time.time())

        cur = await db.db.execute(
            "SELECT status, status_reason_code FROM invocations WHERE id = ?",
            (inv_id,),
        )
        row = dict(await cur.fetchone())
        assert row["status"] == "running"  # unchanged
        assert row["status_reason_code"] is None  # never touched

        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?",
            (inv_id,),
        )
        assert (await cur.fetchone())["n"] == 0
