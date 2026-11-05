# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Machine-wide admission for CLI agents: caps per pool, waiting, refusal, and slots
that outlive neither their holder nor their child."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from anyio import to_thread

from lionagi.ln._proc import process_create_time
from lionagi.service import _subagent_admission as admission
from lionagi.service._subagent_admission import (
    SubagentAdmissionRefusedError,
    admit_subagent,
    subagent_admission_status,
)
from lionhub.serve.mcp._terminal_cause import allowed_cause_classes


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    return tmp_path / "home"


def _pools(home: Path) -> Path:
    return home / "leases" / "subagents"


async def _admit(env: dict[str, str], *, engine: str | None = "codex", workload: str = "heavy"):
    return await admit_subagent(engine=engine, workload=workload, environ=env)


@pytest.mark.asyncio
async def test_with_no_cap_an_agent_admits_without_a_slot(home):
    lease = await _admit({})
    assert lease.slots == []
    assert not _pools(home).exists()


@pytest.mark.asyncio
async def test_a_cap_of_zero_means_no_cap(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "0", "LIONAGI_MAX_SUBAGENTS_HEAVY": "0"}
    leases = [await _admit(env) for _ in range(3)]
    assert all(lease.slots == [] for lease in leases)


@pytest.mark.asyncio
async def test_at_a_cap_of_one_the_second_agent_is_refused_naming_the_holder(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    first = await _admit(env)
    try:
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(env)
    finally:
        first.release()
    message = str(refused.value)
    assert "\n" not in message
    assert "cap of 1 (LIONAGI_MAX_SUBAGENTS)" in message
    assert f"pid {os.getpid()}" in message
    assert refused.value.pool == "all"
    assert refused.value.holders == (os.getpid(),)


@pytest.mark.asyncio
async def test_a_released_slot_admits_the_next_agent(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    first = await _admit(env)
    first.release()
    second = await _admit(env)
    assert [slot.pool for slot in second.slots] == ["all"]
    second.release()


@pytest.mark.asyncio
async def test_an_agent_waits_for_a_slot_freed_during_its_wait(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "30"}
    first = await _admit(env)
    admitted = []

    async def second():
        admitted.append(await _admit(env))

    async with anyio.create_task_group() as tg:
        tg.start_soon(second)
        await anyio.sleep(0.3)
        assert admitted == []
        first.release()
    assert len(admitted[0].slots) == 1
    admitted[0].release()


@pytest.mark.asyncio
async def test_the_wait_ends_in_a_refusal_when_nothing_frees(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0.3"}
    first = await _admit(env)
    started = time.monotonic()
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="waited 0.3s"):
            await _admit(env)
    finally:
        first.release()
    assert time.monotonic() - started >= 0.3


@pytest.mark.asyncio
async def test_a_pool_full_of_slots_held_for_this_processs_ancestors_is_refused_without_waiting(
    home,
):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "5"}
    first = await _admit(env)
    # The slot's child is this process's parent: the agent that started this one.
    first.record_child(os.getppid(), process_create_time(os.getppid())[1])
    started = time.monotonic()
    try:
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(env)
    finally:
        first.release()
    assert time.monotonic() - started < 2
    message = str(refused.value)
    assert "waited" not in message
    assert (
        f"the agent that started this one holds it (pid {os.getpid()} (child {os.getppid()}))"
        in message
    )
    assert refused.value.pool == "all"


@pytest.mark.asyncio
async def test_a_slot_whose_recorded_child_ended_is_not_an_ancestors_by_its_pid(home):
    """A pid a recorded child held can be taken by a later process, here this one's
    parent. The slot is then the holder's alone, so the admission waits for it."""
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0.3"}
    first = await _admit(env)
    started = process_create_time(os.getppid())[1]
    assert started is not None
    # Recorded as started an hour before the parent was: a child that has ended.
    first.record_child(os.getppid(), started - 3600)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="waited 0.3s"):
            await _admit(env)
    finally:
        first.release()


@pytest.mark.asyncio
async def test_a_full_pool_with_room_beyond_its_ancestors_slots_is_waited_on(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "2", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0.3"}
    first = await _admit(env)
    first.record_child(os.getppid(), process_create_time(os.getppid())[1])
    second = await _admit(env)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="waited 0.3s"):
            await _admit(env)
    finally:
        second.release()
        first.release()


_HOLDER = textwrap.dedent(
    """
    import sys, time, anyio
    from lionagi.service._subagent_admission import admit_subagent

    async def main():
        await admit_subagent(engine="codex", workload="heavy", environ={"LIONAGI_MAX_SUBAGENTS": "1"})
        print("held", flush=True)
        time.sleep(60)

    anyio.run(main)
    """
)


def _holder_process(home: Path) -> subprocess.Popen:
    env = {**os.environ, "LIONAGI_HOME": str(home)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER], env=env, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


@pytest.mark.asyncio
async def test_a_killed_holder_frees_its_slot(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    holder = _holder_process(home)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match=f"pid {holder.pid}"):
            await _admit(env)
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
    lease = await _admit(env)
    assert len(lease.slots) == 1
    lease.release()


@pytest.mark.asyncio
async def test_a_slot_stays_held_while_its_recorded_child_runs_after_the_holder_is_gone(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state, started = process_create_time(child.pid)
        assert state == "found"
        lease = await _admit(env)
        lease.record_child(child.pid, started)
        # The holder dies without releasing: its lock goes, the record stays.
        for slot in lease.slots:
            os.close(slot.fd)
            slot.fd = -1
        with pytest.raises(SubagentAdmissionRefusedError, match=f"child {child.pid}"):
            await _admit(env)
    finally:
        child.kill()
        child.wait(timeout=10)
    lease = await _admit(env)
    assert len(lease.slots) == 1
    lease.release()


@pytest.mark.asyncio
async def test_a_pool_being_counted_by_a_status_read_is_waited_on_not_refused(home):
    """``li monitor`` counts a pool under its admission lock. An admission that lands
    while it does, with no wait allowed, still admits once the read ends."""
    import fcntl

    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    pool = _pools(home) / "all"
    pool.mkdir(parents=True)
    fd = os.open(pool / ".admission", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def status_read_ends():
        await anyio.sleep(0.1)
        os.close(fd)

    async with anyio.create_task_group() as tg:
        tg.start_soon(status_read_ends)
        lease = await _admit(env)
    assert [slot.pool for slot in lease.slots] == ["all"]
    lease.release()


@pytest.mark.asyncio
async def test_an_admission_lock_held_too_long_is_refused(home, monkeypatch):
    import fcntl

    monkeypatch.setattr(admission, "_ADMISSION_LOCK_SECONDS", 0.2)
    pool = _pools(home) / "all"
    pool.mkdir(parents=True)
    fd = os.open(pool / ".admission", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="admission lock of pool 'all'"):
            await _admit({"LIONAGI_MAX_SUBAGENTS": "1"})
        assert subagent_admission_status({"LIONAGI_MAX_SUBAGENTS": "1"})["pools"]["all"] == {
            "cap": 1,
            "error": "SubagentAdmissionRefusedError",
        }
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_a_status_read_mid_admission_never_turns_a_free_pool_full(home):
    """A status read locks each slot for a moment to count it; a free slot still holding a
    dead holder's record must not read as held when the admission lands in that moment."""
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    pool = _pools(home) / "all"
    pool.mkdir(parents=True)
    for _ in range(50):
        (pool / "0.lock").write_text('{"holder_pid": 999999, "cap": 1, "child_pid": null}')
        stop = anyio.Event()

        async def read_status():
            while not stop.is_set():
                await to_thread.run_sync(subagent_admission_status, env)

        async with anyio.create_task_group() as tg:
            tg.start_soon(read_status)
            lease = await _admit(env)
            stop.set()
        lease.release()


@pytest.mark.asyncio
async def test_a_dead_holders_record_in_a_free_slot_does_not_count(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    pool = _pools(home) / "all"
    pool.mkdir(parents=True)
    # What a SIGKILLed holder leaves: its record, no lock, no child.
    (pool / "0.lock").write_text('{"holder_pid": 999999, "cap": 1, "child_pid": null}')
    lease = await _admit(env)
    assert [slot.path.name for slot in lease.slots] == ["0.lock"]
    lease.release()


@pytest.mark.asyncio
async def test_a_smaller_cap_a_holder_read_bounds_an_agent_reading_a_larger_one(home):
    narrow = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    wide = {"LIONAGI_MAX_SUBAGENTS": "2", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    first = await _admit(narrow)
    try:
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(wide)
    finally:
        first.release()
    assert "at a cap of 1 a holder read (LIONAGI_MAX_SUBAGENTS=2 here)" in str(refused.value)
    assert refused.value.cap == 1
    # With the narrow holder gone the wide cap is the only one again.
    leases = [await _admit(wide) for _ in range(2)]
    for lease in leases:
        lease.release()


_RACER = textwrap.dedent(
    """
    import sys, time, anyio
    from lionagi.service._subagent_admission import SubagentAdmissionRefusedError, admit_subagent

    async def main():
        env = {"LIONAGI_MAX_SUBAGENTS": sys.argv[1], "LIONAGI_MAX_SUBAGENTS_CODEX": sys.argv[2],
               "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
        start = float(sys.argv[3])
        # Asleep until just before the start, then a short spin, so the racers start together.
        time.sleep(max(0.0, start - time.time() - 0.05))
        while time.time() < start:
            pass
        try:
            await admit_subagent(engine="codex", workload=sys.argv[4], environ=env)
        except SubagentAdmissionRefusedError:
            print("refused", flush=True)
            return
        print("admitted", flush=True)
        time.sleep(5)

    anyio.run(main)
    """
)


@pytest.mark.parametrize(
    ("total", "codex", "workloads", "admitted"),
    [
        # Two admitters at a cap of 2 colliding on the same instant: both fit.
        ("2", "0", ["light", "light"], 2),
        # Four at a cap of 2: exactly two, never more and never fewer.
        ("2", "0", ["light"] * 4, 2),
        # Three engine-capped admitters, one heavy: the engine cap of 3 fits all three.
        ("5", "3", ["heavy", "light", "light"], 3),
    ],
)
def test_admitters_racing_on_one_pool_admit_exactly_up_to_its_cap(
    home, total, codex, workloads, admitted
):
    env = {**os.environ, "LIONAGI_HOME": str(home)}
    start = str(time.time() + 4.0)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, total, codex, start, workload],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        for workload in workloads
    ]
    try:
        outcomes = [proc.stdout.readline().strip() for proc in procs]
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(timeout=10)
    assert outcomes.count("admitted") == admitted, outcomes
    assert outcomes.count("refused") == len(workloads) - admitted, outcomes


@pytest.mark.asyncio
async def test_racers_below_a_holder_at_a_higher_index_admit_up_to_the_room_left(home):
    """A holder that read a cap of 3 keeps slot 2; two agents reading a cap of 2 race
    for the one slot left. One admits and one is refused; neither reads the other's
    half-taken slot as a holder and both refuse."""
    wide = {"LIONAGI_MAX_SUBAGENTS": "3", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    leases = [await _admit(wide) for _ in range(3)]
    leases[0].release()
    leases[1].release()
    try:
        env = {**os.environ, "LIONAGI_HOME": str(home)}
        start = str(time.time() + 4.0)
        outcomes = []
        for _round in range(3):
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", _RACER, "2", "0", start, "light"],
                    env=env,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            try:
                outcomes.append(sorted(proc.stdout.readline().strip() for proc in procs))
            finally:
                for proc in procs:
                    proc.kill()
                    proc.wait(timeout=10)
            start = str(time.time() + 3.0)
        assert outcomes == [["admitted", "refused"]] * 3, outcomes
    finally:
        for lease in leases:
            lease.release()


@pytest.mark.asyncio
async def test_an_agent_waiting_on_one_pool_holds_no_slot_in_another(home):
    env = {
        "LIONAGI_MAX_SUBAGENTS_HEAVY": "1",
        "LIONAGI_MAX_SUBAGENTS_CODEX": "1",
        "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "30",
    }
    other = await _admit(env, engine="claude", workload="heavy")  # fills heavy
    waiting = []

    async def heavy_codex():
        waiting.append(await _admit(env, engine="codex", workload="heavy"))

    async with anyio.create_task_group() as tg:
        tg.start_soon(heavy_codex)
        await anyio.sleep(0.3)
        # The heavy codex agent waits on heavy and holds nothing in codex meanwhile.
        light = await _admit(
            {**env, "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}, engine="codex", workload="light"
        )
        assert [slot.pool for slot in light.slots] == ["codex"]
        light.release()
        other.release()
    assert sorted(slot.pool for slot in waiting[0].slots) == ["codex", "heavy"]
    waiting[0].release()


@pytest.mark.asyncio
async def test_a_child_recorded_without_its_start_time_is_not_protected_after_its_holder(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease = await _admit(env)
        lease.record_child(child.pid, None)
        for slot in lease.slots:
            os.close(slot.fd)
            slot.fd = -1
        # A bare pid could be a reused one, so the slot frees with its holder.
        again = await _admit(env)
        assert len(again.slots) == 1
        again.release()
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize(
    ("extra", "read"),
    [pytest.param(0, True, id="at-the-limit"), pytest.param(1, False, id="one-byte-past")],
)
def test_a_record_is_read_up_to_the_byte_limit_and_not_past_it(tmp_path, extra, read):
    import json

    base = len(json.dumps({"note": ""}).encode())
    record = {"note": "x" * (admission._RECORD_BYTES - base + extra)}
    path = tmp_path / "slot"
    path.write_bytes(json.dumps(record).encode())
    assert path.stat().st_size == admission._RECORD_BYTES + extra

    assert (admission._read_record(path) == record) is read
    if not read:
        assert admission._read_record(path) is None


def test_a_record_padded_past_the_byte_limit_is_not_read(tmp_path):
    """The record itself fits; the file does not. Only the extra byte read tells them apart."""
    import json

    body = json.dumps({"note": "x"}).encode()
    path = tmp_path / "slot"
    path.write_bytes(body + b" " * (admission._RECORD_BYTES + 1 - len(body)))
    assert json.loads(path.read_bytes()[: admission._RECORD_BYTES]) == {"note": "x"}

    assert admission._read_record(path) is None


@pytest.mark.parametrize("opener", [b"[", b'{"a":'], ids=["arrays", "objects"])
def test_a_record_nested_past_the_parser_depth_reads_as_no_record(tmp_path, opener):
    path = tmp_path / "slot"
    path.write_bytes((opener * admission._RECORD_BYTES)[: admission._RECORD_BYTES])

    assert admission._read_record(path) is None


@pytest.mark.asyncio
async def test_a_record_of_several_kilobytes_is_still_read_whole(home, monkeypatch):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state, started = process_create_time(child.pid)
        assert state == "found"
        lease = await _admit(env)
        # An 8,000-byte note: a record of several kilobytes is still read whole, so the
        # slot still reads as held.
        for slot in lease.slots:
            slot.record["note"] = "x" * 8000
        lease.record_child(child.pid, started)
        for slot in lease.slots:
            os.close(slot.fd)
            slot.fd = -1
        with pytest.raises(SubagentAdmissionRefusedError, match=f"child {child.pid}"):
            await _admit(env)
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["all", "heavy", "ALL", "../escaped", "a/b", "", "x" * 33, "9x"])
async def test_an_engine_that_cannot_name_a_pool_is_refused(home, engine):
    env = {
        f"LIONAGI_MAX_SUBAGENTS_{engine.upper()}": "1",
        "LIONAGI_MAX_SUBAGENTS": "3",
        "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0",
    }
    if engine == "":
        # An empty engine is no engine: it counts in the total pool only.
        lease = await _admit(env, engine="")
        assert [slot.pool for slot in lease.slots] == ["all"]
        lease.release()
        return
    with pytest.raises(SubagentAdmissionRefusedError, match="cannot name a pool"):
        await _admit(env, engine=engine)
    assert not (_pools(home).parent / "escaped").exists()


@pytest.mark.asyncio
async def test_a_hyphenated_engine_counts_in_the_pool_its_variable_names(home):
    env = {"LIONAGI_MAX_SUBAGENTS_GEMINI_CLI": "1"}
    lease = await _admit(env, engine="gemini-cli")
    try:
        assert [slot.pool for slot in lease.slots] == ["gemini_cli"]
        assert subagent_admission_status(env)["pools"]["gemini_cli"]["held"] == 1
    finally:
        lease.release()


def test_status_reports_a_file_where_a_pool_directory_belongs(home):
    _pools(home).mkdir(parents=True)
    (_pools(home) / "all").write_text("not a directory")
    status = subagent_admission_status({"LIONAGI_MAX_SUBAGENTS": "2"})
    assert status["pools"]["all"] == {"cap": 2, "error": "NotADirectoryError"}


def test_status_does_not_map_a_reserved_engine_variable_onto_a_pool(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "4", "LIONAGI_MAX_SUBAGENTS_ALL": "1"}
    status = subagent_admission_status(env)
    assert status["caps"]["LIONAGI_MAX_SUBAGENTS_ALL"] == 1
    assert status["pools"]["all"]["cap"] == 4


@pytest.mark.asyncio
async def test_an_engine_cap_holds_back_that_engine_only(home):
    env = {"LIONAGI_MAX_SUBAGENTS_CODEX": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    codex = await _admit(env, engine="codex")
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="LIONAGI_MAX_SUBAGENTS_CODEX"):
            await _admit(env, engine="codex")
        claude = await _admit(env, engine="claude")
        assert claude.slots == []
    finally:
        codex.release()


@pytest.mark.asyncio
async def test_at_the_heavy_cap_a_heavy_agent_is_refused_while_a_light_one_admits(home):
    env = {
        "LIONAGI_MAX_SUBAGENTS": "30",
        "LIONAGI_MAX_SUBAGENTS_HEAVY": "1",
        "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0",
    }
    heavy = await _admit(env, workload="heavy")
    try:
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(env, workload="heavy")
        assert refused.value.pool == "heavy"
        assert "heavy codex agent" in str(refused.value)
        light = await _admit(env, workload="light")
        assert [slot.pool for slot in light.slots] == ["all"]
        light.release()
    finally:
        heavy.release()


@pytest.mark.asyncio
async def test_holders_above_a_lower_cap_still_count(home):
    """A process reading a larger cap can hold a high index; a smaller cap counts it."""
    wide = {"LIONAGI_MAX_SUBAGENTS": "3", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    narrow = {"LIONAGI_MAX_SUBAGENTS": "2", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    leases = [await _admit(wide) for _ in range(3)]
    leases[0].release()  # slot 0 is free; slots 1 and 2 are held
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="cap of 2"):
            await _admit(narrow)
    finally:
        for lease in leases:
            lease.release()


@pytest.mark.asyncio
async def test_a_missing_pool_directory_is_created_private(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "2"}
    lease = await _admit(env)
    pool = _pools(home) / "all"
    assert pool.is_dir()
    assert stat.S_IMODE(pool.stat().st_mode) == 0o700
    assert stat.S_IMODE((pool / "0.lock").stat().st_mode) == 0o600
    lease.release()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.asyncio
async def test_an_unwritable_pool_directory_refuses_instead_of_admitting(home):
    root = _pools(home)
    root.mkdir(parents=True)
    root.chmod(0o500)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="cannot be used"):
            await _admit({"LIONAGI_MAX_SUBAGENTS": "2"})
    finally:
        root.chmod(0o700)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LIONAGI_MAX_SUBAGENTS", "thirty"),
        ("LIONAGI_MAX_SUBAGENTS", "-1"),
        ("LIONAGI_MAX_SUBAGENTS_HEAVY", "2.5"),
        ("LIONAGI_MAX_SUBAGENTS_CODEX", "x"),
    ],
)
async def test_a_cap_that_is_not_a_whole_number_refuses(home, name, value):
    with pytest.raises(SubagentAdmissionRefusedError, match=name):
        await _admit({name: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["soon", "-5", "inf", "nan"])
async def test_a_wait_that_is_not_a_number_of_seconds_refuses(home, value):
    with pytest.raises(SubagentAdmissionRefusedError, match="ADMIT_WAIT_SECONDS"):
        await _admit({"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": value})


@pytest.mark.asyncio
async def test_an_unknown_workload_refuses(home):
    with pytest.raises(SubagentAdmissionRefusedError, match="workload 'medium'"):
        await _admit({}, workload="medium")


@pytest.mark.asyncio
async def test_status_names_each_pools_holders(home):
    env = {"LIONAGI_MAX_SUBAGENTS": "5", "LIONAGI_MAX_SUBAGENTS_HEAVY": "2"}
    lease = await _admit(env)
    lease.record_child(4242, None)
    try:
        status = subagent_admission_status(env)
    finally:
        lease.release()
    assert status["caps"] == {"LIONAGI_MAX_SUBAGENTS": 5, "LIONAGI_MAX_SUBAGENTS_HEAVY": 2}
    for pool in ("all", "heavy"):
        assert status["pools"][pool]["held"] == 1
        [holder] = status["pools"][pool]["holders"]
        assert holder["holder_pid"] == os.getpid()
        assert holder["child_pid"] == 4242
        assert holder["workload"] == "heavy"
    after = subagent_admission_status(env)
    assert after["pools"]["all"]["held"] == 0


@pytest.mark.asyncio
async def test_without_flock_an_uncapped_agent_runs_and_a_capped_one_is_refused(home, monkeypatch):
    monkeypatch.setattr(admission, "_fcntl", None)
    assert (await _admit({})).slots == []
    with pytest.raises(SubagentAdmissionRefusedError, match="no flock"):
        await _admit({"LIONAGI_MAX_SUBAGENTS": "2"})
    assert "error" not in subagent_admission_status({})
    assert subagent_admission_status({"LIONAGI_MAX_SUBAGENTS": "2"})["error"]


def test_the_refusal_is_recorded_on_the_run_by_its_class():
    assert "SubagentAdmissionRefusedError" in allowed_cause_classes()
    assert SubagentAdmissionRefusedError.retryable is True


def _lock_outside_admission(path: Path) -> int:
    """Hold *path* the way a holder between clearing its record and unlocking does:
    locked, through an fd of its own, with nothing written."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_slot_locked_outside_the_admission_lock_is_not_taken(home):
    pool_dir = _pools(home) / "all"
    fd = _lock_outside_admission(pool_dir / "0.lock")
    try:
        slot = admission._take_slot("all", pool_dir, {"0.lock"}, {"holder_pid": os.getpid()})
    finally:
        os.close(fd)
    assert slot is None
    assert (pool_dir / "0.lock").read_bytes() == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("rereads", [admission._CONTENTION_RETRIES, 0])
async def test_a_pool_full_only_of_a_clearing_holder_is_read_again_before_any_wait(
    home, monkeypatch, rereads
):
    """Wait 0: the admission gets no poll. The only slot is locked with no record, as
    a holder's is between clearing and unlocking; it unlocks after the first read. The
    quick re-reads admit; with none the same pool is refused."""
    monkeypatch.setattr(admission, "_CONTENTION_RETRIES", rereads)
    fd = _lock_outside_admission(_pools(home) / "all" / "0.lock")
    held = [fd]
    reads = []
    real_read_pool = admission._read_pool

    def _read_then_unlock(pool_dir):
        result = real_read_pool(pool_dir)
        reads.append(result[2])
        if held:
            os.close(held.pop())
        return result

    monkeypatch.setattr(admission, "_read_pool", _read_then_unlock)
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    try:
        if rereads:
            lease = await _admit(env)
            assert len(lease.slots) == 1
            lease.release()
            assert reads == [1, 0]
        else:
            with pytest.raises(SubagentAdmissionRefusedError, match="waited 0s"):
                await _admit(env)
            assert reads == [1]
    finally:
        for leftover in held:
            os.close(leftover)


@pytest.mark.asyncio
async def test_a_recorded_holder_is_not_read_again(home, monkeypatch):
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    first = await _admit(env)
    reads = []
    real_read_pool = admission._read_pool

    def _counted(pool_dir):
        reads.append(pool_dir.name)
        return real_read_pool(pool_dir)

    monkeypatch.setattr(admission, "_read_pool", _counted)
    try:
        with pytest.raises(SubagentAdmissionRefusedError):
            await _admit(env)
    finally:
        first.release()
    assert reads == ["all"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "reads_expected"), [(1, 1), (2, 1 + admission._CONTENTION_RETRIES)]
)
async def test_a_clearing_holder_is_read_again_only_while_the_recorded_holders_leave_room(
    home, monkeypatch, cap, reads_expected
):
    """Wait 0. One holder is recorded and one is between clearing and unlocking. At a cap
    of 1 the recorded holder alone fills the pool, so the clearing one freeing its slot
    admits nothing and the pool is not read again; at a cap of 2 it would admit."""
    env = {"LIONAGI_MAX_SUBAGENTS": str(cap), "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    first = await _admit(env)
    fd = _lock_outside_admission(_pools(home) / "all" / "1.lock")
    reads = []
    real_read_pool = admission._read_pool

    def _counted(pool_dir):
        result = real_read_pool(pool_dir)
        reads.append((len(result[0]), result[2]))
        return result

    monkeypatch.setattr(admission, "_read_pool", _counted)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="waited 0s"):
            await _admit(env)
    finally:
        os.close(fd)
        first.release()
    assert reads == [(2, 1)] * reads_expected


@pytest.mark.asyncio
async def test_each_wait_gives_a_clearing_holder_met_after_it_its_own_quick_rereads(
    home, monkeypatch
):
    """A clearing holder that stays locked uses up the quick re-reads, then the
    admission waits. The holder it meets after that wait is read again as many times,
    and the second wait frees it."""
    monkeypatch.setattr(admission, "_POLL_SECONDS", 1.0)
    env = {"LIONAGI_MAX_SUBAGENTS": "2", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "60"}
    first = await _admit(env)
    held = [_lock_outside_admission(_pools(home) / "all" / "1.lock")]
    reads = []
    waits = []
    real_read_pool = admission._read_pool

    def _counted(pool_dir):
        reads.append(pool_dir.name)
        return real_read_pool(pool_dir)

    async def _sleep(seconds):
        # Only the wait between polls is this long; a quick re-read sleeps milliseconds.
        if seconds >= 0.5:
            waits.append(len(reads))
            if len(waits) == 2:
                os.close(held.pop())
        await anyio.sleep(0)

    monkeypatch.setattr(admission, "_read_pool", _counted)
    monkeypatch.setattr(admission, "_sleep", _sleep)
    try:
        lease = await _admit(env)
    finally:
        for leftover in held:
            os.close(leftover)
        first.release()
    assert len(lease.slots) == 1
    lease.release()
    per_wait = 1 + admission._CONTENTION_RETRIES
    assert waits == [per_wait, 2 * per_wait]
    assert len(reads) == 2 * per_wait + 1


@pytest.mark.asyncio
async def test_a_wait_spent_on_a_clearing_holder_ends_without_quick_rereads_past_it(
    home, monkeypatch
):
    """A clearing holder that never clears is read again quickly within each wait, but
    the sleep that reaches the deadline leads to one last read and the refusal, with no
    quick re-reads after the wait is spent."""
    monkeypatch.setattr(admission, "_POLL_SECONDS", 1.0)
    env = {"LIONAGI_MAX_SUBAGENTS": "2", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "3"}
    first = await _admit(env)
    held = _lock_outside_admission(_pools(home) / "all" / "1.lock")
    reads = []
    waits = []
    now = [1000.0]
    real_read_pool = admission._read_pool

    def _counted(pool_dir):
        reads.append(pool_dir.name)
        return real_read_pool(pool_dir)

    async def _sleep(seconds):
        if seconds >= 0.5:
            waits.append(len(reads))
        # Rounded so the sleep that runs to the deadline lands on it exactly.
        now[0] = round(now[0] + seconds, 9)
        await anyio.sleep(0)

    monkeypatch.setattr(admission, "_read_pool", _counted)
    monkeypatch.setattr(admission, "_sleep", _sleep)
    monkeypatch.setattr(admission, "random", SimpleNamespace(uniform=lambda low, high: low))
    monkeypatch.setattr(
        admission,
        "time",
        SimpleNamespace(monotonic=lambda: now[0], time=time.time, sleep=time.sleep),
    )
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="waited 3s"):
            await _admit(env)
    finally:
        os.close(held)
        first.release()
    assert now[0] == 1003.0
    assert len(waits) >= 2
    per_wait = 1 + admission._CONTENTION_RETRIES
    assert waits[1] - waits[0] == per_wait
    assert len(reads) - waits[-1] == 1
