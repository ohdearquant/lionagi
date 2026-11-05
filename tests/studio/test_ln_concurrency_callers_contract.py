# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Thread and delay contracts at the admin, lifecycle, and scheduler boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace

import anyio
import pytest

_WAIT_SECONDS = 5
_ADMIN_SITES = (
    "snapshot",
    "identity",
    "phantom",
    "health_scan",
    "transition_scan",
    "transition_phantom",
)


async def _wait_thread_event(event):
    with anyio.fail_after(_WAIT_SECONDS + 1):
        assert await anyio.to_thread.run_sync(
            event.wait,
            _WAIT_SECONDS,
            abandon_on_cancel=True,
            limiter=anyio.CapacityLimiter(1),
        )


class _WorkerProbe:
    def __init__(self, result, *, error=None, blocked=False):
        self.result = result
        self.error = error
        self.context = ContextVar("caller_context", default="unset")
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        if not blocked:
            self.release.set()

    def worker(self, *args, **kwargs):
        self.calls.append((args, kwargs, threading.get_ident(), self.context.get()))
        self.context.set("worker")
        self.started.set()
        try:
            assert self.release.wait(_WAIT_SECONDS), "worker was not released"
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.finished.set()


async def _cancel_while_worker_is_blocked(subject, probe, *, abandon):
    host_done = anyio.Event()
    scope_ready = anyio.Event()
    scopes = []
    errors = []

    async def host():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                scope_ready.set()
                try:
                    await subject()
                except BaseException as exc:
                    errors.append(exc)
        finally:
            host_done.set()

    async with anyio.create_task_group() as group:
        group.start_soon(host)
        try:
            with anyio.fail_after(_WAIT_SECONDS):
                await scope_ready.wait()
            await _wait_thread_event(probe.started)
            scopes[0].cancel()
            with anyio.fail_after(_WAIT_SECONDS):
                if abandon:
                    # Nested fan-out must finish unwinding before the outer host exits.
                    await host_done.wait()
                else:
                    await anyio.wait_all_tasks_blocked()
            assert not probe.finished.is_set()
            assert host_done.is_set() is abandon
        finally:
            probe.release.set()
            with anyio.CancelScope(shield=True):
                await _wait_thread_event(probe.finished)
                with anyio.fail_after(_WAIT_SECONDS):
                    await host_done.wait()
    assert all(isinstance(exc, anyio.get_cancelled_exc_class()) for exc in errors)
    assert len(probe.calls) == 1


def _admin_subject(monkeypatch, tmp_path, site, probe):
    from lionagi.cli import _code_identity
    from lionagi.state import db as state_db
    from lionagi.state.reasons import SessionReasons
    from lionagi.studio import config
    from lionagi.studio.services import admin, sessions

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    row = {
        "id": "session-worker",
        "name": "worker",
        "user_label": None,
        "playbook_name": None,
        "agent_name": None,
        "show_play_name": None,
        "status": "running",
        "invocation_kind": "agent",
        "started_at": 1.0,
        "updated_at": 2.0,
        "last_message_at": 2.0,
        "artifacts_path": str(artifacts),
        "node_metadata": None,
        "message_count": 0,
    }
    boundaries = []
    writes = []

    class Store:
        mode = "native"

        def __init__(self):
            self._in_transaction = False
            self._pending_history = {}
            self._committed_history = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement, *args):
            sql = str(statement)
            writes.append((sql, args))
            if "INSERT INTO status_transitions" in sql:
                assert self._in_transaction
                values = args[0]
                assert values["delivery_eligible"] is True
                self._pending_history[values["id"]] = values["delivery_eligible"]
            elif "SELECT delivery_eligible FROM status_transitions" in sql:
                assert not self._in_transaction
                value = self._committed_history.get(args[0]["id"])
                return SimpleNamespace(first=lambda: None if value is None else (value,))
            return self

        async def fetchone(self):
            return {"n": 1}

        async def fetchall(self):
            return [row]

        async def get_session(self, sid):
            assert sid == row["id"]
            return row

        @contextlib.asynccontextmanager
        async def transaction(self):
            assert not self._in_transaction
            self._in_transaction = True
            try:
                yield self
            except BaseException:
                self._pending_history.clear()
                raise
            else:
                self._committed_history.update(self._pending_history)
                self._pending_history.clear()
            finally:
                self._in_transaction = False

        @contextlib.asynccontextmanager
        async def _read(self):
            assert not self._in_transaction
            yield self

        def _owner_predicate(self, table):
            assert table == "status_transitions"
            return "", {}

        def _lifecycle_service(self):
            from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

            class Callbacks:
                async def emit(_self, envelope):
                    assert not self._in_transaction
                    assert self._committed_history[envelope.event_id] is True
                    writes.append(("terminal_callback", envelope.event_id))

            return SQLAlchemyLifecycleService(self, terminal_callbacks=Callbacks())

        @property
        def rowcount(self):
            return 1

        async def insert_admin_event(self, **kwargs):
            writes.append(("admin_event", kwargs))
            return "audit-worker"

    async def user_label(*args, **kwargs):
        return "user_label"

    async def liveness(*args):
        return False, "process-snapshot"

    async def dead(*args):
        return False

    async def no_identity():
        return {"head": "not-this-worker"}

    def scan(*args, **kwargs):
        boundaries.append(("scan", args, kwargs))
        return admin._ScanResult(None, site == "health_scan")

    def phantom(*args, **kwargs):
        boundaries.append(("phantom", args, kwargs))
        return "process_dead"

    monkeypatch.setattr(admin, "_open_db", lambda *args: Store())
    monkeypatch.setattr(state_db, "StateDB", Store)
    monkeypatch.setattr(admin, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(admin, "require_file_store", lambda: None)
    monkeypatch.setattr(admin, "store_exists", lambda: True)
    monkeypatch.setattr(admin, "store_path", lambda: str(tmp_path / "unused.db"))
    monkeypatch.setattr(sessions, "_user_label_selection", user_label)
    monkeypatch.setattr(admin, "_resolve_process_liveness_with_snapshot", liveness)
    monkeypatch.setattr(admin, "resolve_process_liveness", dead)
    monkeypatch.setattr(admin, "db_health", lambda: {})
    monkeypatch.setattr(admin, "process_snapshot_diagnostics", lambda: {})
    monkeypatch.setattr(config, "scheduler_timezone_report", lambda: {})
    if site != "identity":
        monkeypatch.setattr(admin, "_code_identity_report", no_identity)
    monkeypatch.setattr(admin, "_find_stale_lock", scan)
    monkeypatch.setattr(admin, "_classify_phantom", phantom)
    monkeypatch.setattr(
        admin,
        "time",
        SimpleNamespace(
            time=lambda: 10000.0, monotonic=time.monotonic, perf_counter=time.perf_counter
        ),
    )
    if site == "snapshot":
        # Cache publication belongs to this caller, after the shielded worker finishes.
        monkeypatch.setattr(admin, "time", time)
        monkeypatch.setattr(admin, "_PS_SNAPSHOT_CACHE", None)
        monkeypatch.setattr(admin, "_PS_SNAPSHOT_METRICS", None)
        monkeypatch.setattr(admin, "_PS_SNAPSHOT_SEQUENCE", 0)
        monkeypatch.setattr(admin, "_ps_snapshot", probe.worker)
        subject = admin._capture_ps_snapshot
    elif site == "identity":
        monkeypatch.setattr(_code_identity, "code_identity", probe.worker)
        subject = admin._code_identity_report
    elif site == "phantom":
        monkeypatch.setattr(admin, "_classify_phantom", probe.worker)
        subject = lambda: admin.list_phantom_sessions(stale_hours=2.0)
    elif site == "health_scan":
        monkeypatch.setattr(admin, "_find_stale_lock", probe.worker)
        subject = admin.health_report
    else:
        target = "_find_stale_lock" if site == "transition_scan" else "_classify_phantom"
        monkeypatch.setattr(admin, target, probe.worker)

        async def transition():
            return await admin.transition_sessions(
                [row["id"]],
                target_status="failed",
                reason_code=SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
            )

        subject = transition
    return subject, admin, row, boundaries, writes


def _admin_result(admin, site):
    if site == "snapshot":
        return "captured-processes"
    if site == "identity":
        return {"head": "worker-head"}
    if site.endswith("scan"):
        return admin._ScanResult(None, site == "health_scan")
    return "process_dead"


def _assert_admin_arguments(site, probe, row, boundaries):
    args, kwargs, _, _ = probe.calls[0]
    if site in {"snapshot", "identity"}:
        assert args == () and kwargs == {}
        return
    if site.endswith("scan"):
        assert str(args[0]) == row["artifacts_path"]
        assert kwargs["cutoff"] == 6400.0
        assert set(kwargs) == {"cutoff", "cache", "budget"}
    else:
        assert args[0] is row
        assert kwargs["now"] == 10000.0
        assert kwargs["stale_seconds"] == (7200.0 if site == "phantom" else 3600)
        assert kwargs["ps_snapshot"] == "process-snapshot"
        assert set(kwargs) == {"now", "stale_seconds", "ps_snapshot", "lock_cache", "lock_budget"}
    if site.startswith("transition") and boundaries:
        _, _, other = boundaries[0]
        cache = kwargs.get("cache", kwargs.get("lock_cache"))
        budget = kwargs.get("budget", kwargs.get("lock_budget"))
        assert cache is other.get("cache", other.get("lock_cache"))
        assert budget is other.get("budget", other.get("lock_budget"))


@pytest.mark.asyncio
@pytest.mark.parametrize("site", _ADMIN_SITES)
@pytest.mark.parametrize("fails", [False, True])
async def test_admin_workers_preserve_arguments_context_results_and_errors(
    monkeypatch, tmp_path, site, fails
):
    from lionagi.studio.services import admin

    error = ValueError("worker failed") if fails else None
    probe = _WorkerProbe(_admin_result(admin, site), error=error)
    subject, admin, row, boundaries, writes = _admin_subject(monkeypatch, tmp_path, site, probe)
    token = probe.context.set("caller")
    try:
        if fails and site != "identity":
            with pytest.raises(ValueError) as caught:
                await subject()
            assert caught.value is error
        else:
            result = await subject()
            if fails:
                assert result == {
                    "drift": {
                        "status": "unknown",
                        "reasons": [],
                        "unknown": ["could not establish code identity: ValueError: worker failed"],
                    }
                }
            elif site in {"snapshot", "identity"}:
                assert result is probe.result
                if site == "snapshot":
                    assert admin._PS_SNAPSHOT_CACHE.value is probe.result
                    assert admin._PS_SNAPSHOT_METRICS["captures"] == 1
            elif site == "phantom":
                assert len(result) == 1
                assert result[0]["session_id"] == row["id"]
                assert result[0]["reason"] == "process_dead"
            elif site == "health_scan":
                assert result["sessions"]["scanned"] == 1
                assert result["sessions"]["lock_scan_truncated"] == 1
            else:
                assert result == {
                    "transitioned": [row["id"]],
                    "skipped": [],
                    "event_id": "audit-worker",
                }
                assert any("UPDATE sessions" in statement for statement, _ in writes)
                assert any("INSERT INTO status_transitions" in statement for statement, _ in writes)
                inserted = next(
                    args[0]["id"]
                    for statement, args in writes
                    if "INSERT INTO status_transitions" in statement
                )
                assert ("terminal_callback", inserted) in writes
        assert len(probe.calls) == 1
        _assert_admin_arguments(site, probe, row, boundaries)
        assert probe.calls[0][2] != threading.get_ident()
        assert probe.calls[0][3] == "caller"
        assert probe.context.get() == "caller"
    finally:
        probe.release.set()
        await _wait_thread_event(probe.finished)
        probe.context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", _ADMIN_SITES)
async def test_admin_workers_shield_the_default_wait(monkeypatch, tmp_path, site):
    from lionagi.studio.services import admin

    probe = _WorkerProbe(_admin_result(admin, site), blocked=True)
    subject, _, _, _, _ = _admin_subject(monkeypatch, tmp_path, site, probe)
    await _cancel_while_worker_is_blocked(subject, probe, abandon=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "os", "value"])
async def test_monitor_status_keeps_the_worker_result_and_oserror_boundary(monkeypatch, failure):
    from lionagi.cli import monitor
    from lionagi.service import _subagent_admission as admission

    error = PermissionError("status denied") if failure == "os" else ValueError("bad status")
    result = {"pools": [{"name": "all"}]}
    probe = _WorkerProbe(result, error=error if failure else None)
    monkeypatch.setattr(admission, "subagent_admission_status", probe.worker)
    token = probe.context.set("caller")
    try:
        if failure == "value":
            with pytest.raises(ValueError) as caught:
                await monitor._subagent_status()
            assert caught.value is error
        else:
            observed = await monitor._subagent_status()
            if failure:
                assert observed == {"error": "PermissionError"}
            else:
                assert observed is result
        assert probe.calls == [((), {}, probe.calls[0][2], "caller")]
        assert probe.calls[0][2] != threading.get_ident()
        assert probe.context.get() == "caller"
    finally:
        probe.release.set()
        await _wait_thread_event(probe.finished)
        probe.context.reset(token)


@pytest.mark.asyncio
async def test_monitor_status_shields_the_default_worker_wait(monkeypatch):
    from lionagi.cli import monitor
    from lionagi.service import _subagent_admission as admission

    probe = _WorkerProbe({"pools": []}, blocked=True)
    monkeypatch.setattr(admission, "subagent_admission_status", probe.worker)
    await _cancel_while_worker_is_blocked(monitor._subagent_status, probe, abandon=False)


def _delivery_subject(monkeypatch, tmp_path, consumer, handler):
    from lionagi.state.lifecycle.callbacks import (
        EntityRef,
        RunTerminalEnvelope,
        TerminalCallbackRegistry,
    )

    records = []
    if consumer == "callback":
        registry = TerminalCallbackRegistry()
        registry.register("worker", handler, kinds=["session"], ids=["session-worker"])
        envelope = RunTerminalEnvelope(
            event_id="worker-event",
            entity=EntityRef(kind="session", id="session-worker"),
            previous_status="running",
            terminal_status="completed",
            reason_code="run.completed.ok",
            occurred_at=123.0,
        )
        return lambda: registry.emit(envelope), records

    from lionagi.cli.orchestrate import _notify

    run = SimpleNamespace(run_id="run-worker", notify_outcome_path=tmp_path / "notify.json")
    monkeypatch.setattr(
        _notify,
        "resolve_notify_config",
        lambda **kwargs: SimpleNamespace(reason=None, handler=object()),
    )
    monkeypatch.setattr(_notify, "build_handler", lambda *args, **kwargs: handler)
    monkeypatch.setattr(
        _notify, "record_notify_outcome_to_run", lambda *args, **kwargs: records.append(kwargs)
    )

    async def deliver():
        await _notify.deliver_flow_notify_now(
            override="unused",
            run=run,
            entity_kind="session",
            entity_id="session-worker",
            invocation_id=None,
            flow_kind="flow",
            playbook=None,
            save_dir=None,
            cwd=str(tmp_path),
            started_at=1.0,
            terminal_status="completed",
            reason_code="run.completed.ok",
            occurred_at=123.0,
        )

    return deliver, records


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["callback", "notify"])
@pytest.mark.parametrize("fails", [False, True])
async def test_terminal_sync_workers_keep_context_awaitables_and_failure_recording(
    monkeypatch, tmp_path, caplog, consumer, fails
):
    awaited = []
    error = ValueError("terminal worker failed") if fails else None
    probe = _WorkerProbe(None, error=error)

    async def returned_awaitable():
        awaited.append(probe.context.get())
        return "discarded"

    def handler(envelope):
        probe.worker(envelope)
        return returned_awaitable()

    subject, records = _delivery_subject(monkeypatch, tmp_path, consumer, handler)
    token = probe.context.set("caller")
    try:
        await subject()
        assert len(probe.calls) == 1
        args, kwargs, worker_thread, context = probe.calls[0]
        assert kwargs == {} and len(args) == 1
        assert args[0].entity.id == "session-worker"
        assert args[0].terminal_status == "completed"
        assert args[0].reason_code == "run.completed.ok"
        assert args[0].occurred_at == 123.0
        assert worker_thread != threading.get_ident() and context == "caller"
        assert probe.context.get() == "caller"
        assert awaited == ([] if fails else ["caller"])
        if fails:
            if consumer == "notify":
                messages = [
                    record
                    for record in caplog.records
                    if "direct-path notify.on_terminal delivery failed" in record.getMessage()
                ]
                assert len(messages) == 1
                assert messages[0].getMessage().endswith("for run run-worker (ValueError)")
                assert messages[0].exc_info is None
                assert "terminal worker failed" not in messages[0].getMessage()
                assert len(records) == 1
                assert records[0]["ok"] is False and records[0]["exit_code"] is None
                assert "ValueError: terminal worker failed" in records[0]["stderr_text"]
            else:
                assert any(
                    record.exc_info and record.exc_info[1] is error for record in caplog.records
                )
                assert records == []
        else:
            assert records == []
    finally:
        probe.release.set()
        await _wait_thread_event(probe.finished)
        probe.context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["callback", "notify"])
async def test_terminal_delivery_abandons_a_cancelled_sync_wait(monkeypatch, tmp_path, consumer):
    probe = _WorkerProbe(None, blocked=True)
    subject, records = _delivery_subject(monkeypatch, tmp_path, consumer, probe.worker)
    await _cancel_while_worker_is_blocked(subject, probe, abandon=True)
    assert records == []


@pytest.mark.asyncio
async def test_terminal_callback_deadline_returns_before_the_sync_worker_finishes():
    from lionagi.state.lifecycle.callbacks import (
        EntityRef,
        RunTerminalEnvelope,
        TerminalCallbackRegistry,
    )

    probe = _WorkerProbe(None, blocked=True)
    registry = TerminalCallbackRegistry(budget_seconds=0.2)
    fast = []

    async def sibling(envelope):
        fast.append(envelope.event_id)

    registry.register("worker", probe.worker)
    registry.register("sibling", sibling)
    envelope = RunTerminalEnvelope(
        event_id="deadline-event",
        entity=EntityRef(kind="session", id="session-worker"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=123.0,
    )
    try:
        with anyio.fail_after(3):
            await registry.emit(envelope)
        assert probe.started.is_set()
        assert not probe.finished.is_set()
        assert fast == ["deadline-event"]
    finally:
        probe.release.set()
        await _wait_thread_event(probe.finished)


def _record_sleeps(monkeypatch, sleep):
    # Both primitives are intercepted only for the direct subject call. The saved
    # asyncio primitive remains available to the fake for a real checkpoint.
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(anyio, "sleep", sleep)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["return", "cancel", "stop"])
async def test_scheduler_restart_preserves_delay_before_stop_or_spawn(monkeypatch, outcome):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = object.__new__(SchedulerEngine)
    engine._stopping = False
    order = []
    sentinel = object()
    native_sleep = asyncio.sleep

    def spawn():
        order.append(("spawn",))
        return sentinel

    engine._spawn_tick_loop = spawn

    async def interrupted(seconds):
        order.append(("sleep", seconds))
        engine._stopping = outcome == "stop"
        if outcome != "return":
            raise asyncio.CancelledError()
        await native_sleep(0)

    with monkeypatch.context() as local:
        _record_sleeps(local, interrupted)
        await engine._restart_tick_loop(2.5)
    assert order == ([("sleep", 2.5)] if outcome == "stop" else [("sleep", 2.5), ("spawn",)])
    if outcome != "stop":
        assert engine._task is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("stopping", [False, True])
async def test_scheduler_tick_delay_keeps_deadline_and_stop_cancellation(monkeypatch, stopping):
    from lionagi.studio.scheduler import engine as engine_module

    engine = object.__new__(engine_module.SchedulerEngine)
    engine._stopping = False
    now = [100.0]
    delays = []
    native_sleep = asyncio.sleep
    monkeypatch.setattr(engine_module, "_TICK_INTERVAL", 5.0)
    monkeypatch.setattr(engine_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def interrupted_once(seconds):
        delays.append(seconds)
        if len(delays) == 1:
            now[0] = 102.0
            engine._stopping = stopping
            raise asyncio.CancelledError()
        now[0] += seconds
        await native_sleep(0)

    with monkeypatch.context() as local:
        _record_sleeps(local, interrupted_once)
        if stopping:
            with pytest.raises(asyncio.CancelledError):
                await engine._sleep_between_ticks()
        else:
            await engine._sleep_between_ticks()
    assert delays == ([5.0] if stopping else [5.0, 3.0])
    assert now[0] == (102.0 if stopping else 105.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "sleep", "deadline"])
async def test_admission_lock_retry_preserves_spacing_and_closes_failed_fd(
    monkeypatch, tmp_path, failure
):
    from lionagi.service import _subagent_admission as admission

    pool = tmp_path / "all"
    pool.mkdir()
    opened = []
    delays = []
    choices = []
    native_open = admission._open_slot_file
    native_sleep = asyncio.sleep
    attempts = iter([False, True])
    error = RuntimeError("retry interrupted")
    clock = iter([0.0, 1.0] if failure == "deadline" else [0.0, 0.0])

    def open_slot(path):
        fd = native_open(path)
        opened.append(fd)
        return fd

    def choose(low, high):
        choices.append((low, high))
        return 0.02

    async def sleep(seconds):
        delays.append(seconds)
        if failure == "sleep":
            raise error
        await native_sleep(0)

    monkeypatch.setattr(admission, "_open_slot_file", open_slot)
    monkeypatch.setattr(admission, "_lock_now", lambda fd: next(attempts))
    monkeypatch.setattr(admission, "_ADMISSION_LOCK_SECONDS", 1.0)
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(admission, "random", SimpleNamespace(uniform=choose))
    try:
        with monkeypatch.context() as local:
            local.setattr(anyio, "sleep", sleep)
            if failure == "sleep":
                with pytest.raises(RuntimeError) as caught:
                    await admission._lock_admission(pool)
                assert caught.value is error
            elif failure == "deadline":
                with pytest.raises(admission.SubagentAdmissionRefusedError) as caught:
                    await admission._lock_admission(pool)
                assert str(caught.value) == (
                    "subagent admission refused: the admission lock of "
                    "pool 'all' stayed held for 1s"
                )
            else:
                fd = await admission._lock_admission(pool)
                try:
                    assert fd == opened[0]
                    os.fstat(fd)
                finally:
                    os.close(fd)
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert delays == ([] if failure == "deadline" else [0.02])
        assert choices == ([] if failure == "deadline" else [(0.005, 0.05)])
    finally:
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "poll", "deadline"])
async def test_admission_wait_releases_slots_and_locks_before_retry(monkeypatch, tmp_path, mode):
    from lionagi.service import _subagent_admission as admission

    trace = []
    now = [0.0]
    choices = []
    pass_number = [0]
    native_sleep = asyncio.sleep
    pools = ["all", "heavy"] if mode == "quick" else ["all"]
    caps = [(pool, f"CAP_{pool}", 1) for pool in pools]

    class Slot:
        def __init__(self, pool):
            self.pool = pool

        def release(self):
            trace.append(("release", self.pool))

    async def lock(pool_dir):
        if pool_dir.name == "all":
            pass_number[0] += 1
        trace.append(("lock", pool_dir.name))
        return pool_dir.name

    def read(pool_dir):
        trace.append(("read", pool_dir.name))
        if mode != "quick" and (pass_number[0] == 1 or mode == "deadline"):
            return [{"holder_pid": 123, "cap": 1}], set(), 0
        return [], set(), 0

    def take(pool, *args):
        trace.append(("take", pool))
        if mode == "quick" and pass_number[0] == 1 and pool == "heavy":
            return None
        return Slot(pool)

    def choose(low, high):
        choices.append((low, high))
        return 0.003 if mode == "quick" else 1.25

    async def sleep(seconds):
        trace.append(("sleep", seconds))
        now[0] += seconds
        await native_sleep(0)

    monkeypatch.setattr(admission, "_caps", lambda *args: caps)
    monkeypatch.setattr(admission, "_pools_root", lambda: tmp_path)
    monkeypatch.setattr(admission, "_lock_admission", lock)
    monkeypatch.setattr(admission, "_unlock_admission", lambda fd: trace.append(("unlock", fd)))
    monkeypatch.setattr(admission, "_read_pool", read)
    monkeypatch.setattr(admission, "_take_slot", take)
    monkeypatch.setattr(admission, "_ancestor_pids", lambda: frozenset())
    monkeypatch.setattr(admission, "_fcntl", object())
    monkeypatch.setattr(admission, "_POLL_SECONDS", 2.0)
    monkeypatch.setattr(
        admission, "time", SimpleNamespace(time=lambda: 0.0, monotonic=lambda: now[0])
    )
    monkeypatch.setattr(admission, "random", SimpleNamespace(uniform=choose))
    admitted = []
    lease = None
    try:
        with admission._on_subagent_admission(admitted.append), monkeypatch.context() as local:
            local.setattr(anyio, "sleep", sleep)
            if mode == "deadline":
                with pytest.raises(admission.SubagentAdmissionRefusedError) as caught:
                    await admission.admit_subagent(
                        engine="codex",
                        environ={"LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "1"},
                    )
                assert str(caught.value) == (
                    "subagent admission refused: heavy codex agent waited 1s; "
                    "pool 'all' is at its cap of 1 (CAP_all), held by pid 123"
                )
                assert caught.value.pool == "all" and caught.value.cap == 1
                assert caught.value.holders == (123,)
            else:
                lease = await admission.admit_subagent(
                    engine="codex", environ={"LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "1"}
                )
                assert [slot.pool for slot in lease.slots] == pools
        assert admitted == ([False] if mode == "deadline" else [False, True])
        sleeps = [entry for entry in trace if entry[0] == "sleep"]
        assert sleeps == [("sleep", 0.003 if mode == "quick" else 1.0)]
        first_sleep = trace.index(sleeps[0])
        if mode == "quick":
            assert trace[first_sleep - 3 : first_sleep] == [
                ("unlock", "heavy"),
                ("unlock", "all"),
                ("release", "all"),
            ]
            assert choices == [(0.001, 0.005)]
        else:
            assert trace[first_sleep - 1] == ("unlock", "all")
            assert choices == [(0.75, 1.25)]
        assert pass_number[0] == 2
    finally:
        if lease is not None:
            lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", [*_ADMIN_SITES, "monitor", "callback", "notify"])
async def test_thread_callers_delegate_to_the_shared_worker_helper(monkeypatch, tmp_path, site):
    from functools import partial

    from lionagi.ln import concurrency

    routes = []
    native = concurrency.run_sync

    async def delegated(func, *args, **kwargs):
        routes.append((func, args, kwargs))
        return await native(func, *args, **kwargs)

    if site in _ADMIN_SITES:
        from lionagi.studio.services import admin

        probe = _WorkerProbe(_admin_result(admin, site))
        subject, owner, _, _, _ = _admin_subject(monkeypatch, tmp_path, site, probe)
        monkeypatch.setattr(owner, "_run_sync", delegated, raising=False)
    elif site == "monitor":
        from lionagi.cli import monitor
        from lionagi.service import _subagent_admission as admission

        probe = _WorkerProbe({"pools": []})
        monkeypatch.setattr(admission, "subagent_admission_status", probe.worker)
        # This caller deliberately keeps its import local.
        monkeypatch.setattr(concurrency, "run_sync", delegated)
        subject = monitor._subagent_status
    else:
        from lionagi.cli.orchestrate import _notify
        from lionagi.state.lifecycle import callbacks

        probe = _WorkerProbe(None)
        subject, _ = _delivery_subject(monkeypatch, tmp_path, site, probe.worker)
        owner = callbacks if site == "callback" else _notify
        monkeypatch.setattr(owner, "_run_sync", delegated, raising=False)
    try:
        await subject()
        selected = [
            (func, args, kwargs)
            for func, args, kwargs in routes
            if (func.func if isinstance(func, partial) else func) == probe.worker
        ]
        assert len(selected) == 1, "the caller bypassed the shared worker helper"
        func, args, kwargs = selected[0]
        assert kwargs == ({"abandon_on_cancel": True} if site in {"callback", "notify"} else {})
        assert isinstance(func, partial) is (
            site in {"phantom", "health_scan", "transition_scan", "transition_phantom"}
        )
        if isinstance(func, partial):
            assert args == ()
        assert len(probe.calls) == 1
        assert probe.calls[0][2] != threading.get_ident()
    finally:
        probe.release.set()
        await _wait_thread_event(probe.finished)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["lock", "quick", "poll", "restart", "tick"])
async def test_delay_callers_delegate_to_the_shared_sleep_helper(monkeypatch, tmp_path, site):
    from lionagi.ln import concurrency

    native = concurrency.sleep
    routes = []

    async def delegated(seconds):
        routes.append(seconds)
        await native(seconds)

    if site in {"lock", "quick", "poll"}:
        from lionagi.service import _subagent_admission as admission

        monkeypatch.setattr(admission, "_sleep", delegated, raising=False)
        # Reuse the full caller exercise, including its delay and cleanup
        # assertions, with a spy around the real shared helper.
        if site == "lock":
            await test_admission_lock_retry_preserves_spacing_and_closes_failed_fd(
                monkeypatch, tmp_path, None
            )
        else:
            await test_admission_wait_releases_slots_and_locks_before_retry(
                monkeypatch, tmp_path, site
            )
        assert routes == ([0.02] if site == "lock" else [0.003] if site == "quick" else [1.0])
    else:
        from lionagi.studio.scheduler import engine as engine_module

        monkeypatch.setattr(engine_module, "_sleep", delegated, raising=False)
        if site == "restart":
            await test_scheduler_restart_preserves_delay_before_stop_or_spawn(monkeypatch, "return")
            assert routes == [2.5]
        else:
            await test_scheduler_tick_delay_keeps_deadline_and_stop_cancellation(monkeypatch, False)
            assert routes == [5.0, 3.0]
