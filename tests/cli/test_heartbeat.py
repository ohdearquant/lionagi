# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for per-op heartbeat and idle-child watchdog in li play/flow."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from lionagi.cli.orchestrate import flow as flow_mod

# Unit tests for the heartbeat loop logic


@pytest.mark.asyncio
async def test_heartbeat_emits_progress_for_running_op():
    """Heartbeat loop must call progress() for each running op segment."""
    emitted = []

    # Build a minimal _op_segments list with one running op.
    _op_segments = [
        {
            "op_id": "o1",
            "branch_id": "b1",
            "branch_name": "researcher",
            "status": "running",
            "started_at": time.time() - 90,  # 90s ago
            "ended_at": None,
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            _now = time.time()
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                elapsed = _now - seg.get("started_at", _now)
                seg["last_heartbeat_at"] = _now
                emitted.append(f"heartbeat {elapsed / 60:.0f}m")

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.12)  # let it fire ~2 times
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(emitted) >= 1
    assert "heartbeat" in emitted[0]


@pytest.mark.asyncio
async def test_heartbeat_skips_completed_ops():
    """Heartbeat loop must NOT emit for ops that have already completed."""
    emitted = []

    _op_segments = [
        {
            "op_id": "o1",
            "branch_name": "researcher",
            "status": "completed",  # already done
            "started_at": time.time() - 90,
            "ended_at": time.time(),
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                emitted.append("heartbeat")

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert emitted == []


def _running_segment(now: float, age: float) -> dict:
    return {
        "branch_name": "worker",
        "status": "running",
        "started_at": now - age,
    }


def test_busy_descendants_suppress_idle_warning():
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 601),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=({41: 8.0}, True),
        current=({41: 8.2}, True),
    )
    assert warning is None


def test_quiet_descendants_emit_idle_warning():
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 601),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=({41: 8.0}, True),
        current=({41: 8.0}, True),
    )
    assert warning is not None
    assert "IDLE STALL" in warning


def test_external_io_without_output_writes_suppresses_warning(monkeypatch, tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    connected = threading.Event()
    server_errors: list[OSError] = []
    activity = {"cpu": 0.0}
    tree_walks: list[bool] = []

    def echo_traffic() -> None:
        try:
            connection, _address = listener.accept()
            connected.set()
            with connection:
                while data := connection.recv(65536):
                    activity["cpu"] += len(data) / 1_000_000
                    connection.sendall(data)
        except OSError as exc:
            server_errors.append(exc)

    server_thread = threading.Thread(target=echo_traffic, daemon=True)
    server_thread.start()

    child = SimpleNamespace(
        pid=41,
        cpu_times=lambda: SimpleNamespace(user=activity["cpu"], system=0.0),
    )
    root = SimpleNamespace(children=lambda recursive: tree_walks.append(recursive) or [child])
    monkeypatch.setattr(flow_mod.psutil, "Process", lambda _pid: root)

    with socket.create_connection(listener.getsockname(), timeout=5) as client:
        assert connected.wait(timeout=5), "loopback client did not connect"
        previous = flow_mod._sample_descendant_cpu(7)
        payload = b"x" * 200000
        client.sendall(payload)
        received = bytearray()
        while len(received) < len(payload):
            received.extend(client.recv(len(payload) - len(received)))
        assert bytes(received) == payload
        current = flow_mod._sample_descendant_cpu(7)
        assert previous[1] and current[1]
        assert previous[0].keys() == current[0].keys()
        assert current[0][41] > previous[0][41]

        now = time.time()
        output_path = tmp_path / "output"
        warning = flow_mod._heartbeat_warning(
            _running_segment(now, 601),
            now=now,
            max_idle_seconds=600,
            sample_interval_seconds=60,
            previous=previous,
            current=current,
        )
        assert warning is None
        assert not output_path.exists()

    listener.close()
    server_thread.join(timeout=5)

    assert not server_thread.is_alive()
    assert server_errors == []
    assert tree_walks == [True, True]


def test_empty_descendant_samples_emit_distinct_warning():
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 601),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=({}, True),
        current=({}, True),
    )
    assert warning is not None
    assert "NO DESCENDANTS" in warning
    assert "IDLE STALL" not in warning


def test_incomplete_descendant_sample_suppresses_warning():
    now = time.time()
    assert (
        flow_mod._heartbeat_warning(
            _running_segment(now, 601),
            now=now,
            max_idle_seconds=600,
            sample_interval_seconds=60,
            previous=({41: 8.0}, True),
            current=({}, False),
        )
        is None
    )


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, ({41: 8.0}, True)),
        (({41: 8.0}, True), ({41: 7.9}, True)),
    ],
    ids=["first-sample", "counter-regression"],
)
def test_unproven_descendant_delta_suppresses_warning(previous, current):
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 601),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=previous,
        current=current,
    )
    assert warning is None


def test_busy_new_descendant_suppresses_warning_without_overlap():
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 601),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=({41: 8.0}, True),
        current=({42: 1.0}, True),
    )

    assert warning is None


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (({41: 8.0}, True), ({41: 8.2}, True)),
        (({41: 8.0}, True), ({41: 8.0}, True)),
        (({}, True), ({}, True)),
    ],
)
def test_under_threshold_never_emits_activity_warning(previous, current):
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now, 599),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=60,
        previous=previous,
        current=current,
    )
    assert warning is None


def test_descendant_sampler_sums_each_cpu_total(monkeypatch):
    calls: list[bool] = []
    children = [
        SimpleNamespace(pid=41, cpu_times=lambda: SimpleNamespace(user=1.2, system=0.3)),
        SimpleNamespace(pid=42, cpu_times=lambda: SimpleNamespace(user=2.0, system=0.4)),
    ]
    root = SimpleNamespace(children=lambda recursive: calls.append(recursive) or children)
    monkeypatch.setattr(flow_mod.psutil, "Process", lambda _pid: root)

    assert flow_mod._sample_descendant_cpu(7) == ({41: 1.5, 42: 2.4}, True)
    assert calls == [True]


def test_descendant_sampler_marks_denied_walk_incomplete(monkeypatch):
    def denied(_pid):
        raise PermissionError("denied")

    monkeypatch.setattr(flow_mod.psutil, "Process", denied)
    assert flow_mod._sample_descendant_cpu(7) == ({}, False)


def test_descendant_sampler_marks_denied_cpu_read_incomplete(monkeypatch):
    def denied():
        raise PermissionError("denied")

    child = SimpleNamespace(pid=41, cpu_times=denied)
    root = SimpleNamespace(children=lambda recursive: [child])
    monkeypatch.setattr(flow_mod.psutil, "Process", lambda _pid: root)
    assert flow_mod._sample_descendant_cpu(7) == ({}, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_execute_dag_heartbeat_tracks_samples_and_terminal_state(
    monkeypatch,
    tmp_path,
    terminal_status,
):
    from lionagi import Branch, Session
    from lionagi.casts.emission import TaskAssignment
    from lionagi.engines import PlanningEngine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted

    branch = Branch(name="worker")
    session = Session(default_branch=branch)
    builder = OperationGraphBuilder()
    node_id = builder.add_operation("operate", depends_on=[])
    plan_result = flow_mod._PlanResult(
        assignments=[TaskAssignment(task="work", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = flow_mod._DagState(
        node_ids=[node_id],
        known_nodes={node_id},
        deps_by_node={node_id: []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["test/model"],
    )
    env = SimpleNamespace(
        run=SimpleNamespace(agent_artifact_dir=lambda agent_id: tmp_path / agent_id),
        session=session,
        builder=builder,
        verbose=False,
        team_data=None,
        cwd=None,
        _live_persist=None,
    )

    emitted: list[str] = []
    clock = [1000.0]
    real_sleep = asyncio.sleep
    heartbeat_permits: asyncio.Queue[None] = asyncio.Queue()
    sampled = [asyncio.Event() for _ in range(4)]
    sample_values = [
        ({41: 8.0}, True),
        ({41: 8.2}, True),
        ({41: 8.2}, True),
        ({41: 8.2}, True),
    ]
    sample_count = 0

    async def controlled_sleep(delay: float) -> None:
        if delay == 60:
            await heartbeat_permits.get()
            return
        await real_sleep(3600)

    def sample_cpu(_pid: int):
        nonlocal sample_count
        sample = sample_values[sample_count]
        sampled[sample_count].set()
        sample_count += 1
        return sample

    stall_counts: list[int] = []

    async def run_dag(*_args, **_kwargs):
        await session.emit(NodeStarted(op_id=str(node_id), name="worker", elapsed=0.0))
        clock[0] = 1701.0
        for index in range(3):
            heartbeat_permits.put_nowait(None)
            await asyncio.wait_for(sampled[index].wait(), timeout=1)
            await real_sleep(0)
            stall_counts.append(sum("IDLE STALL" in line for line in emitted))

        signal_type = NodeCompleted if terminal_status == "completed" else NodeFailed
        await session.emit(signal_type(op_id=str(node_id), name="worker", elapsed=1.0))
        heartbeat_permits.put_nowait(None)
        await asyncio.wait_for(sampled[3].wait(), timeout=1)
        await real_sleep(0)
        stall_counts.append(sum("IDLE STALL" in line for line in emitted))
        return {
            "operation_results": {node_id: "result"},
            "spawned_operations": 0,
            "failed_operations": [node_id] if terminal_status == "failed" else [],
        }

    engine_run = SimpleNamespace(run_dag=run_dag)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda *_args, **_kwargs: engine_run)
    monkeypatch.setattr(flow_mod, "progress", emitted.append)
    monkeypatch.setattr(flow_mod.time, "time", lambda: clock[0])
    monkeypatch.setattr(flow_mod._asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(flow_mod, "_sample_descendant_cpu", sample_cpu)

    await flow_mod._execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert stall_counts == [0, 0, 1, 1]
    assert sum("worker heartbeat" in line for line in emitted) == 3
    assert dag_state.op_segments == [
        {
            "op_id": str(node_id),
            "branch_id": str(branch.id),
            "branch_name": "worker",
            "status": terminal_status,
            "started_at": 1000.0,
            "ended_at": 1701.0,
            "last_heartbeat_at": 1701.0,
        }
    ]


@pytest.mark.asyncio
async def test_heartbeat_updates_last_heartbeat_at():
    """Heartbeat loop must update last_heartbeat_at in the segment dict."""
    _op_segments = [
        {
            "op_id": "o1",
            "branch_name": "analyst",
            "status": "running",
            "started_at": time.time() - 30,
            "ended_at": None,
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            _now = time.time()
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                seg["last_heartbeat_at"] = _now

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert _op_segments[0]["last_heartbeat_at"] is not None
    assert _op_segments[0]["last_heartbeat_at"] > _op_segments[0]["started_at"]


@pytest.mark.asyncio
async def test_heartbeat_cancelled_cleanly():
    """Cancelling the heartbeat task must not raise unhandled errors."""
    import contextlib

    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(60)

    task = asyncio.ensure_future(_heartbeat_loop())
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_heartbeat_does_not_fire_before_interval():
    """Heartbeat must not fire immediately on start (must await the interval)."""
    fires = []

    async def _heartbeat_loop(interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            fires.append("fired")

    task = asyncio.ensure_future(_heartbeat_loop(interval=1.0))
    # Only wait 50ms — well below the 1s interval
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert fires == []


# flow.py integration: _record_segment includes last_heartbeat_at


def test_op_segment_schema_includes_heartbeat_field():
    """Structural: flow.py source must contain all heartbeat-related fields and symbols."""
    import inspect

    from lionagi.cli.orchestrate import flow as flow_mod

    src = inspect.getsource(flow_mod)
    assert '"last_heartbeat_at"' in src, (
        "_record_segment must initialise 'last_heartbeat_at' in the segment dict"
    )
    assert "_heartbeat_loop" in src, "flow.py must define _heartbeat_loop"
    assert "_hb_task" in src, "flow.py must create and cancel _hb_task"
    assert "heartbeat_interval" in src, "flow.py must define heartbeat_interval"
    assert "sample_interval_seconds=heartbeat_interval" in src, (
        "the stall predicate must receive the actual heartbeat interval"
    )
    assert "max_idle_seconds" in src, "flow.py must define max_idle_seconds"
    assert "IDLE STALL" in src, "flow.py must emit an IDLE STALL warning"
