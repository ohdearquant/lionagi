# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A run whose persistence setup failed is still stoppable by `li kill`.

Such a run's setup returns no session, so its run id names no row the resolver searches. It records
its own process on its run.json instead, says how to stop it, and `li kill <run_id>` stops it by
the same identity checks a session's pid gets (ADR-0104 Amendment 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from lionagi.cli._kill_service import _settle_degraded_run
from lionagi.cli._runs import (
    PERSISTENCE_DEGRADED_REASON_FIELD,
    _record_persistence_degraded,
    _run_manifest_lock,
    allocate_run,
)
from lionagi.cli.kill import _do_kill, _do_kill_all_stale
from lionagi.ln._proc import raw_create_time

RUN_ID = "20261005T030000-abc123"


@pytest.fixture(autouse=True)
def stable_boot_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-scoped process identity checks deterministic."""
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_700_000_000.0)


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", root)
    return root


@pytest.fixture
def leg(tmp_path: Path):
    """A process `li kill` identifies as a lionagi CLI: a script named `li`, run by its shebang."""
    script = tmp_path / "bin" / "li"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(300)\n")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)])  # noqa: S603
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # the shebang has to have replaced the image first
        try:
            if os.path.basename(psutil.Process(proc.pid).cmdline()[-1]) == "li":
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied, IndexError):
            pass
        time.sleep(0.05)
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _markers(pid: int) -> dict:
    return {
        "pid": pid,
        "pid_create_time": raw_create_time(psutil.Process(pid)),
        "pid_host": socket.gethostname(),
        "pid_boot_time": psutil.boot_time(),
        "process_identity_mode": "local",
    }


def _write_run(root: Path, manifest: dict, run_id: str = RUN_ID) -> Path:
    path = root / run_id / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": run_id, **manifest}))
    return path


def _degraded(pid: int, **extra) -> dict:
    return {
        "status": "running",
        PERSISTENCE_DEGRADED_REASON_FIELD: "OperationalError('database is locked')",
        **_markers(pid),
        **extra,
    }


def _assert_manifest_lock_is_held(state_root: Path, flock) -> None:
    import fcntl

    fd = os.open(state_root / ".run.json.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_a_degraded_run_is_stopped_by_its_run_id_and_marked_cancelled(runs_root, leg):
    path = _write_run(runs_root, _degraded(leg.pid))

    assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0

    assert leg.wait(timeout=10) is not None
    manifest = json.loads(path.read_text())
    # the stand-in writes nothing on SIGTERM, so the kill settles the manifest itself
    assert manifest["status"] == "cancelled"
    assert manifest["ended_at"] is not None


@pytest.mark.asyncio
async def test_a_run_that_wrote_its_own_end_keeps_it(runs_root, tmp_path):
    """The manifest is settled by the kill only when the run could not settle it."""
    script = tmp_path / "li"
    path = runs_root / RUN_ID / "run.json"
    # on SIGTERM the stand-in writes `failed` the way a run's own teardown would
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, signal, sys, time\n"
        f"p = {str(path)!r}\n"
        "def stop(*_):\n"
        "    m = json.load(open(p)); m['status'] = 'failed'; json.dump(m, open(p, 'w')); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open(p + '.ready', 'w').close()\n"
        "time.sleep(300)\n"
    )
    script.chmod(0o755)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": RUN_ID, "status": "running"}))
    proc = subprocess.Popen([str(script)])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        while not Path(str(path) + ".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        path.write_text(json.dumps({"run_id": RUN_ID, **_degraded(proc.pid)}))

        assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0
        proc.wait(timeout=10)
        assert json.loads(path.read_text())["status"] == "failed"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other_process",
    ["another pid", "the same pid, another start time", "another pid, the same start time"],
)
@pytest.mark.parametrize("reached", ["by its run id", "through an imported row"])
async def test_a_run_id_another_invocation_now_records_is_left_to_it(
    runs_root, tmp_path, leg, caplog, reached, other_process
):
    """Invocations sharing a run id share its run.json. One that records itself there while the
    kill stops the other keeps its record: the kill settles only the process it stopped."""
    script = tmp_path / "li"
    path = runs_root / RUN_ID / "run.json"
    # on SIGTERM the stand-in puts the other invocation's record in place, then exits
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        f"p = {str(path)!r}\n"
        "def stop(*_):\n"
        "    os.replace(p + '.next', p); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open(p + '.ready', 'w').close()\n"
        "time.sleep(300)\n"
    )
    script.chmod(0o755)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": RUN_ID, "status": "running"}))
    proc = subprocess.Popen([str(script)])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        while not Path(str(path) + ".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        path.write_text(json.dumps({"run_id": RUN_ID, **_degraded(proc.pid)}))
        other = {"run_id": RUN_ID, **_degraded(leg.pid)}
        if other_process == "the same pid, another start time":
            # Linux counts start times in clock ticks, so the leg started just before this process
            # can carry the very same one; offset it so the start time is another by construction.
            other["pid"] = proc.pid
            other["pid_create_time"] = _markers(proc.pid)["pid_create_time"] + 1.0
        elif other_process == "another pid, the same start time":
            other["pid_create_time"] = _markers(proc.pid)["pid_create_time"]
        Path(str(path) + ".next").write_text(json.dumps(other))
        if reached == "through an imported row":
            await _import_mid_run(path)

        with caplog.at_level(logging.WARNING):
            assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0
        proc.wait(timeout=10)

        assert json.loads(path.read_text()) == other
        assert leg.poll() is None
        assert f"run.json now records another process (pid {other['pid']})" in caplog.text
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_record_written_while_the_kill_settles_waits_for_it_and_is_kept(
    runs_root, leg, monkeypatch
):
    """The kill reads run.json again and writes under the run's manifest lock, which every run.json
    writer takes. Another invocation sharing the run id that records itself after that read waits
    for the kill's write, and its record is the one left."""
    import lionagi.cli._kill_service as kill_service
    import lionagi.cli.kill as kill_mod

    run = allocate_run(run_id=RUN_ID)
    stopped = {"run_id": RUN_ID, **_degraded(leg.pid)}
    run.manifest_path.write_text(json.dumps(stopped))
    other = {"status": "running", "pid": os.getpid(), "pid_create_time": 1.0}
    writer_started = threading.Event()
    writer_finished = threading.Event()
    lock_attempted = threading.Event()
    writer = None
    import fcntl

    actual_flock = fcntl.flock

    def observe_flock(fd, operation):
        if threading.current_thread() is writer:
            lock_attempted.set()
        return actual_flock(fd, operation)

    def write_next_manifest():
        writer_started.set()
        run.write_manifest(other)
        writer_finished.set()

    writer = threading.Thread(target=write_next_manifest)

    def read_then_record(text):
        current = json.loads(text)
        writer.start()  # the other invocation records itself after the kill's read
        assert writer_started.wait(timeout=10)
        assert lock_attempted.wait(timeout=10)
        _assert_manifest_lock_is_held(run.state_root, actual_flock)
        return current

    monkeypatch.setattr(fcntl, "flock", observe_flock)
    monkeypatch.setattr(kill_service, "json", SimpleNamespace(loads=read_then_record))
    _settle_degraded_run(run.manifest_path, stopped)
    writer.join(10)

    assert not writer.is_alive()
    assert writer_finished.is_set()
    final = json.loads(run.manifest_path.read_text())
    assert (final["status"], final["pid"]) == ("running", os.getpid())


@pytest.mark.parametrize("writer", ["write_manifest", "degradation record"])
def test_every_run_json_writer_waits_for_the_manifest_lock(runs_root, monkeypatch, writer):
    import fcntl

    run = allocate_run(run_id=RUN_ID)
    write = {
        "write_manifest": lambda: run.write_manifest({"status": "running", "pid": 1}),
        "degradation record": lambda: _record_persistence_degraded(RuntimeError("locked"), run=run),
    }[writer]
    before = run.manifest_path.read_text()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    lock_attempted = threading.Event()
    thread = None
    actual_flock = fcntl.flock

    def observe_flock(fd, operation):
        if threading.current_thread() is thread:
            lock_attempted.set()
        return actual_flock(fd, operation)

    def write_manifest():
        writer_started.set()
        write()
        writer_finished.set()

    thread = threading.Thread(target=write_manifest)
    with _run_manifest_lock(run.state_root):
        monkeypatch.setattr(fcntl, "flock", observe_flock)
        thread.start()
        assert writer_started.wait(timeout=10)
        assert lock_attempted.wait(timeout=10)
        _assert_manifest_lock_is_held(run.state_root, actual_flock)
        assert run.manifest_path.read_text() == before
    thread.join(10)
    assert not thread.is_alive()
    assert writer_finished.is_set()
    assert run.manifest_path.read_text() != before


def test_a_run_json_whose_lock_cannot_be_taken_is_left_as_it_is(runs_root, leg, caplog):
    path = _write_run(runs_root, _degraded(leg.pid))
    before = path.read_text()
    (path.parent / ".run.json.lock").mkdir()  # an open for writing fails on a directory

    with caplog.at_level(logging.WARNING):
        _settle_degraded_run(path, json.loads(before))

    assert path.read_text() == before
    assert "run.json lock could not be taken" in caplog.text


@pytest.mark.parametrize("writer", ["write_manifest", "degradation record"])
def test_a_writer_that_cannot_take_the_lock_still_writes(runs_root, writer, caplog):
    """A run's own writes do not depend on the lock: one that cannot be taken (here a directory in
    its place) is written without it, with a warning, as before the lock existed."""
    run = allocate_run(run_id=RUN_ID)
    lock = run.state_root / ".run.json.lock"
    lock.unlink()  # allocating the run wrote its placeholder, and so made the lock file
    lock.mkdir()
    write = {
        "write_manifest": lambda: run.write_manifest({"status": "running", "pid": 1}),
        "degradation record": lambda: _record_persistence_degraded(RuntimeError("locked"), run=run),
    }[writer]
    before = run.manifest_path.read_text()

    with caplog.at_level(logging.WARNING):
        write()

    assert run.manifest_path.read_text() != before
    assert "written without its lock" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", ["half written", "removed"])
@pytest.mark.parametrize("reached", ["by its run id", "through an imported row"])
async def test_a_run_json_the_kill_cannot_read_is_left_as_it_is(
    runs_root, tmp_path, caplog, reached, unreadable
):
    """What an unreadable run.json records cannot be checked against the stopped process (another
    invocation sharing the run id may be writing it), so the kill writes nothing over it."""
    script = tmp_path / "li"
    path = runs_root / RUN_ID / "run.json"
    # on SIGTERM the stand-in leaves the run.json half written, or removes it, then exits
    end = (
        'open(p, \'w\').write(\'{"run_id": "x", "sta\')'
        if unreadable == "half written"
        else "os.remove(p)"
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        f"p = {str(path)!r}\n"
        "def stop(*_):\n"
        f"    {end}; sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open(p + '.ready', 'w').close()\n"
        "time.sleep(300)\n"
    )
    script.chmod(0o755)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": RUN_ID, "status": "running"}))
    proc = subprocess.Popen([str(script)])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        while not Path(str(path) + ".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        path.write_text(json.dumps({"run_id": RUN_ID, **_degraded(proc.pid)}))
        if reached == "through an imported row":
            await _import_mid_run(path)

        with caplog.at_level(logging.WARNING):
            assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0
        proc.wait(timeout=10)

        if unreadable == "half written":
            assert path.read_text() == '{"run_id": "x", "sta'
        else:
            assert not path.exists()
        assert f"run {RUN_ID}: run.json could not be read" in caplog.text
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_a_kill_of_a_degraded_run_reports_the_run(runs_root, leg, capsys):
    _write_run(runs_root, _degraded(leg.pid))

    assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0

    assert f"killed run {RUN_ID} (signal=sigterm, pid={leg.pid})" in capsys.readouterr().out
    assert leg.wait(timeout=10) is not None


@pytest.mark.asyncio
async def test_a_run_that_kept_its_session_is_not_reached_by_its_run_id(runs_root, leg, caplog):
    """Only a degraded run resolves by run id; any other run is stopped through its session."""
    markers = _degraded(leg.pid)
    del markers[PERSISTENCE_DEGRADED_REASON_FIELD]
    _write_run(runs_root, markers)

    with caplog.at_level(logging.ERROR):
        assert await _do_kill(RUN_ID) == 1

    assert "entity not found" in caplog.text
    assert leg.poll() is None


@pytest.mark.asyncio
async def test_a_recycled_pid_is_not_signalled(runs_root, leg, caplog, monkeypatch):
    """The run's process is gone and another lionagi-looking process holds its pid."""
    monkeypatch.setattr(psutil, "LINUX", False)
    path = _write_run(
        runs_root, _degraded(leg.pid, pid_create_time=raw_create_time(psutil.Process(leg.pid)) - 60)
    )

    with caplog.at_level(logging.WARNING):
        assert await _do_kill(RUN_ID) == 1

    assert "did not match the expected lionagi process" in caplog.text
    assert leg.poll() is None
    assert json.loads(path.read_text())["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("linux", "expected_message"),
    [
        (False, "did not match the expected lionagi process"),
        (True, "could not be identified reliably"),
    ],
)
async def test_create_time_mismatch_refuses_degraded_kill_on_each_platform(
    runs_root, monkeypatch, caplog, linux, expected_message
):
    import lionagi.cli._kill_service as kill_service
    from lionagi.cli import kill as kill_mod

    monkeypatch.setattr(psutil, "LINUX", linux)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 100.0)
    patched_pid_alive = lambda _pid: True
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_is_zombie", lambda _pid: False)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    path = _write_run(
        runs_root,
        {
            "status": "running",
            PERSISTENCE_DEGRADED_REASON_FIELD: "OperationalError('database is locked')",
            "pid": 4242,
            "pid_create_time": 1.0,
            "process_identity_mode": "local",
        },
    )

    reasons = []
    verdict = kill_mod.check_pid_identity(
        4242, "lionagi", expected_create_time=1.0, _reason_out=reasons
    )
    assert verdict == ("unverifiable" if linux else "not_ours")
    assert reasons == (["start_mismatch"] if linux else [])
    with caplog.at_level(logging.WARNING):
        assert await _do_kill(RUN_ID, grace_seconds=0) == 1

    assert expected_message in caplog.text
    assert signals == []
    assert json.loads(path.read_text())["status"] == "running"


@pytest.mark.asyncio
async def test_a_degraded_run_permission_denial_is_not_marked_cancelled(
    runs_root, leg, monkeypatch
):
    path = _write_run(runs_root, _degraded(leg.pid))
    real_kill = os.kill

    def deny_signals(pid, sig):
        if sig == 0:
            return real_kill(pid, sig)
        raise PermissionError("signal denied")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "kill", deny_signals)
        assert await _do_kill(RUN_ID) == 1

    assert leg.poll() is None
    assert json.loads(path.read_text())["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [0, -1])
async def test_a_degraded_run_with_a_nonpositive_pid_is_left_unchanged(runs_root, leg, pid):
    manifest = {**_degraded(leg.pid), "pid": pid}
    path = _write_run(runs_root, manifest)

    assert await _do_kill(RUN_ID) == 1

    assert leg.poll() is None
    assert json.loads(path.read_text())["status"] == "running"


@pytest.fixture
def ended() -> dict:
    """The markers of a process that has exited and been reaped, so no process holds its pid."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    markers = _markers(proc.pid)
    proc.stdin.close()
    proc.wait(timeout=10)
    assert not psutil.pid_exists(proc.pid)
    return markers


@pytest.mark.asyncio
@pytest.mark.parametrize("process", ["running", "gone"])
@pytest.mark.parametrize("reached", ["by its run id", "through an imported row"])
async def test_a_degraded_run_with_no_start_time_is_not_stopped_and_nothing_is_written(
    runs_root, leg, ended, caplog, process, reached
):
    """The start time is such a run's only recorded identity. Without it the record does not
    show which process its pid named, so a pid nothing holds is no stop either."""
    manifest = {**_degraded(leg.pid), **(ended if process == "gone" else {})}
    del manifest["pid_create_time"]
    path = _write_run(runs_root, manifest)
    if reached == "through an imported row":
        await _import_mid_run(path)

    with caplog.at_level(logging.WARNING):
        assert await _do_kill(RUN_ID) == 1

    assert "was recorded with no start time" in caplog.text
    assert "kill skipped" in caplog.text
    assert leg.poll() is None
    assert json.loads(path.read_text()) == {"run_id": RUN_ID, **manifest}
    if reached == "through an imported row":
        assert await _row_status() == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("reached", ["by its run id", "through an imported row"])
async def test_a_degraded_run_whose_process_already_died_is_marked_cancelled(
    runs_root, ended, reached
):
    """Its recorded pid and start time name a process that no longer exists: the run is over,
    and it could not write its own end, so the kill writes it."""
    path = _write_run(
        runs_root,
        {"status": "running", PERSISTENCE_DEGRADED_REASON_FIELD: "locked", **ended},
    )
    if reached == "through an imported row":
        await _import_mid_run(path)

    assert await _do_kill(RUN_ID) == 0

    manifest = json.loads(path.read_text())
    assert manifest["status"] == "cancelled"
    assert manifest["ended_at"] is not None
    if reached == "through an imported row":
        assert await _row_status() == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest, refusal",
    [
        ({"status": "completed"}, "already in terminal state"),
        ({"pid": None}, "recorded no process to stop"),
    ],
)
async def test_a_degraded_run_refuses_what_it_cannot_stop(
    runs_root, leg, caplog, manifest, refusal
):
    _write_run(runs_root, {**_degraded(leg.pid), **manifest})

    with caplog.at_level(logging.ERROR):
        assert await _do_kill(RUN_ID) == 1

    assert refusal in caplog.text
    assert leg.poll() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["..", f"../runs/{RUN_ID}", f"{RUN_ID}/", "."])
async def test_an_id_that_is_not_one_path_component_is_no_run_id(runs_root, leg, caplog, given):
    _write_run(runs_root, _degraded(leg.pid))

    with caplog.at_level(logging.ERROR):
        assert await _do_kill(given) == 1

    assert "entity not found" in caplog.text
    assert leg.poll() is None


def _wire_agent(monkeypatch, tmp_path: Path):
    """`li agent` with its model and teardown stood in, its run directory and persistence setup real."""
    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli._runs import allocate_run
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_teardown(ctx, *, status="completed", **_):
        return status

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id=RUN_ID)
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return run


@pytest.mark.asyncio
async def test_a_run_whose_persistence_setup_fails_records_its_process_and_says_how_to_stop_it(
    monkeypatch, tmp_path, caplog
):
    import lionagi.cli._runs as runs_mod

    run = _wire_agent(monkeypatch, tmp_path)

    async def locked(*, register=True):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runs_mod, "_open_shared_db", locked)

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.WARNING):
        await _run_agent("codex/model", "do the thing")

    manifest = json.loads(run.manifest_path.read_text())
    assert "database is locked" in manifest[PERSISTENCE_DEGRADED_REASON_FIELD]
    assert manifest["pid"] == os.getpid()
    assert manifest["pid_create_time"] == pytest.approx(
        raw_create_time(psutil.Process(os.getpid()))
    )
    assert manifest["process_identity_mode"] == "local"
    assert (
        f"run {RUN_ID}: persistence setup returned no session; stop it with: li kill {RUN_ID}"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_a_run_that_keeps_its_session_records_no_process_on_its_manifest(
    monkeypatch, tmp_path, caplog
):
    run = _wire_agent(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.WARNING):
        await _run_agent("codex/model", "do the thing")

    manifest = json.loads(run.manifest_path.read_text())
    assert PERSISTENCE_DEGRADED_REASON_FIELD not in manifest
    assert "pid" not in manifest
    assert "li kill" not in caplog.text


async def _import_mid_run(path: Path) -> None:
    """What `li state import` does to a run it finds still running: a row named by the run id,
    with no pid on it."""
    from lionagi.cli.state import _import_one_run
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await _import_one_run(db, RUN_ID, path.parent, json.loads(path.read_text()))
        row = await db.get_session(RUN_ID)
    assert row["status"] == "running"
    assert not (row.get("node_metadata") or {}).get("pid")


async def _row_status() -> str:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return (await db.get_session(RUN_ID))["status"]


@pytest.mark.asyncio
async def test_a_degraded_run_imported_mid_run_is_stopped_through_its_row(runs_root, leg):
    """The imported row resolves first and carries no pid: the kill stops the process the run
    recorded on its run.json, then cancels the row and the run."""
    path = _write_run(runs_root, _degraded(leg.pid))
    await _import_mid_run(path)

    assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0

    assert leg.wait(timeout=10) is not None
    assert await _row_status() == "cancelled"
    assert json.loads(path.read_text())["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stale_sweep_preserves_a_live_imported_degraded_run(runs_root, leg):
    path = _write_run(runs_root, _degraded(leg.pid))
    await _import_mid_run(path)

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 7200, RUN_ID),
        )

    assert await _do_kill_all_stale(threshold_seconds=3600) == 0
    assert await _do_kill(RUN_ID, grace_seconds=5.0) == 0

    assert leg.wait(timeout=10) is not None
    assert await _row_status() == "cancelled"
    assert json.loads(path.read_text())["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["no pid", "recycled pid"])
async def test_an_imported_degraded_run_the_kill_cannot_identify_is_left_as_it_was(
    runs_root, leg, caplog, change
):
    """Not stopped, so nothing is written: the row and the run both still say running."""
    manifest = _degraded(leg.pid)
    if change == "no pid":
        del manifest["pid"]
    else:
        manifest["pid_create_time"] -= 60
    path = _write_run(runs_root, manifest)
    await _import_mid_run(path)

    with caplog.at_level(logging.WARNING):
        assert await _do_kill(RUN_ID) == 1

    assert "kill skipped" in caplog.text
    assert leg.poll() is None
    assert await _row_status() == "running"
    assert json.loads(path.read_text())["status"] == "running"


def _kept_its_session(pid: int) -> dict:
    manifest = _degraded(pid)
    del manifest[PERSISTENCE_DEGRADED_REASON_FIELD]
    return manifest


@pytest.mark.asyncio
async def test_an_imported_run_that_kept_its_session_is_refused_and_left_as_it_was(
    runs_root, leg, caplog
):
    """`li state import` gives every run.json it reads a row named by its run id, with no pid.
    For a run that kept its own session that row holds nothing a kill can stop: refused with
    nothing written, rather than cancelled while the run goes on."""
    path = _write_run(runs_root, _kept_its_session(leg.pid))
    await _import_mid_run(path)

    with caplog.at_level(logging.WARNING):
        assert await _do_kill(RUN_ID) == 1

    assert "is a row `li state import` made from a run.json" in caplog.text
    assert "kill skipped" in caplog.text
    assert leg.poll() is None
    assert await _row_status() == "running"
    assert json.loads(path.read_text())["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link", ["directory outside the root", "directory inside the root", "run.json"]
)
async def test_a_run_reached_through_a_link_is_no_run_id(runs_root, tmp_path, leg, caplog, link):
    """The run.json read, and later written, is the one in the directory the id names."""
    if link == "run.json":
        real = _write_run(tmp_path / "elsewhere", _degraded(leg.pid))
        (runs_root / RUN_ID).mkdir(parents=True)
        (runs_root / RUN_ID / "run.json").symlink_to(real)
    else:
        home = runs_root if link.endswith("inside the root") else tmp_path / "elsewhere"
        real = _write_run(home, _degraded(leg.pid), run_id="the-run-it-points-at")
        runs_root.mkdir(exist_ok=True)
        (runs_root / RUN_ID).symlink_to(real.parent)

    with caplog.at_level(logging.ERROR):
        assert await _do_kill(RUN_ID) == 1

    assert "entity not found" in caplog.text
    assert leg.poll() is None
    assert json.loads(real.read_text())["status"] == "running"


@pytest.mark.asyncio
async def test_an_orchestration_that_degrades_says_it_has_no_kill_handle(
    runs_root, monkeypatch, caplog
):
    from lionagi import Session
    from lionagi.cli._runs import allocate_run
    from lionagi.cli.orchestrate import _orchestration

    async def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(_orchestration, "_open_shared_db", locked)
    run = allocate_run(run_id=RUN_ID)

    with caplog.at_level(logging.WARNING):
        ctx = await _orchestration.setup_orchestration_persist(Session(), run=run, run_manifest={})

    assert ctx is None
    assert (
        f"run {RUN_ID}: persistence setup returned no session, and its run id is no `li kill` handle; "
        f"its process is pid "
        f"{os.getpid()}" in caplog.text
    )
