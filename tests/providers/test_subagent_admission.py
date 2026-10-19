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

import anyio
import pytest

from lionagi.ln._proc import process_create_time
from lionagi.mcp._terminal_cause import allowed_cause_classes
from lionagi.providers import _subagent_admission as admission
from lionagi.providers._subagent_admission import (
    SubagentAdmissionRefusedError,
    admit_subagent,
    subagent_admission_status,
)


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


_HOLDER = textwrap.dedent(
    """
    import sys, time, anyio
    from lionagi.providers._subagent_admission import admit_subagent

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
async def test_a_slot_locked_for_an_instant_by_a_status_read_is_not_a_full_pool(home):
    """``li monitor`` counts a pool by locking each free slot for a moment. An
    admission that lands in that moment, with no wait allowed, still admits."""
    import fcntl

    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    pool = _pools(home) / "all"
    pool.mkdir(parents=True)
    fd = os.open(pool / "0.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def status_read_ends():
        await anyio.sleep(0.1)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    async with anyio.create_task_group() as tg:
        tg.start_soon(status_read_ends)
        lease = await _admit(env)
    assert [slot.pool for slot in lease.slots] == ["all"]
    lease.release()


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
