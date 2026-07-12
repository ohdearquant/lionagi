# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0071 D4: capability-class matching in the claim loop.

Covers the workers-registry heartbeat upsert, the subset-match claim rule
(eligibility∪serialization tokens), execution_target matching (including the
NULL/empty = claimable-by-any case), heartbeat-TTL claim eligibility (and its
non-interference with lease-expiry recovery), serialization-class
concurrency admission, affinity-class candidate ordering, cross-dialect JSON
normalization (SQLite string vs. Postgres native value), and the keyset-paged
claim scan (a persistent prefix of ineligible older rows must never hide a
later eligible row). D3's original claim-race/lease/vocabulary tests live
untouched in test_task_worker.py.
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler.worker import (
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    _matching_candidates,
    _normalize_json_list,
    claim_and_execute,
    reap_expired_leases,
    register_heartbeat,
    worker_tick,
)
from lionagi.studio.services.task_applications import TaskApplication, submit_task


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _submit_task(db: StateDB, **overrides) -> str:
    kwargs = {"action_kind": "agent", "args": {}, "execution_target": "host"}
    kwargs.update(overrides)
    app = TaskApplication(**kwargs)
    return await submit_task(db, app)


async def _noop_execute(row: dict) -> tuple[int, str]:
    return 0, ""


async def _status_of(db: StateDB, run_id: str) -> dict:
    return await db.fetch_one("SELECT status, leased_by FROM schedule_runs WHERE id = ?", (run_id,))


# ── 1. Heartbeat registry upsert ─────────────────────────────────────────


async def test_register_heartbeat_writes_a_worker_row(db: StateDB) -> None:
    now = time.time()
    await register_heartbeat(
        db,
        worker_id="w1",
        advertised_capabilities=["lean-toolchain"],
        execution_targets=["host"],
        now=now,
    )
    row = await db.fetch_one("SELECT * FROM workers WHERE worker_id = ?", ("w1",))
    assert row is not None
    assert json.loads(row["advertised_capabilities"]) == ["lean-toolchain"]
    assert json.loads(row["execution_targets"]) == ["host"]
    assert row["last_heartbeat_at"] == now


async def test_register_heartbeat_upserts_not_duplicates(db: StateDB) -> None:
    now = time.time()
    await register_heartbeat(db, worker_id="w1", advertised_capabilities=["a"], now=now)
    later = now + 30
    await register_heartbeat(db, worker_id="w1", advertised_capabilities=["b"], now=later)

    rows = await db.fetch_all("SELECT * FROM workers WHERE worker_id = ?", ("w1",))
    assert len(rows) == 1
    assert json.loads(rows[0]["advertised_capabilities"]) == ["b"]
    assert rows[0]["last_heartbeat_at"] == later


# ── 2. Subset-match claim rule ───────────────────────────────────────────


async def test_matching_capability_worker_claims(db: StateDB) -> None:
    run_id = await _submit_task(db, required_capabilities=["lean-toolchain"])
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["lean-toolchain"]
    )
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"
    assert row["leased_by"] == "w1"


async def test_missing_one_required_token_not_claimed(db: StateDB) -> None:
    run_id = await _submit_task(db, required_capabilities=["lean-toolchain", "gpu-exclusive"])
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["lean-toolchain"]
    )
    assert claimed == 0
    row = await _status_of(db, run_id)
    assert row["status"] == "queued"
    assert row["leased_by"] is None


async def test_extra_advertised_capabilities_still_claims(db: StateDB) -> None:
    run_id = await _submit_task(db, required_capabilities=["lean-toolchain"])
    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_noop_execute,
        advertised_capabilities=["lean-toolchain", "unrelated-token"],
    )
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


# ── 3. Execution-target matching ─────────────────────────────────────────


async def test_execution_target_mismatch_not_claimed(db: StateDB) -> None:
    run_id = await _submit_task(db, execution_target="local_worktree")
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, execution_targets=["host"]
    )
    assert claimed == 0
    row = await _status_of(db, run_id)
    assert row["status"] == "queued"


async def test_execution_target_in_worker_set_claims(db: StateDB) -> None:
    run_id = await _submit_task(db, execution_target="local_worktree")
    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_noop_execute,
        execution_targets=["host", "local_worktree"],
    )
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


async def test_null_execution_target_claimable_by_any_worker(db: StateDB) -> None:
    run_id = await _submit_task(db)
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE schedule_runs SET execution_target = NULL WHERE id = :id"),
            {"id": run_id},
        )
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, execution_targets=["daytona"]
    )
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


# ── 4. Heartbeat-TTL claim eligibility ───────────────────────────────────


async def test_stale_heartbeat_worker_skipped_for_new_claims(db: StateDB) -> None:
    run_id = await _submit_task(db)
    now = time.time()
    await register_heartbeat(db, worker_id="w1", now=now - (DEFAULT_HEARTBEAT_TTL_SECONDS + 1))

    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute, now=now)
    assert claimed == 0
    row = await _status_of(db, run_id)
    assert row["status"] == "queued"
    assert row["leased_by"] is None


async def test_fresh_heartbeat_worker_claims(db: StateDB) -> None:
    run_id = await _submit_task(db)
    now = time.time()
    await register_heartbeat(db, worker_id="w1", now=now)

    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute, now=now)
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


async def test_never_heartbeated_worker_is_not_stale(db: StateDB) -> None:
    """A worker with no `workers` row (e.g. every D3-era caller) is not
    treated as stale -- there is no signal to distrust."""
    run_id = await _submit_task(db)
    claimed = await claim_and_execute(db, worker_id="never-heartbeated", execute=_noop_execute)
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


async def test_worker_tick_heartbeats_before_claiming_so_never_self_stale(db: StateDB) -> None:
    run_id = await _submit_task(db)
    counts = await worker_tick(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=[]
    )
    assert counts["claimed"] == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


async def test_stale_workers_inflight_lease_still_recovers_via_reap(db: StateDB) -> None:
    """Heartbeat TTL gates NEW claims only: an in-flight lease belonging to
    a now-stale worker still recovers through the unchanged
    lease_expires_at reaper."""
    run_id = await _submit_task(db)
    now = time.time()
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code="run.started.ok"),
            actor=Actor(type="system", id="w1"),
            idempotency_key=f"claim:{run_id}",
        ),
        patch={"leased_by": "w1", "lease_expires_at": now - 10, "lease_attempts": 1},
    )
    assert result.applied is True

    # w1's heartbeat goes stale while its lease is still recorded as running.
    await register_heartbeat(db, worker_id="w1", now=now - (DEFAULT_HEARTBEAT_TTL_SECONDS + 500))

    counts = await reap_expired_leases(db, now=now)
    assert counts == {"requeued": 1, "failed": 0}
    row = await _status_of(db, run_id)
    assert row["status"] == "queued"
    assert row["leased_by"] is None


# ── 5. Serialization-class concurrency admission ─────────────────────────


async def test_serialization_tasks_share_concurrency_key(db: StateDB) -> None:
    run_id_1 = await _submit_task(db, required_capabilities=["gpu-exclusive"])
    run_id_2 = await _submit_task(db, required_capabilities=["gpu-exclusive"])
    row1 = await db.fetch_one("SELECT concurrency_key FROM schedule_runs WHERE id = ?", (run_id_1,))
    row2 = await db.fetch_one("SELECT concurrency_key FROM schedule_runs WHERE id = ?", (run_id_2,))
    assert row1["concurrency_key"] is not None
    assert row1["concurrency_key"] == row2["concurrency_key"]


async def test_second_serialization_task_blocked_while_first_is_running(db: StateDB) -> None:
    run_id_1 = await _submit_task(db, required_capabilities=["gpu-exclusive"])
    run_id_2 = await _submit_task(db, required_capabilities=["gpu-exclusive"])

    # Simulate run_id_1 already claimed and running (a prior, still in-flight
    # execution) so the new claim pass must see it via the running-keys check.
    now = time.time()
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id_1,
            from_state="queued",
            to_state="running",
            reason=StateReason(code="run.started.ok"),
            actor=Actor(type="system", id="w0"),
            idempotency_key=f"claim:{run_id_1}",
        ),
        patch={"leased_by": "w0", "lease_expires_at": now + 300, "lease_attempts": 1},
    )
    assert result.applied is True

    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["gpu-exclusive"]
    )
    assert claimed == 0
    row2 = await _status_of(db, run_id_2)
    assert row2["status"] == "queued"


async def test_two_serialization_tasks_never_both_running_in_one_pass(db: StateDB) -> None:
    """Within a single claim_and_execute pass, the second gpu-exclusive row
    is skipped even though the first already finished by the time the
    second is considered -- the admission check keys off the pass, not just
    the DB's live 'running' snapshot, so two same-key rows are never
    concurrently in flight."""
    run_id_1 = await _submit_task(db, required_capabilities=["gpu-exclusive"])
    run_id_2 = await _submit_task(db, required_capabilities=["gpu-exclusive"])

    executed: list[str] = []

    async def _record(row: dict) -> tuple[int, str]:
        executed.append(row["id"])
        return 0, ""

    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_record, advertised_capabilities=["gpu-exclusive"]
    )
    assert claimed == 1
    assert executed == [run_id_1]
    row2 = await _status_of(db, run_id_2)
    assert row2["status"] == "queued"

    # A later pass, once T1 is terminal, claims T2 cleanly.
    claimed_2 = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["gpu-exclusive"]
    )
    assert claimed_2 == 1
    row2b = await _status_of(db, run_id_2)
    assert row2b["status"] == "completed"


async def test_eligibility_and_affinity_tasks_never_serialize(db: StateDB) -> None:
    """Non-serialization tokens don't get a concurrency_key at all, so two
    eligibility/affinity-only tasks claim and run in the same pass."""
    run_id_1 = await _submit_task(db, required_capabilities=["lean-toolchain"])
    run_id_2 = await _submit_task(db, required_capabilities=["warmed-cache"])

    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_noop_execute,
        advertised_capabilities=["lean-toolchain", "warmed-cache"],
    )
    assert claimed == 2
    assert (await _status_of(db, run_id_1))["status"] == "completed"
    assert (await _status_of(db, run_id_2))["status"] == "completed"


# ── 6. Affinity-class ordering ───────────────────────────────────────────


async def test_affinity_matched_task_preferred_over_earlier_plain_task(db: StateDB) -> None:
    await _submit_task(db)  # plain, queued first
    run_id_affinity = await _submit_task(db, required_capabilities=["warmed-cache"])

    executed: list[str] = []

    async def _record(row: dict) -> tuple[int, str]:
        executed.append(row["id"])
        return 0, ""

    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_record,
        limit=1,
        advertised_capabilities=["warmed-cache"],
    )
    assert claimed == 1
    assert executed == [run_id_affinity]


async def test_non_affinity_worker_still_claims_when_sole_eligible(db: StateDB) -> None:
    """Affinity tokens never filter: a worker not advertising the affinity
    token still claims the task when it is the only eligible worker."""
    run_id = await _submit_task(db, required_capabilities=["warmed-cache"])
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=[]
    )
    assert claimed == 1
    row = await _status_of(db, run_id)
    assert row["status"] == "completed"


async def test_affinity_ordering_does_not_starve_plain_tasks(db: StateDB) -> None:
    """With enough room for both, affinity preference reorders but never
    drops the plain task -- both are claimed within one pass."""
    run_id_plain = await _submit_task(db)
    run_id_affinity = await _submit_task(db, required_capabilities=["warmed-cache"])

    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["warmed-cache"]
    )
    assert claimed == 2
    assert (await _status_of(db, run_id_plain))["status"] == "completed"
    assert (await _status_of(db, run_id_affinity))["status"] == "completed"


# ── 7. No eligible worker => stays queued ────────────────────────────────


async def test_no_eligible_worker_task_remains_queued_across_ticks(db: StateDB) -> None:
    run_id = await _submit_task(db, required_capabilities=["lean-toolchain"])
    for _ in range(3):
        counts = await worker_tick(
            db, worker_id="w1", execute=_noop_execute, advertised_capabilities=[]
        )
        assert counts["claimed"] == 0
    row = await _status_of(db, run_id)
    assert row["status"] == "queued"
    assert row["leased_by"] is None


# ── 8. Cross-dialect JSON normalization ──────────────────────────────────


def test_normalize_json_list_null_and_empty():
    assert _normalize_json_list(None) == []
    assert _normalize_json_list("") == []
    assert _normalize_json_list("[]") == []
    assert _normalize_json_list([]) == []


def test_normalize_json_list_sqlite_shape_is_a_string():
    assert _normalize_json_list('["lean-toolchain", "gpu-exclusive"]') == [
        "lean-toolchain",
        "gpu-exclusive",
    ]


def test_normalize_json_list_postgres_shape_is_a_native_list():
    # asyncpg + SQLAlchemy's JSON type deserialize Postgres JSON columns to
    # native Python values before the row ever reaches this module -- no
    # json.loads() is needed, and calling it on a list raises TypeError.
    assert _normalize_json_list(["lean-toolchain", "gpu-exclusive"]) == [
        "lean-toolchain",
        "gpu-exclusive",
    ]


def test_matching_candidates_accepts_native_list_row_shape():
    """Feeds a Postgres-shaped row (required_capabilities already a native
    list, not a JSON string) through the exact matching path
    claim_and_execute uses, proving it never calls json.loads() on it."""
    pg_shaped_row = {
        "id": "run-1",
        "required_capabilities": ["lean-toolchain"],  # native list, not a string
        "execution_target": "host",
        "concurrency_key": None,
        "queued_at": 1.0,
    }
    matched = _matching_candidates([pg_shaped_row], advertised=["lean-toolchain"], targets={"host"})
    assert len(matched) == 1
    assert matched[0][0] is pg_shaped_row
    assert matched[0][1] == ["lean-toolchain"]


def test_matching_candidates_accepts_sqlite_shaped_row_too():
    sqlite_shaped_row = {
        "id": "run-2",
        "required_capabilities": '["lean-toolchain"]',  # JSON string
        "execution_target": "host",
        "concurrency_key": None,
        "queued_at": 1.0,
    }
    matched = _matching_candidates(
        [sqlite_shaped_row], advertised=["lean-toolchain"], targets={"host"}
    )
    assert len(matched) == 1
    assert matched[0][1] == ["lean-toolchain"]


def test_matching_candidates_native_null_required_capabilities():
    # Postgres represents an absent JSON value as Python None, not "null".
    row = {
        "id": "run-3",
        "required_capabilities": None,
        "execution_target": "host",
        "concurrency_key": None,
        "queued_at": 1.0,
    }
    matched = _matching_candidates([row], advertised=[], targets={"host"})
    assert len(matched) == 1
    assert matched[0][1] == []


async def test_sqlite_backed_claim_still_works_after_normalization_change(db: StateDB) -> None:
    """Confirms claim_and_execute's real SQLite-backed query path (where
    required_capabilities genuinely comes back as a JSON string) still
    claims correctly after routing through _normalize_json_list -- the
    normalization guard must not regress the already-covered SQLite shape."""
    run_id = await _submit_task(db, required_capabilities=["lean-toolchain"])
    row = await db.fetch_one(
        "SELECT required_capabilities FROM schedule_runs WHERE id = ?", (run_id,)
    )
    assert isinstance(row["required_capabilities"], str)
    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["lean-toolchain"]
    )
    assert claimed == 1
    assert (await _status_of(db, run_id))["status"] == "completed"


# ── 9. Keyset-paged claim scan (no starvation behind a long ineligible
#      prefix) ────────────────────────────────────────────────────────────


async def test_eligible_row_behind_a_long_ineligible_prefix_is_still_claimed(
    db: StateDB,
) -> None:
    """A fixed-size prefetch window would hide this row forever: submit more
    non-matching rows than any single fixed window would have covered, then
    one matching row after them, and confirm the paged scan still finds it."""
    for _ in range(120):
        await _submit_task(db, required_capabilities=["other-toolchain"])
    run_id_match = await _submit_task(db, required_capabilities=["lean-toolchain"])

    claimed = await claim_and_execute(
        db, worker_id="w1", execute=_noop_execute, advertised_capabilities=["lean-toolchain"]
    )
    assert claimed == 1
    row = await _status_of(db, run_id_match)
    assert row["status"] == "completed"
    assert row["leased_by"] == "w1"


async def test_eligible_row_behind_long_prefix_claimed_with_small_limit(db: StateDB) -> None:
    """Same shape with a small `limit`, exceeding the page size
    (`_CLAIM_PAGE_SIZE`) rather than the claim limit itself."""
    for _ in range(60):
        await _submit_task(db, required_capabilities=["other-toolchain"])
    run_id_match = await _submit_task(db, required_capabilities=["lean-toolchain"])

    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_noop_execute,
        advertised_capabilities=["lean-toolchain"],
        limit=10,
    )
    assert claimed == 1
    row = await _status_of(db, run_id_match)
    assert row["status"] == "completed"
