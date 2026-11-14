# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run writer locking and degraded-record settlement."

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionhub.orchestration.kill import _settle_degraded_run
from lionhub.orchestration.runs import (
    _record_persistence_degraded,
    _run_manifest_lock,
    allocate_run,
)
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._kill_degraded_run_test_support import RUN_ID as RUN_ID
from tests._kill_degraded_run_test_support import _degraded as _degraded
from tests._kill_degraded_run_test_support import _markers as _markers
from tests._kill_degraded_run_test_support import _write_run as _write_run
from tests._kill_degraded_run_test_support import leg as leg
from tests._kill_degraded_run_test_support import runs_root as runs_root
from tests._kill_degraded_scope_test_support import stable_boot_time as stable_boot_time
from tests._kill_scope_test_support import (
    _guard_runner_probes_in_kill_tests as _guard_runner_probes_in_kill_tests,
)


def _assert_manifest_lock_is_held(state_root: Path, flock) -> None:
    import fcntl

    fd = os.open(state_root / ".run.json.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_a_record_written_while_the_kill_settles_waits_for_it_and_is_kept(
    runs_root, leg, monkeypatch
):
    """The kill reads run.json again and writes under the run's manifest lock, which every run.json
    writer takes. Another invocation sharing the run id that records itself after that read waits
    for the kill's write, and its record is the one left."""
    import lionhub.orchestration.kill as kill_service
    import lionhub.serve.cli.kill as kill_mod

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
