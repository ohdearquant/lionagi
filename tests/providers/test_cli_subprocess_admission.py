# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every CLI agent takes its machine-wide slot in ``ndjson_from_cli``, before the
child exists, and holds it until the child's group has ended.

Real subprocesses: the slot is a lock another process would contend for, and the
recorded child is the kernel's pid.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.service import _subagent_admission as admission
from lionagi.service._subagent_admission import (
    SubagentAdmissionRefusedError,
    subagent_admission_status,
)

# Emits one object, then blocks until killed.
_SLEEPER = (
    "import sys, time; "
    'sys.stdout.write(\'{"type": "hello"}\\n\'); sys.stdout.flush(); '
    "time.sleep(30)"
)


@pytest.fixture()
def capped(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "1")
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "0")
    return tmp_path


def _held():
    return subagent_admission_status()["pools"].get("all", {"held": 0, "holders": []})


def _is_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _await_death(pid: int, deadline: float = 5.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if not _is_alive(pid):
            return True
        time.sleep(0.05)
    return not _is_alive(pid)


@pytest.mark.asyncio
async def test_a_spawn_at_the_cap_is_refused_before_its_child_starts(capped):
    marker = capped / "second-started"
    first = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    async with contextlib.aclosing(first):
        assert (await first.__anext__()) == {"type": "hello"}
        held = _held()
        assert held["held"] == 1
        [holder] = held["holders"]
        assert isinstance(holder["child_pid"], int) and holder["engine"] == "codex"

        second = ndjson_from_cli(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], engine="claude"
        )
        with pytest.raises(SubagentAdmissionRefusedError, match="cap of 1"):
            async with contextlib.aclosing(second):
                await second.__anext__()
        assert not marker.exists()
    # The first stream's teardown ended its child and gave the slot back.
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_child_that_finishes_gives_its_slot_to_the_next_spawn(capped):
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    for _ in range(2):
        stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
        async with contextlib.aclosing(stream):
            assert [obj async for obj in stream] == [{"type": "done"}]
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_spawn_that_fails_gives_its_slot_back(capped, monkeypatch):
    from lionagi._provider_errors import _WorkerSpawnAdmissionError

    spawns = []

    async def unexpected_spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        raise AssertionError("missing executables must be refused before Popen")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    missing = str(Path(capped) / "no-such-binary")
    stream = ndjson_from_cli([missing], engine="codex")
    with pytest.raises(_WorkerSpawnAdmissionError, match="unavailable before launch"):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert spawns == []
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_an_input_that_cannot_be_encoded_is_refused_before_admission(capped, monkeypatch):
    from lionagi.providers import _cli_subprocess

    marker = capped / "started"
    admissions = []
    real_admit = _cli_subprocess.admit_subagent

    async def _spy(**kwargs):
        admissions.append(kwargs)
        return await real_admit(**kwargs)

    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _spy)
    stream = ndjson_from_cli(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        stdin_data="\ud800",
        engine="codex",
    )
    with pytest.raises(UnicodeEncodeError):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert admissions == []
    assert not marker.exists()
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_cap_set_and_an_unreadable_start_time_refuses_and_frees_the_slot(
    capped, monkeypatch
):
    """A child whose start time cannot be read is not recorded: its slot would stop counting
    the moment this holder exits, so the spawn is refused and the child is ended."""
    from lionagi.providers import _cli_subprocess

    monkeypatch.setattr(_cli_subprocess, "_SPAWN_OBSERVATION_BUDGET", 0.05)
    marker = capped / "started"
    spawns = []
    callbacks = []
    observations = []
    real_observe = _cli_subprocess.observe_spawned
    real_end_group = _cli_subprocess.end_child_group

    def _no_start_time(pid, *, expected_marker):
        observations.append(pid)
        observed = real_observe(pid, expected_marker=expected_marker)
        return _cli_subprocess.SpawnedProcess(
            pid=observed.pid, pgid=observed.pgid, create_time=None
        )

    async def _record_end(proc, **kwargs):
        spawns.append(proc.pid)
        return await real_end_group(proc, **kwargs)

    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _no_start_time)
    monkeypatch.setattr(_cli_subprocess, "end_child_group", _record_end)
    stream = ndjson_from_cli(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close(); import time; time.sleep(30)"],
        engine="codex",
        on_spawn=callbacks.append,
    )
    with pytest.raises(SubagentAdmissionRefusedError, match="start time"):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert len(observations) > 2
    assert set(observations) == set(spawns)
    assert len(spawns) == 1
    assert _await_death(spawns[0])
    assert callbacks == []
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_start_time_that_reads_only_after_the_launcher_execs_is_admitted(
    capped, monkeypatch
):
    """A launcher script's marker reads as absent until its interpreter execs; a spawn whose
    first reads come back empty is admitted once a later read binds the child."""
    from lionagi.providers import _cli_subprocess

    observations = []
    real_observe = _cli_subprocess.observe_spawned

    def _late_start_time(pid, *, expected_marker):
        observations.append(pid)
        observed = real_observe(pid, expected_marker=expected_marker)
        if len(observations) <= 3:
            return _cli_subprocess.SpawnedProcess(
                pid=observed.pid, pgid=observed.pgid, create_time=None
            )
        return observed

    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _late_start_time)
    seen = []
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex", on_spawn=seen.append)
    async with contextlib.aclosing(stream):
        assert (await stream.__anext__()) == {"type": "hello"}
        [spawned] = seen
        assert spawned.create_time is not None
        assert len(observations) == 4
        [holder] = _held()["holders"]
        assert holder["child_pid"] == spawned.pid
    assert _await_death(spawned.pid)
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_child_that_exits_before_its_start_time_reads_is_refused_at_once(
    capped, monkeypatch
):
    """Re-reading stops when the child is gone, so a child that exits at start is refused
    without waiting out the observation budget."""
    from lionagi.providers import _cli_subprocess

    monkeypatch.setattr(_cli_subprocess, "_SPAWN_OBSERVATION_BUDGET", 60.0)
    real_observe = _cli_subprocess.observe_spawned

    def _no_start_time(pid, *, expected_marker):
        observed = real_observe(pid, expected_marker=expected_marker)
        return _cli_subprocess.SpawnedProcess(
            pid=observed.pid, pgid=observed.pgid, create_time=None
        )

    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _no_start_time)
    stream = ndjson_from_cli([sys.executable, "-c", "pass"], engine="codex")
    started = time.monotonic()
    with pytest.raises(SubagentAdmissionRefusedError, match="start time"):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert time.monotonic() - started < 30.0
    assert _held()["held"] == 0


def _reread_without_a_start_time(monkeypatch, module, observations, ended, rereading):
    real_observe = module.observe_spawned
    real_end_group = module.end_child_group

    def _no_start_time(pid, *, expected_marker):
        observations.append(pid)
        if len(observations) >= 3:
            rereading.set()
        observed = real_observe(pid, expected_marker=expected_marker)
        return module.SpawnedProcess(pid=observed.pid, pgid=observed.pgid, create_time=None)

    async def _record_end(proc, **kwargs):
        ended.append((proc.pid, _held()["held"]))
        return await real_end_group(proc, **kwargs)

    monkeypatch.setattr(module, "observe_spawned", _no_start_time)
    monkeypatch.setattr(module, "end_child_group", _record_end)


@pytest.mark.asyncio
async def test_a_caller_cancelled_while_the_start_time_is_reread_keeps_the_slot_until_the_child_ends(
    capped, monkeypatch
):
    """The caller unwinds at once; the spawn keeps re-reading, then refuses and ends the
    child, and the slot is held until the child is gone."""
    from lionagi.providers import _cli_subprocess

    monkeypatch.setattr(_cli_subprocess, "_SPAWN_OBSERVATION_BUDGET", 1.5)
    observations, ended, rereading = [], [], asyncio.Event()
    _reread_without_a_start_time(monkeypatch, _cli_subprocess, observations, ended, rereading)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.wait_for(rereading.wait(), timeout=5.0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    pid = observations[0]
    assert _is_alive(pid)
    assert _held()["held"] == 1
    assert ended == []

    await _join_owned_cli_tasks(_cli_subprocess)
    assert ended == [(pid, 1)]
    assert _await_death(pid)
    assert _held()["held"] == 0
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_spawn_cancelled_while_the_start_time_is_reread_ends_the_child_and_frees_the_slot(
    capped, monkeypatch
):
    """Cancelling the spawn itself inside the re-read window, as a loop shutdown does, ends
    the child before the slot is given back."""
    from lionagi.providers import _cli_subprocess

    monkeypatch.setattr(_cli_subprocess, "_SPAWN_OBSERVATION_BUDGET", 60.0)
    observations, ended, rereading = [], [], asyncio.Event()
    _reread_without_a_start_time(monkeypatch, _cli_subprocess, observations, ended, rereading)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.wait_for(rereading.wait(), timeout=5.0)
    [spawn] = [task for task in _cli_subprocess._RETAINED_CLI_TASKS if not task.done()]
    spawn.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _join_owned_cli_tasks(_cli_subprocess)
    assert time.monotonic() - started < 30.0
    pid = observations[0]
    assert ended == [(pid, 1)]
    assert _await_death(pid)
    assert _held()["held"] == 0
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_launcher_script_run_through_usr_bin_env_is_admitted(capped, tmp_path):
    """The shape of a node-script CLI (`#!/usr/bin/env node`): the kernel runs /usr/bin/env,
    which execs the interpreter in the same pid. On macOS the marker of /usr/bin/env cannot be
    read, so a spawn that read only at once was refused. On Linux the marker reads at once, so
    there this checks only that the launcher shape is admitted; the re-read itself is pinned by
    the patched tests above."""
    interpreter = Path(sys.executable)
    launcher = tmp_path / "launcher"
    launcher.write_text(f"#!/usr/bin/env {interpreter.name}\n{_SLEEPER}\n")
    launcher.chmod(0o755)
    env = {**os.environ, "PATH": f"{interpreter.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    seen = []
    stream = ndjson_from_cli([str(launcher)], engine="codex", env=env, on_spawn=seen.append)
    async with contextlib.aclosing(stream):
        assert (await stream.__anext__()) == {"type": "hello"}
        [spawned] = seen
        assert spawned.create_time is not None
    assert _await_death(spawned.pid)
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_slot_that_cannot_record_its_child_refuses_and_ends_the_child(capped, monkeypatch):
    """A slot that cannot name its child would read free once this holder exited while the
    child ran, so the spawn is refused, the child's group is ended and the slot is freed."""
    from lionagi.providers import _cli_subprocess

    real_write = admission._Slot.write
    writes = []
    ended = []
    callbacks = []
    real_end_group = _cli_subprocess.end_child_group

    def _second_write_fails(slot):
        writes.append(slot.pool)
        if len(writes) > 1:
            raise OSError(28, "No space left on device")
        real_write(slot)

    async def _record_end(proc, **kwargs):
        ended.append((proc.pid, kwargs["spawned"].create_time, _held()["held"]))
        return await real_end_group(proc, **kwargs)

    monkeypatch.setattr(admission._Slot, "write", _second_write_fails)
    monkeypatch.setattr(_cli_subprocess, "end_child_group", _record_end)
    stream = ndjson_from_cli(
        [sys.executable, "-c", _SLEEPER], engine="codex", on_spawn=callbacks.append
    )
    with pytest.raises(SubagentAdmissionRefusedError, match="could not record") as refused:
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert refused.value.pool == "all"
    assert writes == ["all", "all"]
    [(pid, started, held_while_ending)] = ended
    assert started is not None
    # The slot is given back after the group is ended, not before.
    assert held_while_ending == 1
    assert _await_death(pid)
    assert callbacks == []
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_slot_written_in_part_refuses_and_ends_the_child(capped, monkeypatch):
    """A record written only in part may not parse, or may run into an older record and
    name another child, so a short write is refused like a failed one."""
    from lionagi.providers import _cli_subprocess

    real_pwrite = os.pwrite
    lengths = []
    ended = []

    def _second_write_short(fd, data, offset):
        lengths.append(len(data))
        if len(lengths) > 1:
            return real_pwrite(fd, data[: len(data) // 2], offset)
        return real_pwrite(fd, data, offset)

    real_end_group = _cli_subprocess.end_child_group

    async def _record_end(proc, **kwargs):
        ended.append(proc.pid)
        return await real_end_group(proc, **kwargs)

    monkeypatch.setattr(admission.os, "pwrite", _second_write_short)
    monkeypatch.setattr(_cli_subprocess, "end_child_group", _record_end)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    with pytest.raises(SubagentAdmissionRefusedError, match="of the record's") as refused:
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert refused.value.pool == "all"
    assert len(lengths) == 2
    [pid] = ended
    assert _await_death(pid)
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_slot_written_in_part_keeps_its_lock_when_the_group_end_raises(
    capped, monkeypatch, caplog
):
    """A record written only in part would not hold the slot once its holder exited, so
    when the child's group cannot be ended the lock is kept until this process exits."""
    from lionagi.providers import _cli_subprocess

    real_admit = _cli_subprocess.admit_subagent
    real_pwrite = os.pwrite
    lengths = []
    pids = []
    leases = []

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    def _second_write_short(fd, data, offset):
        lengths.append(len(data))
        if len(lengths) > 1:
            return real_pwrite(fd, data[: len(data) // 2], offset)
        return real_pwrite(fd, data, offset)

    async def _end_raises(proc, **kwargs):
        pids.append(proc.pid)
        raise RuntimeError("the group could not be ended")

    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    monkeypatch.setattr(admission.os, "pwrite", _second_write_short)
    monkeypatch.setattr(_cli_subprocess, "end_child_group", _end_raises)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    with pytest.raises(SubagentAdmissionRefusedError, match="of the record's") as raised:
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert raised.value.pool == "all"
    assert "failed worker admission cleanup remains unconfirmed" in caplog.text
    assert len(lengths) == 2
    [pid] = pids
    [lease] = leases
    try:
        assert _is_alive(pid)
        assert _held()["held"] == 1
        os.kill(pid, signal.SIGKILL)
        assert _await_death(pid)
        # Kept by its lock, not by the record, so the slot still counts once the child
        # has gone, until this process exits.
        assert _held()["held"] == 1
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        lease.release()
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_a_record_written_over_a_longer_stale_one_names_this_child(capped):
    """A free slot can still hold a longer record from a holder that died. The new record
    is cut to its own length, so the file reads as this child's, and the slot stays held
    for it once its holder exits."""
    pool_dir = admission._pools_root() / "all"
    pool_dir.mkdir(parents=True, mode=0o700)
    stale = {"cap": 1, "child_pid": None, "child_started": None, "engine": "codex"}
    stale["workload"] = "x" * 400
    (pool_dir / "0.lock").write_text(json.dumps(stale, sort_keys=True))
    callbacks = []
    stream = ndjson_from_cli(
        [sys.executable, "-c", _SLEEPER], engine="codex", on_spawn=callbacks.append
    )
    async with contextlib.aclosing(stream):
        await stream.__anext__()
        [spawned] = callbacks
        record = admission._read_record(pool_dir / "0.lock")
        assert record is not None
        assert record["child_pid"] == spawned.pid
        assert record["child_started"] == spawned.create_time
        assert admission._recorded_child_runs(record)
    assert _await_death(spawned.pid)
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_every_slot_stays_held_until_the_childs_group_is_ended(capped, monkeypatch):
    """Each pool's slot is released after ``end_child_group`` returns, never before it runs."""
    from lionagi.providers import _cli_subprocess

    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS_CODEX", "1")
    real_end_group = _cli_subprocess.end_child_group
    held_while_ending = []

    def _pools():
        pools = subagent_admission_status()["pools"]
        return {name: pools.get(name, {"held": 0})["held"] for name in ("all", "codex")}

    async def _record_end(proc, **kwargs):
        held_while_ending.append(_pools())
        return await real_end_group(proc, **kwargs)

    monkeypatch.setattr(_cli_subprocess, "end_child_group", _record_end)
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
    async with contextlib.aclosing(stream):
        assert [obj async for obj in stream] == [{"type": "done"}]
    assert held_while_ending == [{"all": 1, "codex": 1}]
    assert _pools() == {"all": 0, "codex": 0}


@pytest.mark.asyncio
async def test_a_cancel_during_the_spawn_keeps_the_slot_until_the_late_child_is_ended(
    capped, monkeypatch
):
    """A cancellation that lands while the creation call still runs unwinds the caller
    before the child arrives. The slot stays held until that child's group is ended."""
    from lionagi.providers import _cli_subprocess

    real_create = asyncio.create_subprocess_exec
    real_end = _cli_subprocess._end_group_with_evidence
    created = asyncio.Event()
    finish_creation = asyncio.Event()
    pids = []
    ended = []

    async def _slow_create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        pids.append(child.pid)
        created.set()
        await finish_creation.wait()
        return child

    def _record_end(proc, *args, **kwargs):
        ended.append((proc.pid, _held()["held"]))
        return real_end(proc, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _slow_create)
    monkeypatch.setattr(_cli_subprocess, "_end_group_with_evidence", _record_end)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.wait_for(created.wait(), timeout=5.0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # The caller has unwound; the child exists and the creation call has not returned.
    [pid] = pids
    assert _is_alive(pid)
    assert _held()["held"] == 1
    assert ended == []

    finish_creation.set()
    deadline = time.monotonic() + 5.0
    while _held()["held"] and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert ended == [(pid, 1)]
    assert _await_death(pid)
    assert _held()["held"] == 0
    await stream.aclose()


def _cancel_the_caller_as_the_spawn_finishes(monkeypatch, first, pids):
    """The caller is cancelled while it waits on the spawn, and the spawn finishes in that
    same step, so the caller unwinds over a spawn that is already done."""
    real_create = asyncio.create_subprocess_exec

    async def _cancel_then_create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        pids.append(child.pid)
        first[0].cancel()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _cancel_then_create)


@pytest.mark.asyncio
async def test_a_cancel_as_the_spawn_finishes_ends_the_child_before_the_slot_frees(
    capped, monkeypatch
):
    from lionagi.providers import _cli_subprocess

    real_end = _cli_subprocess._end_group_with_evidence
    first: list[asyncio.Future] = []
    pids: list[int] = []
    ended = []

    def _record_end(proc, *args, **kwargs):
        # Inside the caller's own task only when the unwinding caller ended it itself, not
        # a done-callback run later by the loop.
        ended.append((proc.pid, _held()["held"], asyncio.current_task() is first[0]))
        return real_end(proc, *args, **kwargs)

    _cancel_the_caller_as_the_spawn_finishes(monkeypatch, first, pids)
    monkeypatch.setattr(_cli_subprocess, "_end_group_with_evidence", _record_end)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first.append(asyncio.ensure_future(stream.__anext__()))
    with pytest.raises(asyncio.CancelledError):
        await first[0]
    await _join_owned_cli_tasks(_cli_subprocess)
    [pid] = pids
    # Retained cleanup owns the late child while the capacity is still held.
    assert ended == [(pid, 1, False)]
    assert _held()["held"] == 0
    assert _await_death(pid)
    await stream.aclose()


@pytest.mark.asyncio
async def test_an_abandoned_child_whose_group_end_raises_keeps_its_slot(
    capped, monkeypatch, caplog
):
    """Nothing shows that group has ended, so its slot stays held while the child runs,
    with or without this process; the caller still sees its own cancellation."""
    from lionagi.providers import _cli_subprocess

    real_admit = _cli_subprocess.admit_subagent
    first: list[asyncio.Future] = []
    pids: list[int] = []
    leases = []
    ends = []

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    def _end_raises(proc, *args, **kwargs):
        ends.append(asyncio.current_task() is first[0])
        raise RuntimeError("the group could not be signalled")

    _cancel_the_caller_as_the_spawn_finishes(monkeypatch, first, pids)
    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    monkeypatch.setattr(_cli_subprocess, "_end_group_with_evidence", _end_raises)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first.append(asyncio.ensure_future(stream.__anext__()))
    with pytest.raises(asyncio.CancelledError):
        await first[0]
    await _join_owned_cli_tasks(_cli_subprocess)
    [pid] = pids
    [lease] = leases
    try:
        assert ends == [False]
        _exit_the_holder(lease)
        assert _is_alive(pid)
        assert _held()["held"] == 1
        assert "slots stay held until this process exits" in caplog.text
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _await_death(pid)
    assert _held()["held"] == 0
    await stream.aclose()


async def _join_owned_cli_tasks(module) -> None:
    # Late cleanup is retained asynchronous work; give it the loop before any
    # blocking death probe or simulated holder exit in these tests.
    tasks = tuple(module._RETAINED_CLI_TASKS)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)


def _exit_the_holder(lease) -> None:
    """What this process exiting does to the locks it still holds: each is dropped and
    its record stays in the file."""
    import fcntl

    for slot in lease.slots:
        if slot.fd >= 0:
            fd, slot.fd = slot.fd, -1
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@pytest.mark.asyncio
async def test_a_cancel_during_the_spawn_keeps_the_slot_for_the_child_when_its_end_raises(
    capped, monkeypatch, caplog
):
    """The late child is named in the slot before its group is ended, so a failed end
    leaves the slot held while that child runs, even once its holder has exited."""
    from lionagi.providers import _cli_subprocess

    real_admit = _cli_subprocess.admit_subagent
    real_create = asyncio.create_subprocess_exec
    created = asyncio.Event()
    finish_creation = asyncio.Event()
    pids = []
    leases = []

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    async def _slow_create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        pids.append(child.pid)
        created.set()
        await finish_creation.wait()
        return child

    def _end_raises(proc, *args, **kwargs):
        raise RuntimeError("the group could not be signalled")

    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _slow_create)
    monkeypatch.setattr(_cli_subprocess, "_end_group_with_evidence", _end_raises)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.wait_for(created.wait(), timeout=5.0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    [pid] = pids
    [lease] = leases
    try:
        finish_creation.set()
        deadline = time.monotonic() + 5.0
        while "could not end the group" not in caplog.text and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await _join_owned_cli_tasks(_cli_subprocess)
        _exit_the_holder(lease)
        assert _is_alive(pid)
        assert _held()["held"] == 1
        assert "slots stay held until this process exits" in caplog.text
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _await_death(pid)
    assert _held()["held"] == 0
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["teardown", "observer"])
async def test_a_group_end_that_raises_keeps_the_slot_while_the_child_runs(
    capped, monkeypatch, where
):
    """At the stream's end and when the spawn observer fails, a group end that raises
    leaves the holder lock intact even after the direct child has gone."""
    from lionagi.providers import _cli_subprocess

    pids = []
    leases = []
    real_admit = _cli_subprocess.admit_subagent

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    async def _end_raises(proc, **kwargs):
        raise RuntimeError("the group could not be ended")

    def _observe(spawned):
        pids.append(spawned.pid)
        if where == "observer":
            raise ValueError("the observer failed")

    monkeypatch.setattr(_cli_subprocess, "end_child_group", _end_raises)
    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex", on_spawn=_observe)
    error_type = RuntimeError if where == "teardown" else ValueError
    error_text = "could not be ended" if where == "teardown" else "observer failed"
    with pytest.raises(error_type, match=error_text):
        if where == "teardown":
            assert (await stream.__anext__()) == {"type": "hello"}
            await stream.aclose()
        else:
            await stream.__anext__()
    [pid] = pids
    try:
        assert _is_alive(pid)
        assert _held()["held"] == 1
        os.kill(pid, signal.SIGKILL)
        assert _await_death(pid)
        assert _held()["held"] == 1, "unconfirmed descendants still require the holder lock"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        for lease in leases:
            lease.release()
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_an_abandoned_child_without_a_start_time_keeps_the_lock_when_its_end_raises(
    capped, monkeypatch, caplog
):
    """A record without the child's start time would not hold the slot, so the lock is
    kept until this process exits."""
    from lionagi.providers import _cli_subprocess

    real_admit = _cli_subprocess.admit_subagent
    real_observe = _cli_subprocess.observe_spawned
    first: list[asyncio.Future] = []
    pids: list[int] = []
    leases = []

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    def _no_start_time(pid, *, expected_marker):
        observed = real_observe(pid, expected_marker=expected_marker)
        return _cli_subprocess.SpawnedProcess(
            pid=observed.pid, pgid=observed.pgid, create_time=None
        )

    async def _end_raises(proc, *args, **kwargs):
        raise RuntimeError("the group could not be signalled")

    _cancel_the_caller_as_the_spawn_finishes(monkeypatch, first, pids)
    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _no_start_time)
    monkeypatch.setattr(_cli_subprocess, "_SPAWN_OBSERVATION_BUDGET", 0.05)
    monkeypatch.setattr(_cli_subprocess, "end_child_group", _end_raises)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first.append(asyncio.ensure_future(stream.__anext__()))
    with pytest.raises(asyncio.CancelledError):
        await first[0]
    await _join_owned_cli_tasks(_cli_subprocess)
    [pid] = pids
    [lease] = leases
    try:
        os.kill(pid, signal.SIGKILL)
        assert _await_death(pid)
        assert _held()["held"] == 1
        assert "slots stay held until this process exits" in caplog.text
    finally:
        lease.release()
    assert _held()["held"] == 0
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_cap_set_and_a_second_start_time_read_succeeds_runs_and_records_it(
    capped, monkeypatch
):
    """One unreadable observation is retried before the spawn is refused; the second read
    records the start time the slot needs to outlive this holder."""
    from lionagi.providers import _cli_subprocess

    real_observe = _cli_subprocess.observe_spawned
    observations = []

    def _first_read_fails(pid, *, expected_marker):
        observed = real_observe(pid, expected_marker=expected_marker)
        observations.append(observed)
        if len(observations) == 1:
            return _cli_subprocess.SpawnedProcess(
                pid=observed.pid, pgid=observed.pgid, create_time=None
            )
        return observed

    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _first_read_fails)
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
    async with contextlib.aclosing(stream):
        assert [obj async for obj in stream] == [{"type": "done"}]
    assert len(observations) == 2
    assert observations[-1].create_time is not None
    assert _held()["held"] == 0


@pytest.mark.asyncio
async def test_with_no_cap_an_unreadable_start_time_still_runs(monkeypatch):
    """Nothing counts a slot when no pool is capped, so there is no record to keep and the
    child runs as it did before admission existed."""
    from lionagi.providers import _cli_subprocess

    real_observe = _cli_subprocess.observe_spawned
    observations = []

    def _no_start_time(pid, *, expected_marker):
        observations.append(pid)
        observed = real_observe(pid, expected_marker=expected_marker)
        return _cli_subprocess.SpawnedProcess(
            pid=observed.pid, pgid=observed.pgid, create_time=None
        )

    monkeypatch.setattr(_cli_subprocess, "observe_spawned", _no_start_time)
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
    async with contextlib.aclosing(stream):
        assert [obj async for obj in stream] == [{"type": "done"}]
    assert len(observations) == 1


@pytest.mark.asyncio
async def test_with_no_cap_an_abandoned_child_whose_group_end_raises_logs_no_slot(
    monkeypatch, caplog
):
    """With no cap the lease holds no slot, so the logged failure claims none is kept."""
    from lionagi.providers import _cli_subprocess

    for name in ("LIONAGI_MAX_SUBAGENTS", "LIONAGI_MAX_SUBAGENTS_HEAVY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(admission.engine_cap_env("codex"), raising=False)
    real_admit = _cli_subprocess.admit_subagent
    first: list[asyncio.Future] = []
    pids: list[int] = []
    leases = []

    async def _keep_lease(**kwargs):
        lease = await real_admit(**kwargs)
        leases.append(lease)
        return lease

    def _end_raises(proc, *args, **kwargs):
        raise RuntimeError("the group could not be signalled")

    _cancel_the_caller_as_the_spawn_finishes(monkeypatch, first, pids)
    monkeypatch.setattr(_cli_subprocess, "admit_subagent", _keep_lease)
    monkeypatch.setattr(_cli_subprocess, "_end_group_with_evidence", _end_raises)
    stream = ndjson_from_cli([sys.executable, "-c", _SLEEPER], engine="codex")
    first.append(asyncio.ensure_future(stream.__anext__()))
    with pytest.raises(asyncio.CancelledError):
        await first[0]
    await _join_owned_cli_tasks(_cli_subprocess)
    [pid] = pids
    [lease] = leases
    try:
        assert lease.slots == []
        assert "could not end the group of a child whose spawn was abandoned" in caplog.text
        assert "slots stay held" not in caplog.text
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _await_death(pid)
    await stream.aclose()
