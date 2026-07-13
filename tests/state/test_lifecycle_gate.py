# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StateDB lifecycle gate.

Regression class this module pins, in plain terms:

1. ``StateDB.update_status()`` has two different failure contracts that look
   similar and have been confused before: a CAS-miss (a stale
   ``expected_statuses``/``expected_updated_at`` guard) returns ``False``
   silently, with no row change and no exception — an ordinary lost race.
   Attempting to move a *terminal* entity to a different status without
   ``override=True`` is a completely different case: it raises
   ``TransitionRejectedError``. A caller (or a future refactor) that treats
   one of these as the other is a live bug, not a style choice. The two
   contracts can also *interact* — a terminal row with a stale guard still
   returns ``False``, not a raise, because the guard check happens before
   the terminal-exit check; this file pins that interaction directly.

2. New statuses get added to one vocabulary source (a schema ``CHECK``
   constraint, the ``PolicyRegistry``, or the ``VALID_STATUSES_BY_ENTITY_TYPE``
   facade in ``lionagi/state/db.py``) without a matching edit everywhere
   else. This file pins an exact, sorted status list per entity so an
   addition anywhere shows up here as a deliberate diff, not a silent
   widening (or narrowing) of what ``update_status()`` accepts.

3. The transition-policy edge graph (``PolicyRegistry``) is only enforced
   by the *public* ``SQLAlchemyLifecycleService.transition()`` entry point —
   ``StateDB.update_status()`` never enforces it (see
   ``lionagi/state/lifecycle/adapters.py``'s ``enforce_edges`` docstring).
   This file walks every declared edge for session/invocation/play/
   schedule_run and asserts it applies, then samples undeclared edges and
   asserts they come back as a ``"rejected"`` outcome (with an
   ``admin_events`` audit row) — never a raise, never a silent write.

4. The reaper pattern used throughout
   ``lionagi/studio/services/lifecycle.py`` (guard a stale-row transition on
   both ``expected_statuses`` *and* ``expected_updated_at``) is easy to
   weaken without any test noticing: a change that drops the
   ``expected_updated_at`` guard still passes every status-only test, and
   still over-reaps a row that was legitimately re-claimed between the scan
   and the write (status-membership alone cannot distinguish "still stale"
   from "just re-touched"). This file reproduces that exact guarded-write
   shape directly against ``StateDB`` and pins both outcomes: a genuinely
   stale row is reaped, a row whose ``updated_at`` moved between read and
   write is not.

Everything here runs against a temp-dir sqlite ``StateDB`` (``tmp_path /
"state.db"``) — never ``~/.lionagi/state.db``, never any shared path.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import (
    VALID_STATUSES_BY_ENTITY_TYPE,
    StateDB,
    TransitionRejectedError,
)
from lionagi.state.lifecycle import ActorRecord, ReasonRecord, TransitionCommand
from lionagi.state.lifecycle.policy import DEFAULT_REGISTRY
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
from lionagi.state.reasons import PlayReasons, RunReasons, SessionReasons

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "lionagi" / "state" / "schema.sql"

# Mirrors lionagi.studio.services.lifecycle._REAPABLE_PLAY_STATUSES exactly
# (dead-runner-in-flight play statuses; excludes "gated" — a paused gate is
# legitimately long-lived — and "pending" — queued, not an in-flight crash).
# Not imported directly: lionagi.studio.services.* pulls in fastapi, an
# optional extra not installed in the plain `lionagi` dev environment this
# test suite (tests/state/) runs under.
_REAPABLE_PLAY_STATUSES = frozenset({"running", "running_complete", "prepared", "redoing"})


def _wait_clock_past(ts: float) -> None:
    """Block until time.time() is strictly greater than ``ts``.

    updated_at is a float-seconds timestamp; on a coarse clock two immediate
    writes can land the same value, which would let a "stale" version
    snapshot accidentally still match. Version-guard tests call this between
    taking the snapshot and the concurrent write so staleness is guaranteed,
    not clock-dependent.
    """
    while time.time() <= ts:
        time.sleep(0.001)


# ── Fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    state = StateDB(tmp_path / "state.db")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return uuid.uuid4().hex


async def _make_session(db: StateDB, *, status: str) -> str:
    prog_id = _uid()
    await db.create_progression(prog_id)
    sid = _uid()
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})
    return sid


async def _make_invocation(db: StateDB, *, status: str) -> str:
    inv_id = _uid()
    await db.create_invocation(
        {"id": inv_id, "skill": "gate-test-skill", "started_at": time.time(), "status": status}
    )
    return inv_id


async def _make_show(db: StateDB, *, status: str = "active") -> str:
    show_id = _uid()
    await db.create_show(
        {
            "id": show_id,
            "topic": f"gate-topic-{show_id[:8]}",
            "show_dir": "/tmp/gate",
            "status": status,
        }
    )
    return show_id


async def _make_play(db: StateDB, *, status: str) -> str:
    show_id = await _make_show(db)
    play_id = _uid()
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": f"gate-play-{play_id[:8]}",
            "status": status,
            "started_at": time.time(),
        }
    )
    return play_id


async def _make_schedule_run(db: StateDB, *, status: str) -> str:
    sched_id = _uid()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"gate-sched-{sched_id[:8]}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = _uid()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": status,
            "fired_at": time.time(),
        }
    )
    return run_id


async def _make_entity(entity_type: str, db: StateDB, status: str) -> str:
    if entity_type == "session":
        return await _make_session(db, status=status)
    if entity_type == "invocation":
        return await _make_invocation(db, status=status)
    if entity_type == "play":
        return await _make_play(db, status=status)
    if entity_type == "schedule_run":
        return await _make_schedule_run(db, status=status)
    raise ValueError(f"no entity factory for {entity_type!r}")


def _schema_check_status_values(table: str) -> frozenset[str]:
    """Extract the exact ``CHECK(status IN (...))`` value list for *table*
    directly out of ``schema.sql`` — the authoritative on-disk vocabulary,
    read fresh rather than hand-copied, so this test cannot itself drift
    from the schema it is meant to police."""
    schema_text = _SCHEMA_PATH.read_text()
    table_match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\);",
        schema_text,
        re.S,
    )
    assert table_match is not None, f"table {table!r} not found in {_SCHEMA_PATH}"
    body = table_match.group(1)
    check_match = re.search(r"CHECK\(\s*status\s+IN\s*\((.*?)\)\s*\)", body, re.S)
    assert check_match is not None, f"no `CHECK(status IN (...))` found for table {table!r}"
    values = re.findall(r"'([^']+)'", check_match.group(1))
    assert values, f"empty status CHECK value list parsed for table {table!r}"
    return frozenset(values)


# ── 1. Status vocabulary golden ──────────────────────────────────────────────

# Exact status vocabulary per managed entity, pinned as sorted lists. A
# status added to (or removed from) any one of the three sources this test
# cross-checks — the schema CHECK, the PolicyRegistry, or the
# VALID_STATUSES_BY_ENTITY_TYPE facade — must show up here as a deliberate
# edit, not a silent drift between sources.
_EXPECTED_STATUSES: dict[str, list[str]] = {
    "session": sorted(
        {
            "running",
            "completed",
            "completed_empty",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        }
    ),
    "invocation": sorted(
        {
            "running",
            "completed",
            "completed_empty",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        }
    ),
    "play": sorted(
        {
            "pending",
            "prepared",
            "running",
            "running_complete",
            "gated",
            "gate_failed",
            "redoing",
            "merged",
            "escalated",
            "blocked",
            "aborted_after_finish",
        }
    ),
    "schedule_run": sorted(
        {
            "queued",
            "waiting_dependency",
            "running",
            "retry_wait",
            "completed",
            "failed",
            "timed_out",
            "skipped",
            "cancelled",
        }
    ),
    "show": sorted({"active", "completed", "aborted", "imported"}),
    "team": sorted({"active", "archived"}),
}

# Tables whose status vocabulary is *also* enforced by a SQLite CHECK
# constraint. `session.status` is deliberately Python-only (ADR-0057 — see
# schema.sql's comment on the `sessions` table) so it is intentionally
# excluded from this cross-check.
_CHECK_ENFORCED_TABLES: dict[str, str] = {
    "invocation": "invocations",
    "play": "plays",
    "schedule_run": "schedule_runs",
    "show": "shows",
    "team": "teams",
}


def test_status_vocabulary_covers_every_status_managed_entity() -> None:
    """Every entity type the update_status() gate manages must have a pinned
    expected list above — a new status-managed entity added to the registry
    without a matching golden entry here would otherwise sit outside the
    drift gate entirely."""
    assert sorted(_EXPECTED_STATUSES) == sorted(VALID_STATUSES_BY_ENTITY_TYPE)


@pytest.mark.parametrize("entity_type", sorted(_EXPECTED_STATUSES))
def test_status_vocabulary_golden_against_policy_registry(entity_type: str) -> None:
    """`VALID_STATUSES_BY_ENTITY_TYPE` (the update_status() gate) and the
    PolicyRegistry's own `.statuses` must both equal the pinned list — they
    are sourced from the same registry today, but this test would still
    catch either one drifting independently."""
    assert sorted(VALID_STATUSES_BY_ENTITY_TYPE[entity_type]) == _EXPECTED_STATUSES[entity_type]
    assert sorted(DEFAULT_REGISTRY.get(entity_type).statuses) == _EXPECTED_STATUSES[entity_type]


@pytest.mark.parametrize("entity_type", sorted(_CHECK_ENFORCED_TABLES))
def test_status_vocabulary_golden_against_schema_check(entity_type: str) -> None:
    """A status the PolicyRegistry declares but the schema CHECK omits (or
    vice versa) is a live footgun: update_status() would accept a value
    SQLite itself rejects at the UPDATE (an IntegrityError surfacing far
    from the actual mistake), or the CHECK would silently admit a value the
    unified policy never validates. Pin exact parity between the two."""
    table = _CHECK_ENFORCED_TABLES[entity_type]
    assert sorted(_schema_check_status_values(table)) == _EXPECTED_STATUSES[entity_type]


# ── 2. Transition-policy matrix ──────────────────────────────────────────────
# Only the *public* SQLAlchemyLifecycleService.transition() entry point
# enforces the PolicyRegistry's declared-edge graph (StateDB.update_status()
# does not — see lionagi/state/lifecycle/adapters.py's `enforce_edges`
# docstring), so the matrix below drives that public entry point directly.

_MATRIX_ENTITY_TYPES = ("session", "invocation", "play", "schedule_run")

# Any reason code whose domain prefix belongs to the entity's
# `reason_prefixes` works for every one of its declared edges: the service
# only checks the code's `<domain>.` prefix against the policy
# (`command.reason.code.split(".", 1)[0]`), not a per-status mapping.
_REASON_CODE_FOR_ENTITY: dict[str, str] = {
    "session": RunReasons.COMPLETED_OK,  # prefix "run" ∈ {"run", "session"}
    "invocation": RunReasons.COMPLETED_OK,  # prefix "run" ∈ {"run"}
    "play": PlayReasons.MERGED_OK,  # prefix "play" ∈ {"play"}
    "schedule_run": RunReasons.COMPLETED_OK,  # prefix "run" ∈ {"run", "schedule"}
}


def _declared_edges(entity_type: str) -> list[tuple[str, str]]:
    policy = DEFAULT_REGISTRY.get(entity_type)
    return [
        (from_status, edge.to_status)
        for from_status, edges in policy.edges.items()
        for edge in edges
    ]


_ALL_DECLARED_EDGES: list[tuple[str, str, str]] = [
    (entity_type, from_status, to_status)
    for entity_type in _MATRIX_ENTITY_TYPES
    for from_status, to_status in _declared_edges(entity_type)
]

# A representative sample of undeclared moves: same-vocabulary, but not in
# the policy's declared edge set for that `from_status` (including exits
# from a terminal status, which have no declared edges at all).
_UNDECLARED_EDGE_SAMPLE: list[tuple[str, str, str]] = [
    ("session", "failed", "aborted"),
    ("session", "completed", "timed_out"),
    ("invocation", "cancelled", "running"),
    ("play", "merged", "pending"),
    ("play", "gate_failed", "redoing"),
    ("schedule_run", "waiting_dependency", "running"),
    ("schedule_run", "retry_wait", "failed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type,from_status,to_status", _ALL_DECLARED_EDGES)
async def test_every_declared_edge_applies(
    db: StateDB, entity_type: str, from_status: str, to_status: str
) -> None:
    entity_id = await _make_entity(entity_type, db, from_status)
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        TransitionCommand(
            entity_type=entity_type,
            entity_id=entity_id,
            to_status=to_status,
            reason=ReasonRecord(code=_REASON_CODE_FOR_ENTITY[entity_type]),
            actor=ActorRecord(type="system", id="lifecycle-gate-test"),
        )
    )

    assert outcome.result == "applied", (
        f"{entity_type} {from_status!r} -> {to_status!r} is declared in the "
        "PolicyRegistry and must apply"
    )
    assert outcome.current_status == to_status


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type,from_status,to_status", _UNDECLARED_EDGE_SAMPLE)
async def test_undeclared_edge_sample_rejected_not_raised(
    db: StateDB, entity_type: str, from_status: str, to_status: str
) -> None:
    """The public transition() entry point reports an undeclared move as a
    `"rejected"` TransitionOutcome (with an admin_events audit row) — never a
    raise, and the entity's status never actually moves."""
    entity_id = await _make_entity(entity_type, db, from_status)
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        TransitionCommand(
            entity_type=entity_type,
            entity_id=entity_id,
            to_status=to_status,
            reason=ReasonRecord(code=_REASON_CODE_FOR_ENTITY[entity_type]),
            actor=ActorRecord(type="system", id="lifecycle-gate-test"),
        )
    )

    assert outcome.result == "rejected"
    assert outcome.current_status == from_status
    assert outcome.transition_id is None

    events = await db.list_admin_events(action="status_transition_rejected", target_id=entity_id)
    assert len(events) == 1

    transitions = await db.fetch_all(
        "SELECT * FROM status_transitions WHERE entity_id = ?", (entity_id,)
    )
    assert transitions == []


# ── 3. CAS behavior: silent False vs raised TransitionRejectedError ─────────


@pytest.mark.asyncio
async def test_cas_miss_on_expected_statuses_returns_false_silently(db: StateDB) -> None:
    """A stale expected_statuses guard — the caller believed the row was
    still `running`, but a concurrent writer already moved it — is an
    ordinary lost race: `False`, no exception, no row change."""
    sid = await _make_session(db, status="running")

    # A "newer" writer marks the session terminal first.
    applied_first = await db.update_status(
        "session",
        sid,
        new_status="completed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
        expected_statuses={"running"},
    )
    assert applied_first is True

    # A second writer, still holding its stale "running" snapshot, tries the
    # same CAS and must lose silently — not raise, not log-only.
    applied_second = await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
        expected_statuses={"running"},
    )
    assert applied_second is False

    row = await db.get_session(sid)
    assert row["status"] == "completed"  # the first (newer) write wins, untouched by the second


@pytest.mark.asyncio
async def test_cas_miss_on_expected_updated_at_returns_false_silently(db: StateDB) -> None:
    """The version guard (`expected_updated_at`) is a distinct CAS channel
    from `expected_statuses` — pinned separately because a caller could
    plausibly drop one and keep the other without any status-only test
    noticing. A stale version snapshot is also a silent `False`."""
    sid = await _make_session(db, status="running")
    stale_snapshot = await db.get_session(sid)
    stale_version = stale_snapshot["updated_at"]
    _wait_clock_past(stale_version)

    # A concurrent write bumps updated_at (and status) first.
    await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
    )

    applied = await db.update_status(
        "session",
        sid,
        new_status="completed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
        expected_statuses={"failed"},  # status membership alone would pass...
        expected_updated_at=stale_version,  # ...but the version guard catches it
    )
    assert applied is False

    row = await db.get_session(sid)
    assert row["status"] == "failed"  # unchanged by the losing writer


@pytest.mark.asyncio
async def test_terminal_overwrite_without_override_raises_not_returns_false(db: StateDB) -> None:
    """The OTHER update_status() contract: attempting to move a *terminal*
    entity to a different status, with no guard supplied at all and no
    override, is not a conflict/False — it is a raised
    TransitionRejectedError. Confusing this with the CAS-miss case above
    (e.g. treating a caught exception as an ordinary skip, or vice versa) is
    the exact bug class this test pins."""
    sid = await _make_session(db, status="completed")

    with pytest.raises(TransitionRejectedError) as exc_info:
        await db.update_status(
            "session",
            sid,
            new_status="running",
            reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
            source="admin",
        )

    assert exc_info.value.previous_status == "completed"
    assert exc_info.value.attempted_status == "running"
    row = await db.get_session(sid)
    assert row["status"] == "completed"  # untouched — the write never landed


@pytest.mark.asyncio
async def test_terminal_row_with_stale_guard_returns_false_not_raise(db: StateDB) -> None:
    """The two contracts *interact*: the exact same terminal row and the
    exact same attempted write raises TransitionRejectedError with no guard
    (previous test), but returns a silent False when the caller supplies an
    expected_statuses guard that the row's actual status fails — because the
    guard check runs before the terminal-exit check in
    SQLAlchemyLifecycleService._transition(). A caller relying on "terminal
    writes always raise" would be surprised here; this pins the real
    behavior so a future refactor can't silently swap which one fires."""
    sid = await _make_session(db, status="completed")

    applied = await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="system",
        expected_statuses={"running"},  # "completed" is not a member — guarded skip, not a raise
    )

    assert applied is False
    row = await db.get_session(sid)
    assert row["status"] == "completed"


# ── 4. Reaper pattern: version-guarded stale-row transition ─────────────────
# Mirrors lionagi.studio.services.lifecycle.reap_stale_plays()'s exact
# guarded-write shape (expected_statuses=<reapable set> +
# expected_updated_at=<snapshot>) directly against StateDB, so the guarantee
# is pinned at the storage layer the reaper depends on, independent of the
# reaper's own process-liveness plumbing.


@pytest.mark.asyncio
async def test_reaper_pattern_stale_row_is_reaped(db: StateDB) -> None:
    play_id = await _make_play(db, status="running")
    snapshot = await db.get_play(play_id)

    applied = await db.update_status(
        "play",
        play_id,
        new_status="blocked",
        reason_code=RunReasons.CANCELLED_STALE_AUTO,
        reason_summary="play_runner_dead_or_orphaned",
        source="system",
        actor="gate-test-reaper",
        expected_statuses=_REAPABLE_PLAY_STATUSES,
        expected_updated_at=snapshot["updated_at"],
    )

    assert applied is True
    row = await db.get_play(play_id)
    assert row["status"] == "blocked"


@pytest.mark.asyncio
async def test_reaper_pattern_row_claimed_between_read_and_write_is_not_clobbered(
    db: StateDB,
) -> None:
    """A play legitimately re-claimed (re-touched, updated_at bumped) between
    the reaper's scan and its guarded write must survive untouched — even
    though its status is STILL a member of the reapable set, since
    status-membership alone cannot distinguish "still stale" from "just
    re-touched". Dropping expected_updated_at from the guarded write would
    pass every status-only test and still over-reap this exact case."""
    play_id = await _make_play(db, status="running")
    snapshot = await db.get_play(play_id)
    _wait_clock_past(snapshot["updated_at"])

    # Simulate a legitimate concurrent claim: another writer refreshes the
    # row (status untouched, still "running") between the reaper's read and
    # its guarded write.
    await db.update_play(play_id, started_at=time.time())
    refreshed = await db.get_play(play_id)
    assert refreshed["updated_at"] != snapshot["updated_at"]
    assert refreshed["status"] == "running"  # still reapable by status alone

    applied = await db.update_status(
        "play",
        play_id,
        new_status="blocked",
        reason_code=RunReasons.CANCELLED_STALE_AUTO,
        reason_summary="play_runner_dead_or_orphaned",
        source="system",
        actor="gate-test-reaper",
        expected_statuses=_REAPABLE_PLAY_STATUSES,
        expected_updated_at=snapshot["updated_at"],  # the STALE snapshot value
    )

    assert applied is False
    row = await db.get_play(play_id)
    assert row["status"] == "running"  # untouched — the live claim survived
