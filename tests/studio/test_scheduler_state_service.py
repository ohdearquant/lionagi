"""Integration tests for _DBSchedulerStateService against a real StateDB.

The scheduler engine reaches StateDB only through this service layer, so the
service's own parameter defaults — not StateDB's — decide what a call without
overrides observes. These tests pin that the two default surfaces agree.
"""

import pytest

from lionagi.state import db as db_mod
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

pytestmark = pytest.mark.asyncio


async def _seed_schedule_with_runs(path: str, statuses: list[str], sid: str = "sched-svc-1") -> str:
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


async def test_service_lists_every_enabled_schedule_past_public_page_limit(tmp_path, monkeypatch):
    """The scheduler's internal scan must not inherit the public 100-row page.

    A schedule omitted here is never evaluated by the tick loop, so the
    service contract is deliberately complete even though StateDB's public
    list default remains bounded.
    """
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    async with db_mod.StateDB(db_path) as state:
        for index in range(101):
            await state.create_schedule(
                {
                    "id": f"sched-{index:03d}",
                    "name": f"schedule-{index:03d}",
                    "trigger_type": "interval",
                    "interval_sec": 60,
                    "action_kind": "agent",
                    "enabled": True,
                }
            )

    rows = await _DBSchedulerStateService().list_schedules(enabled=True)

    assert len(rows) == 101
    assert {row["id"] for row in rows} == {f"sched-{index:03d}" for index in range(101)}


async def test_persistent_service_reuses_one_open_database(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    sid = await _seed_schedule_with_runs(db_path, [])

    open_count = 0
    original_open = db_mod.StateDB.open

    async def tracking_open(db):
        nonlocal open_count
        open_count += 1
        await original_open(db)

    monkeypatch.setattr(db_mod.StateDB, "open", tracking_open)
    svc = _DBSchedulerStateService(persistent=True)
    try:
        assert (await svc.get_schedule(sid))["id"] == sid
        assert len(await svc.list_schedules()) == 1
    finally:
        await svc.close()

    assert open_count == 1


async def test_persistent_service_follows_a_redirected_store(tmp_path, monkeypatch):
    """The connection outlives any single call, so it has to be keyed to the
    store it was opened against. Bound once and never re-resolved, it answers
    every later read and write from the first database while both are real."""
    first = str(tmp_path / "first.db")
    second = str(tmp_path / "second.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", first)
    await _seed_schedule_with_runs(first, [], sid="sched-first")

    svc = _DBSchedulerStateService(persistent=True)
    try:
        assert (await svc.get_schedule("sched-first"))["id"] == "sched-first"

        monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", second)
        await _seed_schedule_with_runs(second, [], sid="sched-second")

        assert await svc.get_schedule("sched-first") is None
        assert (await svc.get_schedule("sched-second"))["id"] == "sched-second"
    finally:
        await svc.close()


async def test_production_scheduler_uses_and_closes_persistent_state(tmp_path, monkeypatch):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    svc = _DBSchedulerStateService(persistent=True)
    engine = SchedulerEngine(svc=svc)

    await svc.open()
    assert svc._db is not None

    await engine.stop()

    assert svc._db is None
