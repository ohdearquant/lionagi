# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lionagi.studio.scheduler.coordination.compute_files_overlap()
and lionagi.studio.services.scheduler_state.flush_run_telemetry().

Covers:
  * Overlap fixture: two workers reading one shared + N distinct files ->
    count=1, top-1 with the correct worker count.
  * Zero-worker / zero-overlap invocations report {"count": 0, "top": []},
    never an error.
  * top_n truncation and deterministic tie-breaking.
  * Terminal flush: telemetry lands exactly once per run (pop_run_counters
    is consumed, node_metadata["coordination"] merges rather than clobbers
    pre-existing node_metadata, and a run with nothing to report leaves
    node_metadata untouched).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.scheduler.coordination import compute_files_overlap
from lionagi.studio.scheduler.signals import SchedulerSignalBus, ScheduleRunSucceeded


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def _make_invocation(db: StateDB, invocation_id: str) -> None:
    await db.create_invocation({"id": invocation_id, "skill": "test", "started_at": time.time()})


async def _make_worker_session(
    db: StateDB,
    *,
    invocation_id: str,
    session_id: str,
    branch_id: str,
    file_paths: list[str],
) -> None:
    """One session ("worker") with one branch whose progression carries one
    ActionRequest message per path in *file_paths*."""
    session_prog = f"{session_id}-prog"
    await db.create_progression(session_prog)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": session_prog,
            "invocation_id": invocation_id,
            "status": "completed",
        }
    )

    branch_prog = f"{branch_id}-prog"
    msg_ids = [f"{branch_id}-msg-{i}" for i in range(len(file_paths))]
    await db.create_progression(branch_prog, msg_ids)
    await db.create_branch(
        {
            "id": branch_id,
            "created_at": time.time(),
            "name": "worker",
            "session_id": session_id,
            "progression_id": branch_prog,
        }
    )
    for mid, path in zip(msg_ids, file_paths):
        await db.insert_message(
            {
                "id": mid,
                "created_at": time.time(),
                "content": {"function": "Write", "arguments": {"file_path": path}},
                "sender": "worker",
                "recipient": "user",
                "role": "action",
                "node_metadata": {"lion_class": "ActionRequest"},
            }
        )


# ---------------------------------------------------------------------------
# compute_files_overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_child_sessions_reports_zero(db: StateDB):
    inv_id = _uid()
    await _make_invocation(db, inv_id)
    overlap = await compute_files_overlap(db, inv_id)
    assert overlap == {"count": 0, "top": []}


@pytest.mark.asyncio
async def test_single_worker_no_overlap(db: StateDB):
    inv_id = _uid()
    await _make_invocation(db, inv_id)
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/repo/a.py", "/repo/b.py"],
    )
    overlap = await compute_files_overlap(db, inv_id)
    assert overlap == {"count": 0, "top": []}


@pytest.mark.asyncio
async def test_two_workers_one_shared_file_counts_one_overlap(db: StateDB):
    """The design's exact test-plan fixture: two workers, one shared file +
    N distinct files each -> count=1, top-1 naming the shared path."""
    inv_id = _uid()
    await _make_invocation(db, inv_id)
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/repo/shared.py", "/repo/only_a.py", "/repo/only_a2.py"],
    )
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/repo/shared.py", "/repo/only_b.py"],
    )

    overlap = await compute_files_overlap(db, inv_id)
    assert overlap["count"] == 1
    assert overlap["top"] == [{"path": "/repo/shared.py", "workers": 2}]


@pytest.mark.asyncio
async def test_overlap_top_n_truncates_and_orders_by_worker_count(db: StateDB):
    inv_id = _uid()
    await _make_invocation(db, inv_id)
    # 3 workers touching a.py (most overlap), 2 touching b.py, 2 touching c.py.
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/a.py", "/b.py", "/c.py"],
    )
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/a.py", "/b.py"],
    )
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/a.py", "/c.py"],
    )

    overlap = await compute_files_overlap(db, inv_id, top_n=2)
    assert overlap["count"] == 3  # a.py, b.py, c.py all overlap
    assert len(overlap["top"]) == 2
    assert overlap["top"][0] == {"path": "/a.py", "workers": 3}
    # b.py and c.py tie at 2 workers -- deterministic tie-break by path.
    assert overlap["top"][1] == {"path": "/b.py", "workers": 2}


@pytest.mark.asyncio
async def test_worker_with_no_file_touching_messages_excluded(db: StateDB):
    """A worker session with branches/messages but no ActionRequest file
    args contributes nothing to any file set."""
    inv_id = _uid()
    await _make_invocation(db, inv_id)
    await _make_worker_session(
        db, invocation_id=inv_id, session_id=_uid(), branch_id=_uid(), file_paths=[]
    )
    await _make_worker_session(
        db,
        invocation_id=inv_id,
        session_id=_uid(),
        branch_id=_uid(),
        file_paths=["/x.py"],
    )
    overlap = await compute_files_overlap(db, inv_id)
    assert overlap == {"count": 0, "top": []}


# ---------------------------------------------------------------------------
# flush_run_telemetry
# ---------------------------------------------------------------------------


def _make_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_invocation = AsyncMock(return_value=None)
    svc.compute_files_overlap = AsyncMock(return_value={"count": 0, "top": []})
    svc.update_invocation = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_flush_run_telemetry_writes_signals_and_overlap_once():
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: True)
    await bus.emit(ScheduleRunSucceeded(run_id="run-1", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(
        return_value={"count": 1, "top": [{"path": "/shared.py", "workers": 2}]}
    )

    telemetry = await flush_run_telemetry(svc, bus, run_id="run-1", invocation_id="inv-1")

    assert telemetry == {
        "signals": {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1},
        "files_overlap": {"count": 1, "top": [{"path": "/shared.py", "workers": 2}]},
    }
    svc.update_invocation.assert_awaited_once_with(
        "inv-1", node_metadata={"coordination": telemetry}
    )
    # Popped -- a second flush attempt for the same run_id sees no signals.
    assert bus.pop_run_counters("run-1") is None


@pytest.mark.asyncio
async def test_flush_run_telemetry_merges_into_existing_node_metadata():
    """update_invocation replaces node_metadata wholesale, so the flush must
    read-modify-write rather than clobber pre-existing keys."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    await bus.emit(ScheduleRunSucceeded(run_id="run-2", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.get_invocation = AsyncMock(
        return_value={"id": "inv-2", "node_metadata": {"segments": ["existing"]}}
    )

    telemetry = await flush_run_telemetry(svc, bus, run_id="run-2", invocation_id="inv-2")

    assert telemetry is not None
    svc.update_invocation.assert_awaited_once()
    _, kwargs = svc.update_invocation.await_args
    assert kwargs["node_metadata"]["segments"] == ["existing"]
    assert kwargs["node_metadata"]["coordination"] == telemetry


@pytest.mark.asyncio
async def test_flush_run_telemetry_parses_string_node_metadata():
    """A raw-text() query without column typing can return node_metadata as
    a JSON string (SQLite) rather than an already-decoded dict."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    await bus.emit(ScheduleRunSucceeded(run_id="run-3", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.get_invocation = AsyncMock(
        return_value={"id": "inv-3", "node_metadata": '{"segments": ["existing"]}'}
    )

    await flush_run_telemetry(svc, bus, run_id="run-3", invocation_id="inv-3")

    _, kwargs = svc.update_invocation.await_args
    assert kwargs["node_metadata"]["segments"] == ["existing"]
    assert "coordination" in kwargs["node_metadata"]


@pytest.mark.asyncio
async def test_flush_run_telemetry_no_signal_and_no_overlap_writes_nothing():
    """Measure-only: a run that never touched the signal bus and has no
    file overlap leaves node_metadata untouched entirely."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()  # never emitted for this run_id
    svc = _make_svc()

    result = await flush_run_telemetry(svc, bus, run_id="never-emitted", invocation_id="inv-4")

    assert result is None
    svc.update_invocation.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_run_telemetry_writes_when_only_overlap_is_nonzero():
    """A run whose schedule_run write lost its race (so the bus never saw
    it) can still have file overlap worth reporting."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()  # no emit for this run_id
    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(
        return_value={"count": 2, "top": [{"path": "/x.py", "workers": 2}]}
    )

    telemetry = await flush_run_telemetry(svc, bus, run_id="run-5", invocation_id="inv-5")

    assert telemetry["signals"] == {"emitted": {}, "received": 0, "acted_on": 0}
    assert telemetry["files_overlap"]["count"] == 2
    svc.update_invocation.assert_awaited_once()


# ---------------------------------------------------------------------------
# flush_run_telemetry must be best-effort: I/O failures never propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_run_telemetry_swallows_compute_overlap_failure():
    """A failure computing files-overlap must not propagate -- the
    invocation's terminal write it rides on has already committed by the
    time flush runs."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    await bus.emit(ScheduleRunSucceeded(run_id="run-6", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(side_effect=OSError("disk error"))

    result = await flush_run_telemetry(svc, bus, run_id="run-6", invocation_id="inv-6")

    assert result is None
    svc.update_invocation.assert_not_awaited()
    # Popped regardless of the failure -- best-effort means the counters are
    # deliberately dropped, not retried.
    assert bus.pop_run_counters("run-6") is None


@pytest.mark.asyncio
async def test_flush_run_telemetry_swallows_update_invocation_failure():
    """A failure persisting node_metadata must not propagate either."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    await bus.emit(ScheduleRunSucceeded(run_id="run-7", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(
        return_value={"count": 1, "top": [{"path": "/shared.py", "workers": 2}]}
    )
    svc.update_invocation = AsyncMock(side_effect=OSError("disk error"))

    result = await flush_run_telemetry(svc, bus, run_id="run-7", invocation_id="inv-7")

    assert result is None


@pytest.mark.asyncio
async def test_flush_run_telemetry_reraises_cancellation():
    """Cancellation must never be swallowed as a mere telemetry failure."""
    from lionagi.studio.services.scheduler_state import flush_run_telemetry

    bus = SchedulerSignalBus()
    await bus.emit(ScheduleRunSucceeded(run_id="run-8", schedule_id="s1", reason_code="ok"))

    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await flush_run_telemetry(svc, bus, run_id="run-8", invocation_id="inv-8")
