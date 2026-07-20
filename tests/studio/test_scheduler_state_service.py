"""Integration tests for _DBSchedulerStateService against a real StateDB.

The scheduler engine reaches StateDB only through this service layer, so the
service's own parameter defaults — not StateDB's — decide what a call without
overrides observes. These tests pin that the two default surfaces agree.
"""

import pytest

from lionagi.state import db as db_mod
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

pytestmark = pytest.mark.asyncio


async def _seed_schedule_with_runs(path: str, statuses: list[str]) -> str:
    sid = "sched-svc-1"
    state = db_mod.StateDB(path)
    await state.open()
    await state.create_schedule(
        {
            "id": sid,
            "name": "svc-count-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    for i, status in enumerate(statuses):
        await state.create_schedule_run(
            {
                "id": f"run-{i}",
                "schedule_id": sid,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": status,
                "chain_depth": 0,
                "fired_at": 1.0,
            }
        )
    await state.close()
    return sid


async def test_service_default_counts_timed_out(tmp_path, monkeypatch):
    """A call with no statuses override must count timed_out runs: a reaped
    run fired and did real work, so a one-shot whose only run timed out must
    be seen as having consumed its budget (and get auto-disabled by the
    engine's post-run check, which uses exactly this default)."""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    sid = await _seed_schedule_with_runs(db_path, ["timed_out"])

    svc = _DBSchedulerStateService()
    assert await svc.count_schedule_runs(sid, chain_depth=0) == 1


async def test_service_default_matches_statedb_default(tmp_path, monkeypatch):
    """The service-layer default tuple must observe the same rows as
    StateDB's own default: terminal statuses including timed_out, excluding
    skipped and running."""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    sid = await _seed_schedule_with_runs(
        db_path, ["completed", "failed", "cancelled", "timed_out", "skipped", "running"]
    )

    svc = _DBSchedulerStateService()
    via_service = await svc.count_schedule_runs(sid, chain_depth=0)

    state = db_mod.StateDB(db_path)
    await state.open()
    via_statedb = await state.count_schedule_runs(sid, chain_depth=0)
    await state.close()

    assert via_service == via_statedb == 4
