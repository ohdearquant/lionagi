"""A chained action retains its stored kind spelling in run records."""

from unittest.mock import AsyncMock, patch

import pytest

from lionagi.state.db import NO_CURSOR_CLAIM, StateDB
from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService
from tests.studio.test_scheduler_occurrence_atomicity import _schedule_row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain,expected_depth",
    [
        ({"kind": "playbook", "playbook": "triage"}, 1),
        ({"action_kind": "playbook", "playbook": "triage"}, 1),
        (
            {
                "kind": "playbook",
                "playbook": "triage",
                "on_success": {"prompt": "next", "on_success": None, "on_fail": None},
            },
            2,
        ),
    ],
    ids=["kind-alias", "action-kind-alias", "inherited-alias"],
)
async def test_chained_playbook_alias_launch_preserves_record_kind(
    tmp_path, monkeypatch, chain, expected_depth
):
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    chain = {"on_success": None, "on_fail": None, **chain}
    row = _schedule_row(
        "sched-alias",
        trigger_type="interval",
        interval_sec=60,
        action_cwd=str(tmp_path),
        action_kind="agent",
        on_success=chain,
    )
    async with StateDB(db_path) as db:
        await db.create_schedule(row)
        schedule = await db.get_schedule(row["id"])

    launches = []

    async def spawn(argv, *args, on_launched=None, **kwargs):
        launches.append(argv)
        if on_launched is not None:
            await on_launched()
        return 0, ""

    engine = SchedulerEngine(svc=_DBSchedulerStateService())
    with (
        patch(
            "lionagi.studio.scheduler.subprocess.resolve_li_executable", return_value=(["li"], None)
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
        patch(
            "lionagi.studio.scheduler.engine.resolve_invocation_terminal",
            new=AsyncMock(return_value=("completed", RunReasons.COMPLETED_OK, "summary", [], {})),
        ),
    ):
        await engine._fire(
            schedule, "parent-run", trigger_context={}, expect_next_fire_at=NO_CURSOR_CLAIM
        )

    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(row["id"])
        child = next(run for run in runs if run.get("chain_depth") == expected_depth)
        invocation = await db.get_invocation(child["invocation_id"])

    assert len(launches) == expected_depth + 1
    assert launches[-1][:3] == ["li", "play", "triage"]
    assert child["action_kind"] == "playbook"
    assert invocation["node_metadata"]["action_kind"] == "playbook"
