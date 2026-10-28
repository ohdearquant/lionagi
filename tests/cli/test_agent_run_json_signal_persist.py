# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A `li agent` run that bound signal persistence writes into run.json how many signals it
could not confirm it wrote, 0 included, and its teardown names a non-zero count in one log
line. The first time its writer is refused it also marks run.json, off the emit path."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import threading
import time

import pytest

from lionagi.cli import _runs
from lionagi.cli._runs import SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD, TERMINAL_WRITE_LOST_FIELD
from tests.cli.test_agent_run_json_error import (
    _run,
    _snapshot_before_operate,
    _two_runs,
    _with_a_real_row,
)

BUSY_MS = 200
SIGNALS = 5


def _observer_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.session.observer" and record.levelno == logging.WARNING
    ]


@pytest.mark.asyncio
async def test_a_run_under_a_held_writer_writes_its_dropped_signals_into_run_json(
    monkeypatch, tmp_path, caplog
):
    """The writer is held by another connection while the run emits signals, then released
    before the real teardown runs."""
    from lionagi.cli.agent import _run_agent
    from lionagi.session.signal import NodeStarted
    from lionagi.state import engine as state_engine

    monkeypatch.setattr(state_engine, "SQLITE_BUSY_TIMEOUT_MS", BUSY_MS)
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    seen = {}

    async def teardown(ctx, **kw):
        observer = seen["observer"] = ctx["session"].observer
        blocker = sqlite3.connect(str(tmp_path / "state.db"), timeout=0, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            for i in range(SIGNALS):
                await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
        finally:
            blocker.rollback()
            blocker.close()
        return await real_teardown(ctx, **kw)

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING):
        *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "completed"
    assert "signal_persist_dropped" in manifest, manifest
    dropped = manifest["signal_persist_dropped"]
    assert dropped == seen["observer"].persist_dropped
    assert dropped >= SIGNALS
    teardown_lines = [m for m in _observer_warnings(caplog) if " signal(s) " in m]
    assert len(teardown_lines) == 1, teardown_lines
    assert f"could not confirm it wrote {dropped} signal(s)" in teardown_lines[0]


@pytest.mark.asyncio
async def test_a_clean_run_writes_zero_dropped_signals_into_run_json(monkeypatch, tmp_path, caplog):
    from lionagi.cli.agent import _run_agent

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    run = _run(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING):
        await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest.get("signal_persist_dropped") == 0, manifest
    assert _observer_warnings(caplog) == []
    # a clean run marks nothing: no window opened and its terminal write landed
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in manifest
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


@pytest.mark.asyncio
async def test_a_run_keeps_the_time_another_invocation_sharing_its_run_id_recorded(
    monkeypatch, tmp_path
):
    """Another invocation sharing the run id had recorded a degradation time on run.json; this
    run's own whole-file writes, from its setup write to its terminal one, keep it."""
    from lionagi.cli.agent import _run_agent

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    run = _run(monkeypatch, tmp_path)
    run.write_manifest({"status": "running", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 1000.0})

    await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 1000.0


# The marker a run leaves the first time its writer is refused (ADR-0104 Amendment 3).


async def _until(predicate, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def _manifest(run) -> dict:
    return json.loads(run.manifest_path.read_text())


def _operate_with_the_writer_held(monkeypatch, tmp_path, body):
    """Replace the agent's operate: a second connection holds the store's writer while
    ``body(observer, blocker)`` runs, so the signals it emits find the writer refused. The real
    setup and teardown run around it."""
    from lionagi import Branch
    from lionagi.cli import agent as agent_mod
    from lionagi.state import engine as state_engine

    monkeypatch.setattr(state_engine, "SQLITE_BUSY_TIMEOUT_MS", BUSY_MS)
    real_setup = agent_mod.setup_agent_persist
    seen: dict = {}

    async def setup(*args, **kwargs):
        seen["ctx"] = await real_setup(*args, **kwargs)
        return seen["ctx"]

    async def operate(self, instruction=None, **kw):
        blocker = sqlite3.connect(str(tmp_path / "state.db"), timeout=0, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            await body(seen["ctx"]["session"].observer, blocker)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
        return "done"

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(Branch, "operate", operate)


@pytest.mark.asyncio
async def test_the_marker_is_on_disk_while_the_run_goes_on_with_the_writer_refused(
    monkeypatch, tmp_path
):
    """The writer is held and the run has not reached teardown: run.json already says so."""
    from lionagi.cli.agent import _run_agent
    from lionagi.session.signal import NodeStarted

    _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)
    during: dict = {}

    async def body(observer, blocker):
        for i in range(SIGNALS):
            await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
        await _until(lambda: SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(run), "the marker")
        during.update(_manifest(run))

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)
    before = time.time()

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    assert during["status"] == "running"
    marker = during[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD]
    assert isinstance(marker, float) and before <= marker <= time.time()
    final = _manifest(run)
    assert terminal_status == final["status"] == "completed"
    assert final[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == marker  # the terminal write carries it
    assert TERMINAL_WRITE_LOST_FIELD not in final


@pytest.mark.asyncio
async def test_emits_do_not_wait_on_the_run_directory_lock_and_the_marker_lands_when_it_is_freed(
    monkeypatch, tmp_path
):
    """Another descriptor holds the run directory's manifest lock for HOLD_S seconds. The emits
    return at once, the marker is not on disk while the lock is held, and lands after it."""
    from lionagi.cli.agent import _run_agent
    from lionagi.session.signal import NodeStarted

    hold_s = 1.5
    _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)
    measured: dict = {}

    async def body(observer, blocker):
        lock_fd = os.open(run.state_root / ".run.json.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        def release() -> None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        timer = threading.Timer(hold_s, release)
        started = time.monotonic()
        timer.start()
        try:
            for i in range(SIGNALS):
                await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
            measured["emitted_in"] = time.monotonic() - started
            measured["on_disk_while_held"] = SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(run)
            await _until(
                lambda: SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(run), "the marker"
            )
            measured["landed_after"] = time.monotonic() - started
        finally:
            timer.join()

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)

    await _run_agent("claude/sonnet", "go")

    assert measured["emitted_in"] < hold_s / 2, measured
    assert measured["on_disk_while_held"] is False
    assert measured["landed_after"] >= hold_s, measured


@pytest.mark.asyncio
async def test_two_window_openings_write_one_marker(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent
    from lionagi.session import observer as observer_mod
    from lionagi.session.signal import NodeStarted

    _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(observer_mod, "_PERSIST_SKIP_WINDOW_SECONDS", 0.0)
    writes: list[float] = []
    real_write = _runs._write_signal_persistence_degraded_at

    def counting_write(state_root, at):
        writes.append(at)
        real_write(state_root, at)

    monkeypatch.setattr(_runs, "_write_signal_persistence_degraded_at", counting_write)
    first: list[float] = []

    async def body(observer, blocker):
        await observer.emit(NodeStarted(op_id="held", name="step"))  # the first opening
        await _until(lambda: SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(run), "the marker")
        first.append(_manifest(run)[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD])
        blocker.rollback()
        await observer.emit(NodeStarted(op_id="lands", name="step"))  # the window closes
        blocker.execute("BEGIN IMMEDIATE")
        await observer.emit(NodeStarted(op_id="held-again", name="step"))  # and opens again
        await asyncio.sleep(0.3)  # time for a second write, if one were made

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)

    await _run_agent("claude/sonnet", "go")

    assert len(first) == 1
    assert writes == first
    assert _manifest(run)[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == first[0]


@pytest.mark.asyncio
async def test_a_binding_without_a_run_manifest_marks_nothing(monkeypatch, tmp_path, caplog):
    """Only the `li agent` binding, which passes its manifest and run directory, is marked."""
    from lionagi import Branch
    from lionagi.cli._runs import RunDir
    from lionagi.session.signal import NodeStarted
    from lionagi.state import engine as state_engine

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(state_engine, "SQLITE_BUSY_TIMEOUT_MS", BUSY_MS)
    run = RunDir(run_id="run-g", state_root=tmp_path / "g", artifact_root=tmp_path / "g-art")
    run.ensure_state_dirs()
    run.write_manifest({"status": "running"})
    live = await _runs.setup_agent_persist(Branch(), run_id=run.run_id, run=run, share_db=False)
    assert live is not None
    blocker = sqlite3.connect(str(tmp_path / "state.db"), timeout=0, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.DEBUG, logger="lionagi.session.observer"):
            await live["session"].observer.emit(NodeStarted(op_id="held", name="step"))
            await asyncio.sleep(0.3)
    finally:
        blocker.rollback()
        blocker.close()
        await _runs.teardown_agent_persist(live, status="completed")

    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in run.read_manifest()
    # no callback was bound, so none ran and failed
    assert not any("window callback failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_window_callback_sets_the_time_once_and_hands_the_write_to_a_thread(
    monkeypatch, tmp_path
):
    handed: list[tuple[int, object, float]] = []

    def record(state_root, at):
        handed.append((threading.get_ident(), state_root, at))

    monkeypatch.setattr(_runs, "_write_signal_persistence_degraded_at", record)
    run_manifest = {"status": "running"}
    mark = _runs._signal_window_marker(run_manifest, tmp_path)

    mark()
    first = run_manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD]
    mark()
    await _until(lambda: handed, "the hand-off")
    await asyncio.sleep(0.1)

    assert isinstance(first, float)
    assert run_manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == first
    assert [(root, at) for _tid, root, at in handed] == [(tmp_path, first)]
    assert handed[0][0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_hand_off_that_fails_keeps_the_time_on_the_manifest_and_logs(
    monkeypatch, tmp_path, caplog
):
    def refuse(*_a, **_kw):
        raise RuntimeError("can't start new thread")

    run_manifest = {"status": "running"}
    mark = _runs._signal_window_marker(run_manifest, tmp_path)
    with caplog.at_level(logging.WARNING, logger="lionagi.cli._runs"):
        with monkeypatch.context() as patched:
            patched.setattr(_runs.threading, "Thread", refuse)
            mark()  # raises nothing

    assert isinstance(run_manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD], float)
    assert any("can't start new thread" in record.getMessage() for record in caplog.records)


def test_the_marker_write_keeps_an_earlier_time_and_the_rest_of_the_file(tmp_path):
    from lionagi.cli._runs import RunDir

    run = RunDir(run_id="run-k", state_root=tmp_path / "k", artifact_root=tmp_path / "k-art")
    run.ensure_state_dirs()
    run.write_manifest({"status": "running", "branch_id": "b-1"})

    _runs._write_signal_persistence_degraded_at(run.state_root, 100.0)
    _runs._write_signal_persistence_degraded_at(run.state_root, 200.0)

    manifest = run.read_manifest()
    assert manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 100.0
    assert (manifest["status"], manifest["branch_id"]) == ("running", "b-1")


@pytest.mark.parametrize("bound", [False, True], ids=["from-path", "provenance-bound"])
def test_a_whole_file_write_keeps_the_earliest_time_on_file(tmp_path, bound):
    """A manifest written without the time, or with a later one, keeps the time on file, as the
    whole-file write of another invocation sharing the run id must; an earlier one replaces it."""
    from lionagi.cli._runs import RunDir

    run = RunDir(
        run_id="run-s",
        state_root=tmp_path / "s",
        artifact_root=tmp_path / "s-art",
        cwd=tmp_path if bound else None,
    )
    run.ensure_state_dirs()
    run.write_manifest({"status": "running", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 1000.0})

    run.write_manifest({"status": "running"})
    assert run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 1000.0

    run.write_manifest({"status": "completed", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 2000.0})
    manifest = run.read_manifest()
    assert (manifest["status"], manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD]) == (
        "completed",
        1000.0,
    )

    run.write_manifest({"status": "completed", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 500.0})
    assert run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 500.0


@pytest.mark.parametrize(
    "on_file",
    ["yesterday", True, [1.0], None, float("nan"), float("inf"), 10**400],
    ids=["string", "bool", "list", "null", "nan", "infinity", "past-float-range"],
)
def test_a_malformed_time_on_file_is_not_kept(tmp_path, on_file):
    """A whole-file write does not carry a time no reader takes, and the marker thread writes
    over one."""
    from lionagi.cli._runs import RunDir

    run = RunDir(run_id="run-m", state_root=tmp_path / "m", artifact_root=tmp_path / "m-art")
    run.ensure_state_dirs()
    on_disk = {
        "run_id": "run-m",
        "status": "running",
        SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: on_file,
    }

    run.manifest_path.write_text(json.dumps(on_disk))
    run.write_manifest({"status": "running"})
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in run.read_manifest()

    run.manifest_path.write_text(json.dumps(on_disk))
    _runs._write_signal_persistence_degraded_at(run.state_root, 100.0)
    assert run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 100.0


def test_the_degradation_record_keeps_the_earliest_time_on_file(tmp_path):
    """Recording why persistence was disabled writes the whole file, and carries the time on file
    as every other whole-file write does: a later time does not replace it, an earlier one does."""
    from lionagi.cli._runs import PERSISTENCE_DEGRADED_REASON_FIELD, RunDir

    run = RunDir(run_id="run-d", state_root=tmp_path / "d", artifact_root=tmp_path / "d-art")
    run.ensure_state_dirs()
    run.write_manifest({"status": "running", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 1000.0})

    _runs._record_persistence_degraded(
        RuntimeError("store gone"),
        run=run,
        run_manifest={"status": "running", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 2000.0},
    )
    manifest = run.read_manifest()
    assert manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 1000.0
    assert "store gone" in manifest[PERSISTENCE_DEGRADED_REASON_FIELD]

    _runs._record_persistence_degraded(
        RuntimeError("store gone"),
        run=run,
        run_manifest={"status": "running", SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD: 500.0},
    )
    assert run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 500.0


def test_the_copy_back_waits_for_the_manifest_lock(tmp_path):
    """Copying a resumed leg's lost-write record onto the run's run.json takes the run directory's
    manifest lock: it does not write while another descriptor holds it, and writes once freed."""
    from lionagi.cli._runs import RunDir

    run = RunDir(run_id="run-c", state_root=tmp_path / "c", artifact_root=tmp_path / "c-art")
    run.ensure_state_dirs()
    run.write_manifest({"status": "cancelled"})
    record = {"status": "completed", "reason_code": "completed_ok", "at": 1.0}
    lock_fd = os.open(run.state_root / ".run.json.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    done = threading.Event()

    def copy_back():
        _runs._write_terminal_write_lost(run.state_root, record)
        done.set()

    started = threading.Event()

    def copy_back_after_start():
        started.set()
        copy_back()

    worker = threading.Thread(target=copy_back_after_start, daemon=True)
    worker.start()
    try:
        assert started.wait(10)
        assert not done.wait(1.0)
        assert TERMINAL_WRITE_LOST_FIELD not in run.read_manifest()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert done.wait(10)
    manifest = run.read_manifest()
    assert (manifest["status"], manifest[TERMINAL_WRITE_LOST_FIELD]) == ("cancelled", record)


def test_the_copy_back_writes_while_it_holds_the_manifest_lock(monkeypatch, tmp_path):
    """At the moment the copy-back writes run.json, the run directory's manifest lock is held."""
    from lionagi.cli._runs import RunDir

    run = RunDir(run_id="run-h", state_root=tmp_path / "h", artifact_root=tmp_path / "h-art")
    run.ensure_state_dirs()
    run.write_manifest({"status": "cancelled"})
    real_write = _runs._atomic_write_json
    held: list[bool] = []

    def write_and_probe(path, payload):
        probe = os.open(run.state_root / ".run.json.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(True)
        else:
            held.append(False)
        finally:
            os.close(probe)
        real_write(path, payload)

    monkeypatch.setattr(_runs, "_atomic_write_json", write_and_probe)
    record = {"status": "completed", "reason_code": "completed_ok", "at": 1.0}
    _runs._write_terminal_write_lost(run.state_root, record)

    assert held == [True]
    assert run.read_manifest()[TERMINAL_WRITE_LOST_FIELD] == record


@pytest.mark.parametrize("token", ["NaN", "Infinity"])
def test_a_time_on_file_that_is_not_one_is_left_out_by_both_partial_writers(tmp_path, token):
    """run.json holding a NaN or an infinity for the time does not stop the degradation record or
    the copy-back from writing; both leave the value out, as a whole-file write does."""
    from lionagi.cli._runs import PERSISTENCE_DEGRADED_REASON_FIELD, RunDir

    run = RunDir(run_id="run-n", state_root=tmp_path / "n", artifact_root=tmp_path / "n-art")
    run.ensure_state_dirs()
    bad = f'{{"status": "running", "{SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD}": {token}}}'
    run.manifest_path.write_text(bad)
    _runs._record_persistence_degraded(
        RuntimeError("store gone"), run=run, run_manifest={"status": "running"}
    )
    manifest = run.read_manifest()
    assert "store gone" in manifest[PERSISTENCE_DEGRADED_REASON_FIELD]
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in manifest

    run.manifest_path.write_text(bad)
    record = {"status": "completed", "reason_code": "completed_ok", "at": 1.0}
    _runs._write_terminal_write_lost(run.state_root, record)
    manifest = run.read_manifest()
    assert manifest[TERMINAL_WRITE_LOST_FIELD] == record
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in manifest


def test_an_integer_time_on_file_is_kept_by_every_writer(tmp_path):
    """JSON holds a whole-second time as an integer, which readers take as a time, so the
    degradation record, the copy-back, the marker write and a whole-file write all keep it."""
    from lionagi.cli._runs import RunDir

    run = RunDir(run_id="run-i", state_root=tmp_path / "i", artifact_root=tmp_path / "i-art")
    run.ensure_state_dirs()
    on_file = f'{{"status": "running", "{SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD}": 100}}'
    record = {"status": "completed", "reason_code": "completed_ok", "at": 1.0}
    writers = [
        lambda: _runs._record_persistence_degraded(
            RuntimeError("store gone"), run=run, run_manifest={"status": "running"}
        ),
        lambda: _runs._write_terminal_write_lost(run.state_root, record),
        lambda: _runs._write_signal_persistence_degraded_at(run.state_root, 200.0),
        lambda: run.write_manifest({"status": "running"}),
    ]

    for write in writers:
        run.manifest_path.write_text(on_file)
        write()
        # 100 == 100.0, so the type says whether the integer itself was kept.
        assert type(run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD]) is int
        assert run.read_manifest()[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == 100


def test_a_provenance_bound_write_over_an_unreadable_file_still_writes(tmp_path):
    """A run bound to its provenance reads the file only for the time, so a file it cannot read
    is written over as before, without one."""
    from lionagi.cli._runs import RunDir

    run = RunDir(
        run_id="run-u", state_root=tmp_path / "u", artifact_root=tmp_path / "u-art", cwd=tmp_path
    )
    run.ensure_state_dirs()
    run.manifest_path.write_bytes(b"\xff\xfe not json")

    run.write_manifest({"status": "running"})

    manifest = run.read_manifest()
    assert (manifest["status"], manifest["cwd"]) == ("running", str(tmp_path))
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in manifest


@pytest.mark.parametrize("content", [None, b"not json", b"[1, 2]"], ids=["absent", "text", "list"])
def test_a_marker_write_that_fails_logs_and_changes_nothing(tmp_path, caplog, content):
    state_root = tmp_path / "f"
    state_root.mkdir()
    if content is not None:
        (state_root / "run.json").write_bytes(content)

    with caplog.at_level(logging.WARNING, logger="lionagi.cli._runs"):
        _runs._write_signal_persistence_degraded_at(state_root, 100.0)  # raises nothing

    assert any("signal persistence degradation" in r.getMessage() for r in caplog.records)
    if content is None:
        assert not (state_root / "run.json").exists()
    else:
        assert (state_root / "run.json").read_bytes() == content


@pytest.mark.asyncio
async def test_a_resumed_legs_first_skip_window_marks_the_run_json_the_job_reads(
    monkeypatch, tmp_path
):
    """The run timed out and resumed itself, and only the resumed leg, in a run directory of its
    own, found the writer refused. The run.json of the run it resumed for, the one the job reads,
    is marked while the resumed leg still runs, and keeps the mark to the end beside the status
    that run wrote."""
    from lionagi.cli.agent import _run_agent
    from lionagi.session.signal import NodeStarted

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    job, resumed = _two_runs(monkeypatch, tmp_path)
    legs: list = []
    during: dict = {}

    async def body(observer, blocker):
        legs.append(observer)
        if len(legs) == 1:
            raise TimeoutError("deadline")
        for i in range(SIGNALS):
            await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
        await _until(
            lambda: SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(job), "the job's mark"
        )
        during.update(_manifest(job))

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)
    _snapshot_before_operate(monkeypatch)
    before = time.time()

    *_, terminal_status, _session_id = await _run_agent(
        "claude/sonnet", "go", resume_on_timeout=True
    )

    assert len(legs) == 2
    marker = during[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD]
    assert isinstance(marker, float) and before <= marker <= time.time()
    final = _manifest(job)
    assert terminal_status == _manifest(resumed)["status"] == "completed"
    assert during["status"] == final["status"] == "timed_out"
    assert final[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == marker
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD in _manifest(resumed)


@pytest.mark.asyncio
async def test_a_resumed_run_whose_legs_were_never_refused_writes_no_time(monkeypatch, tmp_path):
    """A run that resumed itself, with no skip window in either leg, has no time to write again
    when its leg ends, and neither run.json gets the field, not even as null."""
    from lionagi.cli.agent import _run_agent

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    job, resumed = _two_runs(monkeypatch, tmp_path)
    legs: list = []

    async def body(observer, blocker):
        legs.append(observer)
        if len(legs) == 1:
            raise TimeoutError("deadline")

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)
    _snapshot_before_operate(monkeypatch)

    *_, terminal_status, _session_id = await _run_agent(
        "claude/sonnet", "go", resume_on_timeout=True
    )

    assert len(legs) == 2
    assert terminal_status == _manifest(resumed)["status"] == "completed"
    assert _manifest(job)["status"] == "timed_out"
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in _manifest(job)
    assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in _manifest(resumed)


@pytest.mark.asyncio
async def test_a_resumed_run_writes_the_time_again_when_its_leg_ends(monkeypatch, tmp_path):
    """The marker's thread wrote nothing (it failed, or the process left before it ran), and the
    run that resumed itself writes no manifest after its leg, so it writes the time again then."""
    from lionagi.cli.agent import _run_agent
    from lionagi.session.signal import NodeStarted

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    job, resumed = _two_runs(monkeypatch, tmp_path)
    legs: list = []
    handed: list = []
    monkeypatch.setattr(
        _runs, "_write_signal_persistence_degraded_at", lambda root, at: handed.append((root, at))
    )

    async def body(observer, blocker):
        legs.append(observer)
        if len(legs) == 1:
            raise TimeoutError("deadline")
        for i in range(SIGNALS):
            await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
        await _until(lambda: any(root == job.state_root for root, _ in handed), "the hand-off")
        assert SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD not in _manifest(job)

    _operate_with_the_writer_held(monkeypatch, tmp_path, body)
    _snapshot_before_operate(monkeypatch)

    await _run_agent("claude/sonnet", "go", resume_on_timeout=True)

    assert len(legs) == 2
    marker = next(at for root, at in handed if root == job.state_root)
    final = _manifest(job)
    assert final["status"] == "timed_out"
    assert final[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] == marker
