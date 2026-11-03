# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine cancellation owns the CLI process groups its run started."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import psutil
import pytest

from lionhub.orchestration.engines.engine import Engine, EngineRun
from lionhub.providers._cli_subprocess import ndjson_from_cli


def _process_is_live(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_until_gone(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_is_live(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    return not _process_is_live(pid)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
@pytest.mark.slow_timing
async def test_deadline_reaps_cli_group_when_worker_swallows_cancel() -> None:
    """A task can abandon its generator, but its run-owned group still dies."""

    ready = asyncio.Event()
    release = asyncio.Event()
    pids: dict[str, int] = {}
    worker_task: asyncio.Task | None = None

    child_code = "\n".join(
        (
            "import json, os, signal, subprocess, sys, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "desc = subprocess.Popen([sys.executable, '-c', "
            '"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])',
            "print(json.dumps({'leader': os.getpid(), 'descendant': desc.pid}), flush=True)",
            "while True: time.sleep(60)",
        )
    )

    class _DeadlineEngine(Engine):
        run_ref: EngineRun | None = None

        async def _run(self, run: EngineRun, *args, **kwargs):
            nonlocal worker_task
            self.run_ref = run

            async def stubborn_cli_worker() -> None:
                async with contextlib.aclosing(
                    ndjson_from_cli([sys.executable, "-c", child_code], cwd=Path.cwd())
                ) as stream:
                    async for item in stream:
                        pids.update(item)
                        ready.set()
                        while not release.is_set():
                            try:
                                await asyncio.sleep(60)
                            except asyncio.CancelledError:
                                # Deliberately retain the open generator and its child.
                                pass

            worker_task = run.spawn(stubborn_cli_worker())
            await ready.wait()
            await asyncio.sleep(60)

    engine = _DeadlineEngine(deadline_s=1.0, cancel_timeout_s=0.05)
    try:
        result = await engine.run()
        assert result is None
        assert pids.keys() == {"leader", "descendant"}
        assert await _wait_until_gone(pids["leader"])
        assert await _wait_until_gone(pids["descendant"])

        assert engine.run_ref is not None
        record = engine.run_ref.cancellation_records[-1]
        assert record.tasks_abandoned == 1
        assert record.processes_reaped == 1
    finally:
        # Red-test cleanup: the test must not leak either process before the fix.
        # It runs before the worker is released, because the worker's next step
        # reads the child's output and would wait on a live child for good.
        leader = pids.get("leader")
        if leader is not None and _process_is_live(leader):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(leader, signal.SIGKILL)
        for pid in pids.values():
            if _process_is_live(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        release.set()
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        for pid in pids.values():
            await _wait_until_gone(pid)


@pytest.mark.asyncio
async def test_cancel_without_cli_processes_signals_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lionhub.providers._cli_subprocess as cli_subprocess

    called = False

    async def unexpected_cleanup(*args, **kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli_subprocess, "end_child_group", unexpected_cleanup)

    class _StubEngine(Engine):
        async def _run(self, run, *args, **kwargs):  # pragma: no cover
            return None

    run = _StubEngine().new_run()
    task = run.spawn(asyncio.sleep(60))
    assert task is not None
    await asyncio.sleep(0)

    await run.cancel_active()

    assert called is False
    assert run.cancellation_records[-1].tasks_abandoned == 0
    assert run.cancellation_records[-1].processes_reaped == 0


class _FakeProcess:
    """Just the two attributes the tracking map reads off a CLI handle."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode


def _tracked(run, pid: int, returncode: int | None = None) -> _FakeProcess:
    from lionhub.providers._cli_subprocess import SpawnedProcess

    process = _FakeProcess(pid, returncode)
    run._track_cli_process(process, SpawnedProcess(pid=pid, pgid=pid, create_time=1.0))
    return process


class _StubEngineForTracking(Engine):
    async def _run(self, run, *args, **kwargs):  # pragma: no cover - never run
        return None


@pytest.mark.asyncio
async def test_a_reap_that_raises_is_counted_as_abandoned_not_merely_unreaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """processes_reaped == 0 is ambiguous on its own: it is also what a run with
    nothing to reap reports. The failure path has to write its own count, or the
    record cannot say that the children are still running."""
    import lionhub.providers._cli_subprocess as cli_subprocess

    async def always_fails(*args, **kwargs) -> None:
        raise OSError("kill refused")

    monkeypatch.setattr(cli_subprocess, "end_child_group", always_fails)

    run = _StubEngineForTracking().new_run()
    _tracked(run, 424242)

    await run.cancel_active()

    record = run.cancellation_records[-1]
    assert record.processes_abandoned == 1
    assert record.processes_reaped == 0


@pytest.mark.asyncio
async def test_a_reap_that_succeeds_is_not_counted_as_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The must-not-match half: the abandoned count stays 0 on the happy path,
    so a non-zero value means what it says."""
    import lionhub.providers._cli_subprocess as cli_subprocess

    async def succeeds(*args, **kwargs):
        return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli_subprocess, "end_child_group", succeeds)

    run = _StubEngineForTracking().new_run()
    _tracked(run, 424243)

    await run.cancel_active()

    record = run.cancellation_records[-1]
    assert record.processes_reaped == 1
    assert record.processes_abandoned == 0


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("result_kind", ["unconfirmed", "none"])
async def test_unconfirmed_cleanup_remains_tracked_until_group_absence_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, result_kind: str
) -> None:
    import lionhub.providers._cli_subprocess as cli_subprocess

    cleanup_result = (
        cli_subprocess._ChildGroupCleanup.UNCONFIRMED if result_kind == "unconfirmed" else None
    )
    calls = []

    async def cleanup(process, *args, **kwargs):
        calls.append(process)
        return cleanup_result

    monkeypatch.setattr(cli_subprocess, "end_child_group", cleanup)
    run = _StubEngineForTracking().new_run()
    process = _tracked(run, 529999)
    tracked = run._cli_processes[id(process)]

    counts = await run._reap_cli_processes([tracked])

    assert counts == (0, 1), "unconfirmed cleanup must be abandoned, not reaped"
    assert tracked.reaped is False
    assert run._cli_processes[id(process)] is tracked
    assert run._live_cli_processes() == [tracked]

    # A later positive result can retire the same entry; refusing everything
    # would satisfy the first half but leave the run unable to finish cleanup.
    cleanup_result = cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY
    assert await run._reap_cli_processes([tracked]) == (1, 0)
    assert calls == [process, process]
    assert tracked.reaped is True
    assert run._live_cli_processes() == []
    assert id(process) not in run._cli_processes


@pytest.mark.asyncio
async def test_an_exited_cli_process_is_dropped_from_the_tracking_map() -> None:
    """Entries are removed, not just filtered out. Each one strongly retains a
    process handle and its spawn identity for the run's whole lifetime
    otherwise, so a fan-out run accumulates every child it ever started."""
    run = _StubEngineForTracking().new_run()
    still_running = _tracked(run, 424244)
    exited = _tracked(run, 424245, returncode=0)

    assert len(run._cli_processes) == 2

    live = run._live_cli_processes()

    assert [item.process for item in live] == [still_running]
    assert len(run._cli_processes) == 1
    assert id(exited) not in run._cli_processes


@pytest.mark.asyncio
async def test_completed_cli_worker_is_not_signalled() -> None:
    class _StubEngine(Engine):
        async def _run(self, run, *args, **kwargs):  # pragma: no cover
            return None

    class _CompletedProcess:
        pid = 4242
        returncode = 0

        def terminate(self) -> None:  # pragma: no cover - must never run
            raise AssertionError("completed CLI worker was signalled")

        def kill(self) -> None:  # pragma: no cover - must never run
            raise AssertionError("completed CLI worker was signalled")

    from lionhub.providers._cli_subprocess import SpawnedProcess

    run = _StubEngine().new_run()
    run._track_cli_process(
        _CompletedProcess(),
        SpawnedProcess(pid=4242, pgid=4242, create_time=None),
    )

    await run.cancel_active()

    assert run.cancellation_records == []


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
@pytest.mark.slow_timing
async def test_an_exited_leader_does_not_strand_its_surviving_descendant() -> None:
    """A leader that exits on its own is not a finished group.

    The leader here prints its group and returns immediately, leaving behind a
    descendant that ignores SIGTERM. Tracking keyed on the leader's returncode
    dropped the entry the moment it exited, so the one routine that can end the
    survivors through frozen spawn-time witnesses was never called and the
    descendant outlived the run.
    """

    ready = asyncio.Event()
    release = asyncio.Event()
    pids: dict[str, int] = {}
    worker_task: asyncio.Task | None = None

    child_code = "\n".join(
        (
            "import json, os, subprocess, sys",
            "desc = subprocess.Popen([sys.executable, '-c', "
            '"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])',
            "print(json.dumps({'leader': os.getpid(), 'descendant': desc.pid}), flush=True)",
            # The leader leaves; the group does not.
            "sys.exit(0)",
        )
    )

    class _LeaderExitsEngine(Engine):
        run_ref: EngineRun | None = None

        async def _run(self, run: EngineRun, *args, **kwargs):
            nonlocal worker_task
            self.run_ref = run

            async def worker() -> None:
                async with contextlib.aclosing(
                    ndjson_from_cli([sys.executable, "-c", child_code], cwd=Path.cwd())
                ) as stream:
                    async for item in stream:
                        pids.update(item)
                        ready.set()
                        # Never advance the iterator again, so the generator is
                        # retained and its own teardown cannot do the cleanup
                        # this test is about.
                        while not release.is_set():
                            try:
                                await asyncio.sleep(60)
                            except asyncio.CancelledError:
                                pass

            worker_task = run.spawn(worker())
            await ready.wait()
            await asyncio.sleep(60)

    engine = _LeaderExitsEngine(deadline_s=1.0, cancel_timeout_s=0.05)
    try:
        await engine.run()
        assert pids.keys() == {"leader", "descendant"}
        assert await _wait_until_gone(pids["descendant"]), (
            "the descendant outlived the run: its leader had already exited, so the "
            "tracking entry was dropped before the group was ended"
        )
    finally:
        # Before the worker is released: the descendant holds the output pipe
        # open, so the worker's next read would otherwise wait on it. The leader
        # exited on its own and its pid may already belong to someone else.
        descendant = pids.get("descendant")
        if descendant is not None and _process_is_live(descendant):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(descendant, signal.SIGKILL)
        release.set()
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
@pytest.mark.slow_timing
async def test_a_spawn_arriving_after_the_run_drained_is_ended_not_tracked() -> None:
    """The run-scoped observer lives in the context of every task the run made,
    including one that swallowed cancellation and outlived the drain. A spawn
    from such a task used to be filed into a finished run's tracking table,
    where no later pass would ever look at it. Recording it there is the leak;
    the group has to be ended at the point it arrives instead.
    """

    captured: list[tuple[object, object]] = []
    pids: dict[str, int] = {}

    child_code = "\n".join(
        (
            "import json, os, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "print(json.dumps({'leader': os.getpid()}), flush=True)",
            "while True: time.sleep(60)",
        )
    )

    from lionhub.providers._cli_subprocess import _observe_run_cli_spawns

    engine = Engine()
    run = engine.new_run()

    stream = ndjson_from_cli([sys.executable, "-c", child_code], cwd=Path.cwd())
    try:
        # Capture a real (process, spawned) pair without handing it to the run,
        # so the run's own bookkeeping is not what this test is measuring.
        with _observe_run_cli_spawns(lambda proc, spawned: captured.append((proc, spawned))):
            async for item in stream:
                pids.update(item)
                break

        assert captured, "no CLI spawn was observed, so this proves nothing"
        assert _process_is_live(pids["leader"])

        process, spawned = captured[0]
        run._cli_tracking_closed = True
        run._track_cli_process(process, spawned)

        assert id(process) not in run._cli_processes, (
            "the spawn was filed against a drained run, where nothing will sweep it"
        )
        assert await _wait_until_gone(pids["leader"]), (
            "the spawn arrived after the run drained and was left running"
        )
    finally:
        for pid in pids.values():
            if _process_is_live(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process groups")
@pytest.mark.slow_timing
async def test_a_late_spawn_whose_identity_was_not_captured_still_has_its_child_ended() -> None:
    """Without a start time there is no proof the group is ours, so no group
    signal is sent; the direct child is still ours through its handle, and a
    late spawn must not be left running because its identity was never read.
    """
    from lionhub.providers._cli_subprocess import SpawnedProcess, _observe_run_cli_spawns

    captured: list[tuple[object, object]] = []
    pids: dict[str, int] = {}
    child_code = "\n".join(
        (
            "import json, os, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "print(json.dumps({'leader': os.getpid()}), flush=True)",
            "while True: time.sleep(60)",
        )
    )

    run = Engine().new_run()
    stream = ndjson_from_cli([sys.executable, "-c", child_code], cwd=Path.cwd())
    try:
        with _observe_run_cli_spawns(lambda proc, spawned: captured.append((proc, spawned))):
            async for item in stream:
                pids.update(item)
                break

        assert captured, "no CLI spawn was observed, so this proves nothing"
        assert _process_is_live(pids["leader"])

        process, spawned = captured[0]
        run._cli_tracking_closed = True
        run._track_cli_process(
            process, SpawnedProcess(pid=spawned.pid, pgid=spawned.pgid, create_time=None)
        )

        assert await _wait_until_gone(pids["leader"]), (
            "a late spawn with no captured identity was left running"
        )
    finally:
        for pid in pids.values():
            if _process_is_live(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await stream.aclose()


@pytest.mark.asyncio
async def test_a_spawn_during_the_final_cancel_reap_is_ended_not_left_tracked_and_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rescan cap is a stopping rule, not a licence to return with a live child.

    Once the bounded rescans are exhausted, ``cancel_active`` does one last reap
    over the entries in hand. That reap awaits, and a task that is still
    spawning can put another child into the tracking map during the await.
    Nothing sweeps after it, and closing the run's tracking afterwards governs
    only FUTURE spawns -- so the newcomer stayed tracked, unreaped, and alive
    once cancellation had returned, which is the exact outcome the rescan
    exists to prevent.
    """
    import lionhub.providers._cli_subprocess as cli_subprocess

    run = _StubEngineForTracking().new_run()
    ended: list[int] = []
    next_pid = iter(range(525000, 525100))

    monkeypatch.setattr(
        EngineRun,
        "_end_cli_group_now",
        staticmethod(lambda process, spawned: ended.append(spawned.pid)),
    )

    async def reap_and_let_another_arrive(*args, **kwargs):
        # A task that swallowed cancellation, spawning while the reaper works.
        _tracked(run, next(next_pid))
        return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli_subprocess, "end_child_group", reap_and_let_another_arrive)

    _tracked(run, next(next_pid))
    await run.cancel_active()

    # The substantive claim first, so a regression reports what actually broke
    # rather than tripping the vacuity guard on its way there.
    assert run._live_cli_processes() == [], (
        "cancel_active returned with a CLI process still tracked and unreaped: "
        f"{[item.spawned.pid for item in run._live_cli_processes()]}"
    )
    assert ended, (
        "no spawn ever reached the closed-tracking path, so the rescan cap was "
        "never exhausted and this test proves nothing"
    )


class _SlottedFakeProcess:
    """A CLI handle stand-in whose freed memory the next one is likely to get.

    Its size (twelve slots) is chosen so that, under CPython's small-object
    allocator, a handle dropped once its entry is pruned tends to be
    reallocated, at the same address, as the next spawn's handle.
    """

    __slots__ = ("pid", "returncode", *(f"_pad{i}" for i in range(10)))

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None


@pytest.mark.asyncio
async def test_a_spawn_given_a_reaped_handles_address_is_still_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remembering the handles already tried by ``id()`` alone is not enough.

    A reaped entry is pruned from the tracking map. Once nothing else holds its
    handle, a spawn arriving during the next reap can be given the same address,
    and a rescan comparing bare ids reads the newcomer as already tried and
    returns with it still tracked and running.
    """
    import lionhub.providers._cli_subprocess as cli_subprocess

    ended: list[int] = []
    pids = iter(range(526000, 527000))
    monkeypatch.setattr(
        EngineRun,
        "_end_cli_group_now",
        staticmethod(lambda process, spawned: ended.append(spawned.pid)),
    )

    async def cancel_with_late_spawns() -> list[int]:
        run = _StubEngineForTracking().new_run()

        def spawn() -> None:
            pid = next(pids)
            run._track_cli_process(
                _SlottedFakeProcess(pid),
                cli_subprocess.SpawnedProcess(pid=pid, pgid=pid, create_time=1.0),
            )

        async def reap_and_let_another_arrive(*args, **kwargs):
            spawn()
            return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

        monkeypatch.setattr(cli_subprocess, "end_child_group", reap_and_let_another_arrive)
        spawn()
        await run.cancel_active()
        return [item.spawned.pid for item in run._live_cli_processes()]

    for attempt in range(10):
        left = await cancel_with_late_spawns()
        assert left == [], f"attempt {attempt}: cancel_active left {left} tracked and unreaped"
        assert len(ended) == attempt + 1, "the rescan cap was not exhausted on this attempt"


@pytest.mark.asyncio
async def test_cancelling_leaves_tracking_open_for_the_partial_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The must-match half: the tracking door reopens when cancellation returns.

    ``cancel_active`` is not always the end of a run. ``_degrade_export``
    cancels in-flight work and then runs the partial export under the same
    spawn observer, so its spawns are legitimate and still have to be tracked.
    A fix that simply left tracking shut would kill those instead.
    """
    import lionhub.providers._cli_subprocess as cli_subprocess

    run = _StubEngineForTracking().new_run()
    ended: list[int] = []
    next_pid = iter(range(526000, 526100))

    monkeypatch.setattr(
        EngineRun,
        "_end_cli_group_now",
        staticmethod(lambda process, spawned: ended.append(spawned.pid)),
    )

    async def reap_and_let_another_arrive(*args, **kwargs):
        # Keeps arriving, so the rescans run out and the door is shut for the
        # final reap; that is the state this test needs to see undone.
        _tracked(run, next(next_pid))
        return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli_subprocess, "end_child_group", reap_and_let_another_arrive)

    _tracked(run, next(next_pid))
    await run.cancel_active()

    assert ended, "the tracking door was never shut, so this test proves nothing"
    assert run._cli_tracking_closed is False
    later = _tracked(run, 526999)
    assert id(later) in run._cli_processes, (
        "a spawn after cancellation was refused tracking, which is what the "
        "partial export path depends on"
    )


@pytest.mark.asyncio
async def test_a_spawn_after_run_returns_is_ended_where_it_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing sweeps a run again once ``run()`` has returned, so its tracking
    has to be closed on the way out. A task that outlived the drain can still
    spawn, and that spawn is ended at once rather than filed for a sweep that
    never comes."""
    ended: list[int] = []
    monkeypatch.setattr(
        EngineRun,
        "_end_cli_group_now",
        staticmethod(lambda process, spawned: ended.append(spawned.pid)),
    )

    class _QuickEngine(Engine):
        run_ref: EngineRun | None = None

        async def _run(self, run: EngineRun, *args, **kwargs):
            self.run_ref = run
            return None

    engine = _QuickEngine()
    await engine.run()

    assert engine.run_ref is not None
    late = _tracked(engine.run_ref, 527001)

    assert id(late) not in engine.run_ref._cli_processes
    assert ended == [527001]


@pytest.mark.asyncio
async def test_a_cli_started_by_the_partial_export_belongs_to_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export that follows a budget or deadline stop still runs under the
    run's spawn tracking, so a CLI it leaves running is reaped with the run."""
    import lionhub.providers._cli_subprocess as cli_subprocess
    from lionhub.orchestration.engines.engine import EngineBudgetError
    from lionhub.providers._cli_subprocess import SpawnedProcess

    reaped: list[int] = []

    async def records_reap(process, *args, **kwargs):
        reaped.append(process.pid)
        return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli_subprocess, "end_child_group", records_reap)

    class _BudgetEngine(Engine):
        async def _run(self, run: EngineRun, *args, **kwargs):
            raise EngineBudgetError("no agents left")

        async def _partial_export(self, run: EngineRun, *args, **kwargs):
            observer = cli_subprocess._RUN_CLI_SPAWN_OBSERVER.get()
            assert observer is not None, "the partial export ran outside the run's spawn tracking"
            observer(_FakeProcess(527101), SpawnedProcess(pid=527101, pgid=527101, create_time=1.0))
            return None

    await _BudgetEngine().run()

    assert reaped == [527101]


@pytest.mark.asyncio
async def test_a_cli_still_running_at_a_budget_stop_is_reaped_before_the_partial_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial export reads a stable snapshot, and work past the budget is
    not supposed to keep burning while it runs. A run-owned CLI that is still
    live when the budget stops the run is reaped before the export starts, even
    when no task is left to cancel."""
    import lionhub.providers._cli_subprocess as cli_subprocess
    from lionhub.orchestration.engines.engine import EngineBudgetError

    reaped: list[int] = []
    reaped_before_export: list[int] = []

    async def records_reap(process, *args, **kwargs):
        reaped.append(process.pid)
        return cli_subprocess._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli_subprocess, "end_child_group", records_reap)

    class _BudgetEngine(Engine):
        async def _run(self, run: EngineRun, *args, **kwargs):
            _tracked(run, 527201)
            raise EngineBudgetError("no agents left")

        async def _partial_export(self, run: EngineRun, *args, **kwargs):
            reaped_before_export.extend(reaped)
            return None

    await _BudgetEngine().run()

    assert reaped_before_export == [527201]
