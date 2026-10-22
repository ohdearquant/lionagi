# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every CLI agent takes its machine-wide slot in ``ndjson_from_cli``, before the
child exists, and holds it until the child's group has ended.

Real subprocesses: the slot is a lock another process would contend for, and the
recorded child is the kernel's pid.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

from lionagi.providers import _subagent_admission as admission
from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.providers._subagent_admission import (
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
async def test_a_spawn_that_fails_gives_its_slot_back(capped):
    missing = str(Path(capped) / "no-such-binary")
    stream = ndjson_from_cli([missing], engine="codex")
    with pytest.raises(OSError):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
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
        await real_end_group(proc, **kwargs)

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
    assert len(observations) == 2
    assert spawns == observations[:1]
    assert _await_death(spawns[0])
    assert callbacks == []
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
        await real_end_group(proc, **kwargs)

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
    """A record written only in part does not parse, so the slot would read as unrecorded:
    a short write is refused like a failed one."""
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
        await real_end_group(proc, **kwargs)

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
        await real_end_group(proc, **kwargs)

    monkeypatch.setattr(_cli_subprocess, "end_child_group", _record_end)
    quick = 'import sys; sys.stdout.write(\'{"type": "done"}\\n\')'
    stream = ndjson_from_cli([sys.executable, "-c", quick], engine="codex")
    async with contextlib.aclosing(stream):
        assert [obj async for obj in stream] == [{"type": "done"}]
    assert held_while_ending == [{"all": 1, "codex": 1}]
    assert _pools() == {"all": 0, "codex": 0}


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
