# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settled GitHub poll accounting uses the current stored schedule row."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService
from lionagi.studio.services.schedules import compute_schedule_health

_COUNTER = "poller_consecutive_cap_no_dispatch"
_LIMIT = "poller_last_capped_page_limit"


def _schedule(schedule_id="github", *, trigger_type="github_poll"):
    return {
        "id": schedule_id,
        "name": schedule_id,
        "trigger_type": trigger_type,
        "github_repo": "example/repository" if trigger_type == "github_poll" else None,
        "interval_sec": 60 if trigger_type == "interval" else None,
        "action_kind": "agent",
        "created_at": 10.0,
        "updated_at": 10.0,
    }


async def test_stagnation_defaults_to_zero_and_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        await db.create_schedule(_schedule())
        await db.create_schedule(_schedule("interval", trigger_type="interval"))
        assert {row["id"]: row[_COUNTER] for row in await db.list_schedules()} == {
            "github": 0,
            "interval": 0,
        }
        assert await db.record_github_poll_stagnation("github", reset=False) is True
        assert await db.record_github_poll_stagnation("github", reset=False) is True

    async with StateDB(path) as reopened:
        assert (await reopened.get_schedule("github"))[_COUNTER] == 2
        assert (await reopened.get_schedule("interval"))[_COUNTER] == 0


async def test_old_schedule_store_migrates_with_unknown_last_capped_limit(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        await db.create_schedule({**_schedule(), "github_filter": {"event": "pr_merged"}})
        for _ in range(3):
            await db.record_github_poll_stagnation("github", reset=False)
        before = compute_schedule_health(await db.get_schedule("github"), {}, now=100.0)
        version = await db.schema_version()
        await db.execute(f"ALTER TABLE schedules DROP COLUMN {_LIMIT}")
        await db.execute("ALTER TABLE schedules DROP COLUMN poller_target_generation")
    async with StateDB(path) as reopened:
        row = await reopened.get_schedule("github")
        assert await reopened.schema_version() == version
        assert row[_COUNTER] == 3 and row[_LIMIT] is None
        assert row["poller_target_generation"] == 0
        assert compute_schedule_health(row, {}, now=100.0) == before


async def test_last_capped_limit_and_counter_commit_together(tmp_path):
    async with StateDB(tmp_path / "state.db") as first, StateDB(tmp_path / "state.db") as second:
        await first.create_schedule(_schedule())
        await first.record_github_poll_stagnation("github", reset=False, page_limit=5)
        row = await second.get_schedule("github")
        assert (row[_COUNTER], row[_LIMIT]) == (1, 5)
        await second.record_github_poll_stagnation("github", reset=False, page_limit=100)
        row = await first.get_schedule("github")
        assert (row[_COUNTER], row[_LIMIT]) == (2, 100)
        await first.record_github_poll_stagnation("github", reset=True)
        row = await second.get_schedule("github")
        assert (row[_COUNTER], row[_LIMIT]) == (0, None)


async def test_postgres_capped_limit_is_integer_and_reset_clears_it(pg_url):
    schedule_id = f"github-{uuid.uuid4()}"
    async with StateDB(url=pg_url) as db:
        assert db.dialect == "postgresql"
        await db.create_schedule(_schedule(schedule_id))
        try:
            assert await db.record_github_poll_stagnation(schedule_id, reset=False, page_limit=5)
            assert (await db.get_schedule(schedule_id))[_LIMIT] == 5
            assert await db.record_github_poll_stagnation(schedule_id, reset=True, page_limit=5)
            assert (await db.get_schedule(schedule_id))[_LIMIT] is None
        finally:
            await db.delete_schedule(schedule_id)


async def test_merged_page_limit_is_validated_and_survives_reopen(tmp_path):
    from lionagi.studio.scheduler.github import merged_page_limit
    from lionagi.studio.services.schedules import _svc_validate_github_filter

    config = {"event": "pr_merged", "merged_page_limit": 20}
    _svc_validate_github_filter(config)
    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        await db.create_schedule({**_schedule(), "github_filter": config})
    async with StateDB(path) as db:
        assert (await db.get_schedule("github"))["github_filter"] == config
        reset = {"event": "pr_merged", "merged_page_limit": None}
        _svc_validate_github_filter(reset)
        await db.update_schedule("github", github_filter=reset)
    async with StateDB(path) as db:
        saved = (await db.get_schedule("github"))["github_filter"]
        assert saved == reset
        assert merged_page_limit(saved["merged_page_limit"]) == 5


async def test_stagnation_uses_current_committed_value_across_connections(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        await first.create_schedule(_schedule())
        for _ in range(2):
            assert await first.record_github_poll_stagnation("github", reset=False)
        first_snapshot = await first.get_schedule("github")
        second_snapshot = await second.get_schedule("github")
        assert first_snapshot[_COUNTER] == second_snapshot[_COUNTER] == 2

        assert await first.record_github_poll_stagnation("github", reset=False)
        assert (await second.get_schedule("github"))[_COUNTER] == 3
        assert await second.record_github_poll_stagnation("github", reset=False)
        assert (await first.get_schedule("github"))[_COUNTER] == 4
        assert await first.record_github_poll_stagnation("github", reset=True)
        assert (await second.get_schedule("github"))[_COUNTER] == 0
        assert await second.record_github_poll_stagnation("github", reset=False)
        assert (await first.get_schedule("github"))[_COUNTER] == 1
        assert first_snapshot[_COUNTER] == second_snapshot[_COUNTER] == 2


async def test_accounting_only_changes_its_counter_and_existing_update_stamp(tmp_path, monkeypatch):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(_schedule())
        await db.create_schedule(_schedule("other"))
        await db.update_schedule(
            "github",
            github_cursor="2026-10-23T00:00:00Z",
            last_healthy_poll_at=100.0,
            poller_consecutive_401=7,
            last_fired_at=80.0,
            next_fire_at=200.0,
        )
        before = await db.get_schedule("github")
        other = await db.get_schedule("other")
        history = await db.get_schedule_history("github")
        assert len(history["events"]) == 1
        with monkeypatch.context() as clock:
            clock.setattr(db_mod.time, "time", lambda: 1234.0)
            assert await db.record_github_poll_stagnation("github", reset=False)
        after = await db.get_schedule("github")
        assert after == {**before, _COUNTER: 1, "updated_at": 1234.0}
        assert await db.get_schedule("other") == other
        assert await db.get_schedule_history("github") == history


@pytest.mark.parametrize("reset", [False, True], ids=["increment", "reset"])
async def test_accounting_refuses_deleted_and_changed_type_rows(tmp_path, reset):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(_schedule("interval", trigger_type="interval"))
        await db.create_schedule(_schedule("changed"))
        assert await db.record_github_poll_stagnation("changed", reset=False, page_limit=5)
        before_change = await db.get_schedule("changed")
        assert (before_change[_COUNTER], before_change[_LIMIT]) == (1, 5)
        assert await db.update_schedule("changed", trigger_type="interval", interval_sec=60)
        await db.create_schedule(_schedule("deleted"))
        assert await db.delete_schedule("deleted")
        interval = await db.get_schedule("interval")
        changed = await db.get_schedule("changed")
        assert (changed[_COUNTER], changed[_LIMIT], changed["poller_target_generation"]) == (
            0,
            None,
            1,
        )
        for schedule_id in ("interval", "changed", "deleted", "missing"):
            assert await db.record_github_poll_stagnation(schedule_id, reset=reset) is False
        assert await db.get_schedule("interval") == interval
        assert await db.get_schedule("changed") == changed
        assert await db.get_schedule("deleted") is None
        assert await db.get_schedule("missing") is None


@pytest.mark.parametrize("reset", [False, True], ids=["increment", "reset"])
async def test_failed_accounting_rolls_back_counter_and_update_stamp(tmp_path, reset):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(_schedule())
        for _ in range(2):
            assert await db.record_github_poll_stagnation("github", reset=False)
        before = await db.get_schedule("github")
        history = await db.get_schedule_history("github")
        await db.execute(
            "CREATE TRIGGER refuse_stagnation BEFORE UPDATE OF "
            "poller_consecutive_cap_no_dispatch ON schedules "
            "BEGIN SELECT RAISE(ABORT, 'accounting refused'); END"
        )
        with pytest.raises(IntegrityError, match="accounting refused"):
            await db.record_github_poll_stagnation("github", reset=reset)
        assert await db.get_schedule("github") == before
        assert await db.get_schedule_history("github") == history


async def test_stagnation_is_not_an_authored_schedule_update(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(_schedule())
        before = await db.get_schedule("github")
        for field in (_COUNTER, _LIMIT):
            with pytest.raises(ValueError, match="Invalid schedule field"):
                await db.update_schedule("github", **{field: 99})
        assert await db.get_schedule("github") == before


@pytest.mark.parametrize("persistent", [False, True], ids=["per-call", "persistent"])
async def test_scheduler_port_records_increment_reset_and_unmatched_rows(
    tmp_path, monkeypatch, persistent
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(_schedule())
        await db.create_schedule(_schedule("interval", trigger_type="interval"))
    service = _DBSchedulerStateService(persistent=persistent)
    try:
        for expected in (1, 2):
            assert await service.record_github_poll_stagnation("github", reset=False) is True
            assert (await service.get_schedule("github"))[_COUNTER] == expected
        assert await service.record_github_poll_stagnation("github", reset=True) is True
        assert (await service.get_schedule("github"))[_COUNTER] == 0
        assert await service.record_github_poll_stagnation("github", reset=True) is True
        assert await service.record_github_poll_stagnation("github", reset=False) is True
        assert (await service.get_schedule("github"))[_COUNTER] == 1
        assert await service.record_github_poll_stagnation("interval", reset=False) is False
        assert await service.record_github_poll_stagnation("missing", reset=True) is False
        assert (await service.get_schedule("interval"))[_COUNTER] == 0
        assert await service.get_schedule("missing") is None
    finally:
        await service.close()
