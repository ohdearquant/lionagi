# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Observe a real scheduler's recorded chain outcome before and after child creation."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons, ScheduleReasons
from lionhub.orchestration.scheduler import engine as scheduler
from lionhub.orchestration.scheduler import subprocess as launcher
from lionhub.orchestration.scheduler.scheduler_state import (
    _DBSchedulerStateService,
    create_skipped_run,
)
from lionhub.serve.cli.monitor import (
    _advance_chains,
    _dispatch_wait,
    _new_chain_state,
    _poll_pending_once,
)
from tests.state.conftest import pg_url  # noqa: F401
from tests.state.test_chain_decision_readers import _isolated_postgres_store

from .test_scheduler_occurrence_atomicity import _schedule_row, _seed_dispatched_run_with_session

_SCHEDULE = "chain-schedule"
_PARENT = "chain-parent"
_CHILD_PROMPT = "run the follow-up"


@pytest_asyncio.fixture(params=["sqlite", "postgresql"], loop_scope="function")
async def chain_store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        path = tmp_path / "state.db"
        monkeypatch.setattr(
            db_mod,
            "settings",
            db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
        )
        monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
        async with StateDB(path) as db:
            yield db
        return

    # Recovery scans every dispatched run, so give it its own database even
    # when normal backend tests share a server or test worker.
    async with _isolated_postgres_store(request.getfixturevalue("pg_url"), monkeypatch) as db:
        yield db


async def _seed_session(db, invocation_id, status):
    progression = f"{invocation_id}-progression"
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": f"{invocation_id}-session",
            "progression_id": progression,
            "invocation_id": invocation_id,
            "status": status,
        }
    )


class _ObservedScheduler:
    def __init__(self, db, tmp_path, monkeypatch):
        self.db = db
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.svc = _DBSchedulerStateService(persistent=True)
        self.engine = scheduler.SchedulerEngine(svc=self.svc)
        self.pending = {_PARENT: {}}
        self.done = []
        self.monitor = _new_chain_state(self.pending, chain=True)
        self.processed = 0
        self.waited_before_child = None
        self.followed_child = None
        self.recorded_before_child = None
        self.launches = []
        monkeypatch.setattr(
            scheduler, "time", SimpleNamespace(time=lambda: 1001.0, monotonic=time.monotonic)
        )
        monkeypatch.setattr(launcher, "resolve_li_executable", lambda: (["/usr/bin/true"], None))

    async def tick(self):
        await _poll_pending_once(self.db, self.pending, {}, self.done)
        self.processed = await _advance_chains(
            self.db, self.pending, self.done, chain_state=self.monitor, processed=self.processed
        )

    async def children(self):
        return await self.db.fetch_all(
            "SELECT * FROM schedule_runs WHERE chain_parent_id = ?", (_PARENT,)
        )

    def observe_launches(
        self,
        *,
        session_status,
        child_exit=0,
        child_status="completed",
        remove_branch=None,
        stop_before_child=False,
    ):
        real_fire = self.engine._fire

        async def observe_fire(schedule, run_id, *, chain_depth=0, **kwargs):
            if chain_depth == 1:
                assert await self.children() == []
                if remove_branch is not None:
                    await self.db.update_schedule(_SCHEDULE, **{remove_branch: None})
                await self.tick()
                self.waited_before_child = _PARENT in self.monitor["awaiting_grace"]
                parent = await self.db.get_schedule_run(_PARENT)
                self.recorded_before_child = _decisions(parent)
                if stop_before_child:
                    raise RuntimeError("interrupted before recording the follow-up")
            return await real_fire(schedule, run_id, chain_depth=chain_depth, **kwargs)

        async def spawn_in_process(argv, invocation_id, *, on_launched, **_):
            await on_launched()
            if _CHILD_PROMPT in argv:
                self.launches.append("child")
                [child] = await self.children()
                assert child["dispatched_at"] is not None
                await self.tick()
                self.followed_child = child["id"] in self.pending
                await _seed_session(self.db, invocation_id, child_status)
                return child_exit, ""
            self.launches.append("parent")
            parent = await self.db.get_schedule_run(_PARENT)
            assert parent["dispatched_at"] is not None and parent["status"] == "running"
            await _seed_session(self.db, invocation_id, session_status)
            return 0, ""

        self.monkeypatch.setattr(self.engine, "_fire", observe_fire)
        self.monkeypatch.setattr(launcher, "spawn_and_wait", spawn_in_process)

    def schedule_fields(self, branch):
        fields = {"action_cwd": str(self.tmp_path)}
        if branch is not None:
            fields[branch] = {
                "kind": "agent",
                "prompt": _CHILD_PROMPT,
                "on_success": None,
                "on_fail": None,
            }
        return fields

    async def create_schedule(self, branch):
        await self.db.create_schedule(_schedule_row(_SCHEDULE, **self.schedule_fields(branch)))

    async def fire(self):
        schedule = await self.db.get_schedule(_SCHEDULE)
        return await asyncio.wait_for(
            self.engine._fire(
                schedule,
                _PARENT,
                trigger_context={},
                expect_next_fire_at=schedule["next_fire_at"],
            ),
            timeout=10,
        )

    async def recover(self, *, session_status, branch):
        await _seed_dispatched_run_with_session(
            self.db,
            sid=_SCHEDULE,
            run_id=_PARENT,
            inv_id="orphan-invocation",
            sess_id="orphan-session",
            session_status=session_status,
        )
        await self.db.update_schedule(_SCHEDULE, **self.schedule_fields(branch))
        await self.engine._reconcile_dispatched_orphans()
        if self.engine._fire_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(self.engine._fire_tasks)), timeout=10)

    async def wait_exit(self, *, chain=True):
        return await asyncio.to_thread(
            _dispatch_wait, [_PARENT], interval=0, follow=False, chain=chain, max_wait=10
        )

    async def assert_followed(self, *, launches, child_exit, child_status):
        await self.tick()
        assert self.launches == launches
        assert self.waited_before_child is True
        assert self.followed_child is True
        [child] = await self.children()
        assert child["exit_code"] == child_exit
        assert child["status"] == child_status
        assert self.monitor["resolved_roots"] == {_PARENT}
        assert self.monitor["chain_tail_status"][_PARENT] == child_status

    async def assert_resolved_without_child(self):
        await self.tick()
        assert await self.children() == []
        assert self.monitor["awaiting_grace"] == {}
        assert self.monitor["resolved_roots"] == {_PARENT}


@pytest_asyncio.fixture(loop_scope="function")
async def observed(chain_store, tmp_path, monkeypatch):
    scheduler = _ObservedScheduler(chain_store, tmp_path, monkeypatch)
    try:
        yield scheduler
    finally:
        await scheduler.svc.close()


def _decisions(row):
    refs = row.get("status_evidence_refs")
    if isinstance(refs, str):
        refs = json.loads(refs)
    return [ref for ref in refs or [] if ref["kind"] == "chain_decision"]


def _decision(ref):
    return [{"kind": "chain_decision", "v": 1, "ref": ref}]


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["on_fail", "on_success"])
async def test_completed_empty_follows_only_selected_failure_action(observed, branch):
    await observed.create_schedule(branch)
    observed.observe_launches(session_status="running", child_exit=0)

    assert await observed.fire()
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"]) == ("failed", 0)
    assert parent["status_reason_code"] == RunReasons.COMPLETED_EMPTY_NO_EVIDENCE
    if branch == "on_fail":
        await observed.assert_followed(
            launches=["parent", "child"], child_exit=0, child_status="completed"
        )
        assert await observed.wait_exit() == 0
        assert observed.recorded_before_child == _decision("on_fail")
        expected = "on_fail"
    else:
        await observed.assert_resolved_without_child()
        assert observed.launches == ["parent"]
        assert await observed.wait_exit() == 1
        expected = "none"
    assert _decisions(parent) == _decision(expected)
    invocation = await observed.db.get_invocation(parent["invocation_id"])
    assert _decisions(invocation) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True], ids=["unchanged", "removed-after-decision"])
async def test_successful_run_follows_the_selected_action(observed, edit):
    await observed.create_schedule("on_success")
    observed.observe_launches(
        session_status="completed",
        child_status="failed",
        remove_branch="on_success" if edit else None,
    )

    assert await observed.fire()
    await observed.assert_followed(
        launches=["parent", "child"], child_exit=0, child_status="failed"
    )
    # The child process exited cleanly, but its independently stored outcome failed.
    assert await observed.wait_exit() == 1
    assert await observed.wait_exit(chain=False) == 0
    assert observed.recorded_before_child == _decision("on_success")
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"]) == ("completed", 0)
    assert _decisions(parent) == _decision("on_success")
    if edit:
        schedule = await observed.db.get_schedule(_SCHEDULE)
        assert schedule["on_success"] is None


@pytest.mark.asyncio
async def test_dispatched_startup_recovery_follows_failure_without_exit_code(observed):
    observed.observe_launches(session_status="failed", child_exit=0)

    await observed.recover(session_status="failed", branch="on_fail")

    await observed.assert_followed(launches=["child"], child_exit=0, child_status="completed")
    assert await observed.wait_exit() == 0
    assert observed.recorded_before_child == _decision("on_fail")
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"]) == ("failed", None)
    assert _decisions(parent) == _decision("on_fail")
    invocation = await observed.db.get_invocation(parent["invocation_id"])
    assert _decisions(invocation) == []


@pytest.mark.asyncio
async def test_pre_dispatch_failure_resolves_without_waiting_for_failure_action(observed):
    await observed.create_schedule("on_fail")
    observed.observe_launches(session_status="failed")
    observed.monkeypatch.setattr(
        launcher, "resolve_li_executable", lambda: (None, "unavailable in this test")
    )

    assert not await observed.fire()
    await observed.assert_resolved_without_child()
    assert observed.launches == []
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"], parent["dispatched_at"]) == (
        "failed",
        None,
        None,
    )
    assert _decisions(parent) == _decision("none")


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True], ids=["normal", "startup"])
async def test_cancelled_run_follows_the_recorded_failure_action(observed, startup):
    observed.observe_launches(session_status="cancelled", child_exit=0)
    if startup:
        await observed.recover(session_status="cancelled", branch="on_fail")
    else:
        await observed.create_schedule("on_fail")
        assert await observed.fire()

    await observed.assert_followed(
        launches=["child"] if startup else ["parent", "child"],
        child_exit=0,
        child_status="completed",
    )
    assert await observed.wait_exit() == 0
    assert observed.recorded_before_child == _decision("on_fail")
    parent = await observed.db.get_schedule_run(_PARENT)
    assert parent["status"] == "cancelled"
    assert _decisions(parent) == _decision("on_fail")


@pytest.mark.asyncio
async def test_skipped_run_resolves_without_waiting_for_failure_action(observed):
    await observed.create_schedule("on_fail")
    schedule = await observed.db.get_schedule(_SCHEDULE)

    await create_skipped_run(
        observed.svc,
        run_id=_PARENT,
        schedule=schedule,
        trigger_context={},
        now=1001.0,
        reason_code=ScheduleReasons.SKIPPED_OVERLAP,
        reason_summary="Another occurrence already owns this schedule.",
        metadata={},
    )

    await observed.assert_resolved_without_child()
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"]) == ("skipped", None)
    assert _decisions(parent) == _decision("none")


@pytest.mark.asyncio
async def test_recorded_failure_action_expires_grace_when_child_does_not_start(observed):
    await observed.create_schedule("on_fail")
    observed.observe_launches(session_status="running", stop_before_child=True)

    await observed.fire()

    assert observed.waited_before_child is True
    assert observed.launches == ["parent"]
    assert _PARENT in observed.monitor["awaiting_grace"]
    assert observed.monitor["resolved_roots"] == set()
    await observed.assert_resolved_without_child()
    parent = await observed.db.get_schedule_run(_PARENT)
    assert (parent["status"], parent["exit_code"]) == ("failed", 0)
    assert _decisions(parent) == _decision("on_fail")
    assert observed.recorded_before_child == _decision("on_fail")
    assert observed.monitor["chain_tail_status"][_PARENT] == "failed"
    assert await observed.wait_exit() == 1
