# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Stopping a recorded worker does not rewrite a terminal session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest

from lionagi.cli.kill import _do_kill
from lionagi.ln._proc import raw_create_time, start_time_matches
from lionagi.ln.concurrency import sleep as _real_sleep
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

_LEADER = """
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

child = None

def stop_child():
    if child is not None:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)

def stop(*_):
    stop_child()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
try:
    Path(sys.argv[1]).write_text(json.dumps({'child': child.pid}))
    while True:
        time.sleep(0.01)
finally:
    stop_child()
"""


def _same_owned_process(pid: int, birth: float) -> bool:
    try:
        return start_time_matches(raw_create_time(psutil.Process(pid)), birth)
    except (psutil.Error, OSError):
        return False


async def _wait_for_file(path: Path, leader: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 5.0
    while not path.exists():
        assert leader.poll() is None, "the worker fixture exited before publishing its child"
        assert time.monotonic() < deadline, "the worker fixture did not become ready"
        await asyncio.sleep(0.01)
    return json.loads(path.read_text())


async def _reap_leader(leader: subprocess.Popen) -> int:
    deadline = time.monotonic() + 12.0
    while leader.poll() is None:
        assert time.monotonic() < deadline, "the fixture leader was not reaped within its bound"
        await asyncio.sleep(0.01)
    return leader.returncode


@pytest.mark.asyncio
async def test_terminal_session_stops_its_recorded_worker_group(tmp_path, monkeypatch, capsys):
    """The recorded group ends, its record settles, and the failed session stays failed."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(db_mod, "settings", SimpleNamespace(LIONAGI_STATE_DB_URL=None))
    ready = tmp_path / "worker.json"
    owned = {}
    reaped = None
    leader = subprocess.Popen(  # noqa: S603 — this fixture owns both processes
        [sys.executable, "-c", _LEADER, str(ready)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        owned[leader.pid] = raw_create_time(psutil.Process(leader.pid))
        reaped = asyncio.create_task(_reap_leader(leader))
        child_pid = int((await _wait_for_file(ready, leader))["child"])
        owned[child_pid] = raw_create_time(psutil.Process(child_pid))
        assert os.getpgid(leader.pid) == leader.pid
        assert os.getpgid(child_pid) == leader.pid
        # Limit population enumeration to this fixture's exact known children.
        # Per-pid birth, group and kernel absence probes stay real.
        monkeypatch.setattr(psutil, "pids", lambda: list(owned))
        sent = []
        real_killpg = os.killpg

        def record_signal(pgid, sig):
            if sig != 0:
                assert pgid == leader.pid, "a signal targeted a group outside this fixture"
                sent.append((pgid, sig))
            return real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", record_signal)

        session_id, generation, progression, spawn_id = (str(uuid4()) for _ in range(4))
        async with StateDB(path) as db:
            await db.create_progression(progression)
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": progression,
                    "status": "running",
                    "cli_worker_generation": generation,
                    "invocation_kind": "agent",
                    "started_at": time.time(),
                }
            )
            await db.register_cli_worker_spawn(
                {
                    "spawn_id": spawn_id,
                    "session_id": session_id,
                    "generation_id": generation,
                    "pid": leader.pid,
                    "pgid": leader.pid,
                    "pid_create_time": owned[leader.pid],
                    "pid_host": socket.gethostname(),
                    "pid_boot_time": psutil.boot_time(),
                    "process_identity_mode": "local",
                }
            )
            await db.update_status(
                "session",
                session_id,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="the runner failed before the worker ended",
            )
            before = await db.get_session(session_id)
            transitions = await db.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_id = ?", (session_id,)
            )
            assert len((await db.snapshot_cli_worker_spawns(session_id))["spawns"]) == 1

        result = await _do_kill(session_id, grace_seconds=1.0)
        assert result == 0, "a terminal session must stop its live recorded worker group"
        assert sent == [(leader.pid, signal.SIGTERM)]
        assert await asyncio.wait_for(asyncio.shield(reaped), timeout=3.0) == 0
        with pytest.raises(ProcessLookupError):
            os.killpg(leader.pid, 0)
        async with StateDB(path) as db:
            assert await db.get_session(session_id) == before
            assert (
                await db.fetch_all(
                    "SELECT * FROM status_transitions WHERE entity_id = ?", (session_id,)
                )
                == transitions
            )
            record = await db.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
            )
            assert record["confirmed_empty_at"] is not None
        assert (
            f"stopped worker for session {session_id[:12]} "
            f"(pid={leader.pid}, pgid={leader.pid}, signal=sigterm); terminal status remains 'failed'"
        ) in capsys.readouterr().out
    finally:
        # The Popen handle is owned even when the first identity read failed.
        # Its TERM handler also ends and reaps the child when no birth was read.
        try:
            if leader.poll() is None:
                leader.terminate()
            try:
                leader.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                leader.kill()
                leader.wait(timeout=3.0)
        finally:
            # Any fallback signal names only a fixture-owned, birth-pinned pid.
            for pid, birth in reversed(tuple(owned.items())):
                if _same_owned_process(pid, birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            if reaped is not None:
                if not reaped.done():
                    reaped.cancel()
                await asyncio.gather(reaped, return_exceptions=True)


class _CleanupDB:
    def __init__(self, status="failed", *, guards=(), outcome="applied"):
        from contextlib import asynccontextmanager

        self.session = {
            "id": "session-test",
            "status": status,
            "cli_worker_generation": "generation-a",
            "updated_at": 42.0,
            "node_metadata": {"pid": 76543},
        }
        self.records = [
            {
                "spawn_id": str(uuid4()),
                "pid": pid,
                "pgid": pid,
                "pid_create_time": 10.0,
                "pid_host": "host",
                "pid_boot_time": 1.0,
                "process_identity_mode": "local",
            }
            for pid in (76541, 76542)
        ]
        self.guards = iter(guards)
        self.outcome = outcome
        self.cancelled = []
        self.cancel_identities = []
        self.settled = []
        self.guard_calls = []
        self.in_signal_guard = False

        @asynccontextmanager
        async def guard(*args):
            self.guard_calls.append(args)
            verdict = next(self.guards, "unchanged")
            if isinstance(verdict, Exception):
                raise verdict
            self.in_signal_guard = True
            try:
                yield verdict
            finally:
                self.in_signal_guard = False

        self.guard_cli_worker_signal = guard

    async def snapshot_cli_worker_spawns(self, session_id):
        assert session_id == self.session["id"]
        return {"session": dict(self.session), "spawns": list(self.records)}

    async def cancel_session_if_worker_generation(self, session_id, generation, **kwargs):
        self.cancelled.append((session_id, generation))
        self.cancel_identities.append(kwargs["expected_runner_identity"])
        if self.outcome == "applied":
            self.session["status"] = "cancelled"
        return self.outcome

    async def get_session(self, session_id):
        assert session_id == self.session["id"]
        return dict(self.session)

    async def settle_cli_worker_spawn(self, spawn_id, **kwargs):
        self.settled.append(spawn_id)
        return True


def _stub_recorded_groups(monkeypatch, module):
    from lionagi.ln._proc import _RecordedGroupWitness

    sent = []
    monkeypatch.setattr(
        module,
        "_admit_recorded_worker_group",
        lambda identity, **kwargs: _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        ),
    )
    monkeypatch.setattr(module, "_recorded_worker_group_matches", lambda witness: "matches")
    monkeypatch.setattr(module, "_recorded_worker_group_state", lambda identity: "empty")
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    return sent


@pytest.mark.asyncio
async def test_guard_failure_stops_remaining_groups_and_all_settlement(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(guards=("unchanged", TimeoutError("guard deadline")))
    sent = _stub_recorded_groups(monkeypatch, module)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == []


@pytest.mark.asyncio
async def test_interruption_during_grace_propagates_without_kill_or_settlement(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)

    async def interrupted(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "sleep", interrupted)
    before = dict(db.session)
    with pytest.raises(asyncio.CancelledError):
        await module._try_recorded_worker_cleanup(db, db.session, user_reason="", grace_seconds=1)
    assert sent == [(76541, signal.SIGTERM), (76542, signal.SIGTERM)]
    assert db.settled == []
    assert db.session == before


@pytest.mark.asyncio
async def test_resume_race_cancellation_conflict_never_signals_workers(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: ("gone", None))
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": 76543}]
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_cancellation_captures_only_present_runner_markers(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    db.session["node_metadata"] = {
        "pid": 76543,
        "pid_create_time": None,
        "pid_host": "worker-host",
        "process_identity_mode": "local",
        "unrelated": "not part of the runner identity",
    }
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(module, "_unaddressable_pid_reason", lambda meta: None)
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: ("gone", None))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancel_identities == [
        {
            "pid": 76543,
            "pid_create_time": None,
            "pid_host": "worker-host",
            "process_identity_mode": "local",
        }
    ]
    assert "pid_boot_time" not in db.cancel_identities[0]
    assert "unrelated" not in db.cancel_identities[0]
    assert sent == []
    assert db.settled == []


@pytest.mark.parametrize("invalid_metadata", [None, [], "not-json"])
@pytest.mark.asyncio
async def test_unusable_snapshot_metadata_cannot_cancel_or_clean_workers(
    monkeypatch, invalid_metadata
):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = invalid_metadata
    sent = _stub_recorded_groups(monkeypatch, module)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_missing_runner_row_identity_cannot_cancel_away_recorded_workers(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {}
    db.session["artifacts_path"] = "/outside-the-test"
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []
    assert any(
        "worker group for session session-test could not be confirmed stopped" in msg
        for msg in warnings
    )


@pytest.mark.parametrize("invalid_pid", [76543.5, float("inf"), 2**31, 10**50, str(10**50), True])
@pytest.mark.asyncio
async def test_invalid_runner_pid_cannot_probe_cancel_or_clean_workers(monkeypatch, invalid_pid):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": invalid_pid}
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    probes = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: probes.append(pid))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == []
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []
    assert any(
        "worker group for session session-test could not be confirmed stopped" in msg
        for msg in warnings
    )


@pytest.mark.asyncio
async def test_nan_runner_pid_cannot_probe_cancel_or_clean_workers(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": float("nan")}
    sent = _stub_recorded_groups(monkeypatch, module)
    probes = []
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: probes.append(pid))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == []
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.parametrize("valid_pid", [76543, 76543.0, "76543", " +76543 "])
@pytest.mark.asyncio
async def test_integer_runner_pid_forms_are_preserved(monkeypatch, valid_pid):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    db.session["node_metadata"] = {"pid": valid_pid}
    sent = _stub_recorded_groups(monkeypatch, module)
    probes = []

    def dead_runner(pid):
        probes.append(pid)
        return "gone", None

    monkeypatch.setattr(module, "_runner_process_create_time", dead_runner)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == [76543]
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": valid_pid}]
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_unreadable_runner_cannot_cancel_away_recorded_workers(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: ("unknown", None))
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_all_terms_precede_one_grace_then_exact_settlement(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    waits = []

    async def one_wait(delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        waits.append(delay)

    monkeypatch.setattr(module, "sleep", one_wait)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: (
            "missing" if any(pid == witness.identity.pgid for pid, _ in sent) else "matches"
        ),
    )
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0.5
    )
    assert result == 0
    assert sent == [(76541, signal.SIGTERM), (76542, signal.SIGTERM)]
    assert waits == [0.5]
    assert db.settled == [record["spawn_id"] for record in db.records]


@pytest.mark.asyncio
async def test_missing_killpg_refuses_without_settling(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    monkeypatch.delattr(os, "killpg")
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.settled == []


@pytest.mark.parametrize("with_workers", [False, True])
@pytest.mark.parametrize("child_blocked", [False, True])
@pytest.mark.asyncio
async def test_recursive_running_dead_runner_handles_children_before_parent(
    monkeypatch, capsys, with_workers, child_blocked
):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    if not with_workers:
        db.records = []
    calls = []
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: ("gone", None))

    async def no_wait(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
        return None

    monkeypatch.setattr(module, "sleep", no_wait)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: (
            "missing" if any(pid == witness.identity.pgid for pid, _ in sent) else "matches"
        ),
    )

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(db_mod, "StateDB", Context)

    async def resolve(_db, _identifier):
        return "sessions", "session", db.session

    async def children(_db, entity_type, entity_id):
        calls.append("children")
        return [("invocations", "invocation", {"id": "child-invocation"})]

    async def kill_one(_db, entity_type, entity_id, row, **kwargs):
        calls.append("child")
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": "permission_denied" if child_blocked else "sigterm",
            "pid": 987,
        }

    original_cancel = db.cancel_session_if_worker_generation

    async def cancel(*args, **kwargs):
        calls.append("parent")
        return await original_cancel(*args, **kwargs)

    monkeypatch.setattr(module, "_resolve_entity", resolve)
    monkeypatch.setattr(module, "_list_running_children", children)
    monkeypatch.setattr(module, "_kill_one", kill_one)
    db.cancel_session_if_worker_generation = cancel

    result = await module._do_kill("session-test", recursive=True, grace_seconds=0)
    assert calls == ["children", "child", "parent"]
    assert result == (1 if child_blocked else 0)
    assert db.cancelled == [("session-test", "generation-a")]
    if with_workers:
        assert sent == [(76541, signal.SIGTERM), (76542, signal.SIGTERM)]
        assert db.settled == [record["spawn_id"] for record in db.records]
    else:
        assert sent == []
        assert db.settled == []
    output = capsys.readouterr().out
    if not child_blocked:
        assert output.index("killed child invocation") < output.index("killed session")
    if with_workers:
        assert output.index("stopped worker for session") < output.index("killed session")


@pytest.mark.asyncio
async def test_terminal_session_recursive_does_not_expand_to_children(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="failed")
    calls = []

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(db_mod, "StateDB", Context)

    async def resolve(_db, _identifier):
        return "sessions", "session", db.session

    async def children(*args):
        calls.append("children")
        return []

    async def terminal(*args, **kwargs):
        calls.append("terminal")
        return 0

    monkeypatch.setattr(module, "_resolve_entity", resolve)
    monkeypatch.setattr(module, "_list_running_children", children)
    monkeypatch.setattr(module, "_try_recorded_worker_cleanup", terminal)
    assert await module._do_kill("session-test", recursive=True) == 0
    assert calls == ["terminal"]


@pytest.mark.asyncio
async def test_resolve_to_snapshot_resume_never_signals_or_cancels_new_runner(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    stale = dict(db.session)
    stale["node_metadata"] = {"pid": 76543, "pid_create_time": 10.0}
    db.session["cli_worker_generation"] = "generation-b"
    db.session["node_metadata"] = {"pid": 76544, "pid_create_time": 20.0}
    calls = []
    errors = []

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    async def resolve(_db, _identifier):
        return "sessions", "session", stale

    async def children(*args):
        calls.append("children")
        return []

    async def kill_one(*args, **kwargs):
        calls.append("signal")
        return {"signal": "sigterm", "pid": 76544}

    monkeypatch.setattr(db_mod, "StateDB", Context)
    monkeypatch.setattr(module, "_resolve_entity", resolve)
    monkeypatch.setattr(module, "_list_running_children", children)
    monkeypatch.setattr(module, "_kill_one", kill_one)
    monkeypatch.setattr(module, "log_error", errors.append)
    assert await module._do_kill("session-test", recursive=True) == 1
    assert calls == []
    assert db.cancelled == []
    assert db.settled == []
    assert any("changed during cleanup" in message for message in errors)


@pytest.mark.asyncio
async def test_all_refused_groups_still_report_a_concurrent_generation_change(monkeypatch):
    import lionagi.cli.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal

    db = _CleanupDB()
    original = dict(db.session)
    errors = []
    sent = []

    def refuse(identity, **kwargs):
        db.session["cli_worker_generation"] = "generation-b"
        return _RecordedGroupRefusal("host_mismatch")

    monkeypatch.setattr(module, "_admit_recorded_worker_group", refuse)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(module, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, original, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == []
    assert db.settled == []
    assert any("changed during cleanup" in message for message in errors)


@pytest.mark.asyncio
async def test_one_malformed_record_produces_one_identity_warning(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    db.records = [dict(db.records[0])]
    del db.records[0]["pid"]
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.settled == []
    assert sum("recorded worker identity" in message for message in warnings) == 1


@pytest.mark.asyncio
async def test_running_dead_runner_boot_mismatch_preserves_original_refusal(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.records = []
    warnings = []

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    async def resolve(_db, _identifier):
        return "sessions", "session", db.session

    monkeypatch.setattr(db_mod, "StateDB", Context)
    monkeypatch.setattr(module, "_resolve_entity", resolve)
    monkeypatch.setattr(module, "_unaddressable_pid_reason", lambda meta: "boot_mismatch")
    monkeypatch.setattr(module, "warn", warnings.append)
    assert await module._do_kill("session-test") == 1
    assert db.cancelled == []
    assert db.settled == []
    assert any("last booted" in message for message in warnings)


def _serve_db(monkeypatch, module, db):
    """Make the kill command open ``db`` and resolve its one session."""

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    async def resolve(_db, _identifier):
        return "sessions", "session", db.session

    monkeypatch.setattr(db_mod, "StateDB", Context)
    monkeypatch.setattr(module, "_resolve_entity", resolve)


async def _kill_runner_with_ambiguous_boot(monkeypatch, runner_pid):
    """Run the kill command on a running session with recorded workers whose runner
    pid is alive and whose boot time disagrees on Linux; return its db, exit code and
    the worker groups that were signalled."""
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": runner_pid}
    sent = _stub_recorded_groups(monkeypatch, module)
    _serve_db(monkeypatch, module, db)
    monkeypatch.setattr(module, "_unaddressable_pid_reason", lambda meta: "boot_unverifiable")
    monkeypatch.setattr(module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        module, "_terminate_pid", lambda *args, **kwargs: pytest.fail("unexpected signal")
    )
    monkeypatch.setattr(module, "warn", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "log_error", lambda *args, **kwargs: None)
    code = await module._do_kill("session-test")
    return db, code, sent


@pytest.mark.asyncio
async def test_direct_kill_of_a_live_runner_with_ambiguous_boot_cancels_nothing_and_signals_nothing(
    monkeypatch,
):
    db, code, sent = await _kill_runner_with_ambiguous_boot(monkeypatch, 76543)
    assert code == 1
    assert db.session["status"] == "running"
    assert db.cancelled == []
    assert db.settled == []
    assert sent == []


@pytest.mark.asyncio
async def test_direct_kill_with_a_reserved_runner_pid_and_ambiguous_boot_cancels_nothing_and_signals_nothing(
    monkeypatch,
):
    db, code, sent = await _kill_runner_with_ambiguous_boot(monkeypatch, 1)
    assert code == 1
    assert db.session["status"] == "running"
    assert db.cancelled == []
    assert db.settled == []
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["host_mismatch", "in_process", "foreign_mode"])
async def test_recorded_workers_of_a_runner_this_host_cannot_judge_are_reported(
    monkeypatch, reason
):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    warnings = []
    monkeypatch.setattr(module, "_unaddressable_pid_reason", lambda meta: reason)
    monkeypatch.setattr(module, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert db.cancelled == []
    assert db.settled == []
    [message] = [m for m in warnings if "recorded worker group(s) not handled" in m]
    assert "2 recorded worker group(s)" in message
    assert module._REFUSED_SIGNAL_REASONS[reason] in message


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "records"), [("boot_mismatch", True), ("host_mismatch", False)])
async def test_no_unhandled_worker_report_without_live_recorded_workers(
    monkeypatch, reason, records
):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    if not records:
        db.records = []
    warnings = []
    monkeypatch.setattr(module, "_unaddressable_pid_reason", lambda meta: reason)
    monkeypatch.setattr(module, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert not [m for m in warnings if "not handled" in m]


@pytest.mark.asyncio
async def test_unreadable_worker_records_still_kill_the_running_session(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    calls = []
    warnings = []
    errors = []

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    async def resolve(_db, _identifier):
        return "sessions", "session", db.session

    async def unreadable(session_id):
        raise RuntimeError("store unavailable")

    async def recorded(*args, **kwargs):
        calls.append("recorded")
        return 0

    async def kill_one(*args, **kwargs):
        calls.append("signal")
        return {"signal": "sigterm", "pid": 76543}

    db.snapshot_cli_worker_spawns = unreadable
    monkeypatch.setattr(db_mod, "StateDB", Context)
    monkeypatch.setattr(module, "_resolve_entity", resolve)
    monkeypatch.setattr(module, "_try_recorded_worker_cleanup", recorded)
    monkeypatch.setattr(module, "_kill_one", kill_one)
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(module, "log_error", errors.append)
    assert await module._do_kill("session-test") == 1
    assert calls == ["signal"]
    assert any("worker records could not be read (store unavailable)" in m for m in warnings)
    assert any("recorded worker groups were not handled" in m for m in errors)


@pytest.mark.asyncio
async def test_running_dead_runner_resumed_during_grace_settles_old_group(monkeypatch, capsys):
    import lionagi.cli.kill as module

    db = _CleanupDB(status="running")
    db.records = db.records[:1]
    sent = _stub_recorded_groups(monkeypatch, module)
    events = []
    errors = []
    monkeypatch.setattr(module, "_runner_process_create_time", lambda pid: ("gone", None))
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    original_settle = db.settle_cli_worker_spawn
    original_get = db.get_session

    async def settle(*args, **kwargs):
        events.append("settle")
        return await original_settle(*args, **kwargs)

    async def get(*args, **kwargs):
        events.append("final-read")
        return await original_get(*args, **kwargs)

    async def resume_during_grace(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append("resume")
        db.session["status"] = "running"
        db.session["cli_worker_generation"] = "generation-b"
        db.session["updated_at"] = 43.0
        db.session["node_metadata"] = {"pid": 76544}

    db.settle_cli_worker_spawn = settle
    db.get_session = get
    monkeypatch.setattr(module, "sleep", resume_during_grace)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0.5
    )
    assert result == 1
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": 76543}]
    assert sent == [(76541, signal.SIGTERM)]
    assert db.guard_calls == [
        ("session-test", "cancelled", "generation-a", db.records[0]["spawn_id"])
    ]
    assert db.settled == [db.records[0]["spawn_id"]]
    assert events == ["resume", "settle", "final-read"]
    assert db.session["status"] == "running"
    assert db.session["cli_worker_generation"] == "generation-b"
    assert db.session["updated_at"] == 43.0
    assert db.session["node_metadata"] == {"pid": 76544}
    assert any("changed during cleanup" in msg for msg in errors)
    output = capsys.readouterr().out
    assert "terminal status remains" not in output
    assert "killed session" not in output


@pytest.mark.asyncio
async def test_terminal_mixed_refusal_and_resume_never_reports_stale_terminal_status(
    monkeypatch, capsys
):
    import lionagi.cli.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []
    warnings = []
    events = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("identity_mismatch")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    async def resume_during_grace(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append("resume")
        db.session["status"] = "running"
        db.session["cli_worker_generation"] = "generation-b"

    original_settle = db.settle_cli_worker_spawn
    original_get = db.get_session

    async def settle(*args, **kwargs):
        events.append("settle")
        return await original_settle(*args, **kwargs)

    async def get(*args, **kwargs):
        events.append("final-read")
        return await original_get(*args, **kwargs)

    db.settle_cli_worker_spawn = settle
    db.get_session = get
    monkeypatch.setattr(module, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    monkeypatch.setattr(module, "sleep", resume_during_grace)
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(module, "warn", warnings.append)

    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0.5
    )
    assert result == 1
    assert sent == [(76542, signal.SIGTERM)]
    assert db.settled == [db.records[1]["spawn_id"]]
    assert events == ["resume", "settle", "final-read"]
    assert db.session["status"] == "running"
    assert db.session["cli_worker_generation"] == "generation-b"
    assert any("changed during cleanup" in msg for msg in errors)
    assert not any("already in terminal state" in msg for msg in errors)
    assert not any("terminal status remains" in msg for msg in warnings)
    assert "terminal status remains" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_final_session_read_failure_reports_unconfirmed_without_stale_status(
    monkeypatch, capsys
):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    db.records = db.records[:1]
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )

    async def fail_read(_sid):
        raise TimeoutError("read failed")

    db.get_session = fail_read
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == [db.records[0]["spawn_id"]]
    assert any("could not be confirmed stopped" in msg for msg in warnings)
    assert all("terminal status remains" not in msg for msg in warnings)
    assert "terminal status remains" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_terminal_generations_share_grace_and_kill_only_survivor(monkeypatch, capsys):
    import lionagi.cli.kill as module

    db = _CleanupDB()
    db.records[0]["generation_id"] = "old-generation"
    db.records[1]["generation_id"] = "generation-a"
    before = dict(db.session)
    _stub_recorded_groups(monkeypatch, module)
    events = []

    def fake_killpg(pgid, sig):
        assert pgid in (76541, 76542)
        events.append(("signal", pgid, sig))

    def matches(witness):
        pgid = witness.identity.pgid
        if pgid == 76542 and ("signal", pgid, signal.SIGTERM) in events:
            return "missing"
        return "matches"

    def group_state(identity):
        pid = identity.pgid
        signal_needed = signal.SIGKILL if pid == 76541 else signal.SIGTERM
        return "empty" if ("signal", pid, signal_needed) in events else "present"

    async def fake_sleep(delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append(("sleep", delay))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", matches)
    monkeypatch.setattr(module, "_recorded_worker_group_state", group_state)
    monkeypatch.setattr(module, "sleep", fake_sleep)

    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0.5
    )

    assert result == 0
    assert events == [
        ("signal", 76541, signal.SIGTERM),
        ("signal", 76542, signal.SIGTERM),
        ("sleep", 0.5),
        ("signal", 76541, signal.SIGKILL),
    ]
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert db.session == before
    assert db.cancelled == []
    output = capsys.readouterr().out
    assert "pid=76541, pgid=76541, signal=sigkill" in output
    assert "pid=76542, pgid=76542, signal=sigterm" in output


@pytest.mark.asyncio
async def test_terminal_identity_mismatch_never_signals_or_settles_that_record(monkeypatch):
    import lionagi.cli.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    probed = []
    errors = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("identity_mismatch")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    def matches(witness):
        return "missing" if sent else "matches"

    def group_state(identity):
        probed.append(identity.pgid)
        assert identity.pgid == 76542
        return "empty"

    monkeypatch.setattr(module, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", matches)
    monkeypatch.setattr(module, "_recorded_worker_group_state", group_state)
    monkeypatch.setattr(module, "log_error", errors.append)

    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )

    assert result == 1
    assert sent == [(76542, signal.SIGTERM)]
    assert probed == [76542]
    assert db.settled == [db.records[1]["spawn_id"]]
    assert db.records[0]["spawn_id"] not in db.settled
    assert db.cancelled == []
    assert any("already in terminal state: 'failed'" in msg for msg in errors)


@pytest.mark.asyncio
async def test_terminal_changed_signal_guard_stops_remaining_groups_and_settlement(monkeypatch):
    import lionagi.cli.kill as module

    db = _CleanupDB(guards=("unchanged", "changed"))
    sent = _stub_recorded_groups(monkeypatch, module)
    before = dict(db.session)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )

    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == []
    assert db.cancelled == []
    assert db.session == before


@pytest.mark.asyncio
async def test_terminal_already_empty_record_and_stopped_worker_both_settle(monkeypatch, capsys):
    import lionagi.cli.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("leader_not_live")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    monkeypatch.setattr(module, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    monkeypatch.setattr(module, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 0
    assert sent == [(76542, signal.SIGTERM)]
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert errors == []
    output = capsys.readouterr().out
    assert output.count("stopped worker for session") == 1
    assert "pid=76542, pgid=76542, signal=sigterm" in output
    assert "pid=76541" not in output


@pytest.mark.asyncio
async def test_terminal_already_empty_records_without_signal_refuse(monkeypatch):
    import lionagi.cli.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []
    monkeypatch.setattr(
        module,
        "_admit_recorded_worker_group",
        lambda identity, **kwargs: _RecordedGroupRefusal("leader_not_live"),
    )
    monkeypatch.setattr(module, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == []
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert any("already in terminal state: 'failed'" in msg for msg in errors)


@pytest.mark.asyncio
async def test_native_corrupt_worker_row_refuses_entire_snapshot_without_probe(
    tmp_path, monkeypatch
):
    from sqlalchemy import text

    import lionagi.cli.kill as module

    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(db_mod, "settings", SimpleNamespace(LIONAGI_STATE_DB_URL=None))
    session_id, generation, progression = (str(uuid4()) for _ in range(3))
    spawns = [str(uuid4()), str(uuid4())]
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression,
                "status": "running",
                "cli_worker_generation": generation,
                "invocation_kind": "agent",
                "started_at": time.time(),
            }
        )
        for spawn_id, pid in zip(spawns, (76541, 76542), strict=True):
            await db.register_cli_worker_spawn(
                {
                    "spawn_id": spawn_id,
                    "session_id": session_id,
                    "generation_id": generation,
                    "pid": pid,
                    "pgid": pid,
                    "pid_create_time": 10.0,
                    "pid_host": "worker-host",
                    "pid_boot_time": 1.0,
                    "process_identity_mode": "local",
                }
            )
        await db.update_status(
            "session",
            session_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="fixture failure",
        )
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE cli_worker_spawns SET recorded_at = :bad WHERE spawn_id = :id"),
                {"bad": float("inf"), "id": spawns[1]},
            )
        before = await db.get_session(session_id)
        before_spawns = await db.fetch_all(
            "SELECT spawn_id, confirmed_empty_at, recorded_at FROM cli_worker_spawns "
            "WHERE session_id = ? ORDER BY spawn_id",
            (session_id,),
        )
        assert len(before_spawns) == 2
        assert next(row for row in before_spawns if row["spawn_id"] == spawns[1])[
            "recorded_at"
        ] == float("inf")

    probes = []
    signals = []
    warnings = []
    monkeypatch.setattr(
        module, "_admit_recorded_worker_group", lambda *a, **kw: probes.append("admit")
    )
    monkeypatch.setattr(module, "_recorded_worker_group_matches", lambda *a: probes.append("match"))
    monkeypatch.setattr(module, "_recorded_worker_group_state", lambda *a: probes.append("state"))
    monkeypatch.setattr(module, "_runner_process_create_time", lambda *a: probes.append("pid"))
    monkeypatch.setattr(os, "killpg", lambda *a: signals.append(a))
    monkeypatch.setattr(module, "warn", warnings.append)
    result = await _do_kill(session_id, grace_seconds=0)
    assert result == 1
    assert probes == []
    assert signals == []
    assert any("worker records could not be read" in warning for warning in warnings)
    async with StateDB(path) as db:
        assert await db.get_session(session_id) == before
        assert (
            await db.fetch_all(
                "SELECT spawn_id, confirmed_empty_at, recorded_at FROM cli_worker_spawns "
                "WHERE session_id = ? ORDER BY spawn_id",
                (session_id,),
            )
            == before_spawns
        )
        assert all(row["confirmed_empty_at"] is None for row in before_spawns)


async def _seed_native_running_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "native-running-state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(db_mod, "settings", SimpleNamespace(LIONAGI_STATE_DB_URL=None))
    sid, generation, progression, spawn_id = (str(uuid4()) for _ in range(4))
    markers = {"pid": 76543, "pid_create_time": 10.0}
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "cli_worker_generation": generation,
                "node_metadata": markers,
                "invocation_kind": "agent",
                "started_at": time.time(),
            }
        )
        await db.update_session(sid, current_phase="executing")
        await db.register_cli_worker_spawn(
            {
                "spawn_id": spawn_id,
                "session_id": sid,
                "generation_id": generation,
                "pid": 76541,
                "pgid": 76541,
                "pid_create_time": 11.0,
                "pid_host": "worker-host",
                "pid_boot_time": 1.0,
                "process_identity_mode": "local",
            }
        )
        history = await db.fetch_all("SELECT * FROM status_transitions WHERE entity_id = ?", (sid,))
    return path, sid, generation, spawn_id, markers, history


def _mock_native_running_cleanup_processes(monkeypatch, module):
    from lionagi.ln._proc import _RecordedGroupWitness

    signals = []
    runner_probes = []
    unexpected = []
    admissions = []

    def forbidden(label):
        def fail(*args, **kwargs):
            unexpected.append(label)
            raise AssertionError(f"unexpected OS process operation: {label}")

        return fail

    def dead_runner(pid):
        runner_probes.append(pid)
        assert pid == 76543
        return "gone", None

    def admit(identity, **_kwargs):
        admissions.append(identity.pid)
        assert identity.pid == identity.pgid == 76541
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    def fake_killpg(pgid, sig):
        assert pgid == 76541
        signals.append((pgid, sig))

    monkeypatch.setattr(module, "_runner_process_create_time", dead_runner)
    monkeypatch.setattr(module, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(
        module,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if signals else "matches",
    )
    monkeypatch.setattr(module, "_recorded_worker_group_state", lambda identity: "empty")
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", forbidden("os.kill"))
    monkeypatch.setattr(os, "getpgid", forbidden("os.getpgid"))
    monkeypatch.setattr(psutil, "Process", forbidden("psutil.Process"))
    monkeypatch.setattr(psutil, "pids", forbidden("psutil.pids"))
    return signals, runner_probes, unexpected, admissions


@pytest.mark.asyncio
async def test_native_running_dead_runner_cancels_and_settles_recorded_worker(
    tmp_path, monkeypatch, capsys
):
    import lionagi.cli.kill as module

    path, sid, generation, spawn_id, markers, history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    result = await module._do_kill(sid, grace_seconds=0)
    assert result == 0
    assert probes == [76543]
    assert admissions == [76541]
    assert signals == [(76541, signal.SIGTERM)]
    assert unexpected == []
    async with StateDB(path) as db:
        session = await db.get_session(sid)
        spawn = await db.fetch_one(
            "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
        )
        after_history = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    assert session["status"] == "cancelled"
    assert session["current_phase"] is None
    assert session["cli_worker_generation"] == generation
    assert session["node_metadata"] == markers
    assert spawn["confirmed_empty_at"] is not None
    assert len(after_history) == len(history) + 1
    transition = after_history[-1]
    assert transition["previous_status"] == "running"
    assert transition["status"] == "cancelled"
    assert transition["reason_code"] == RunReasons.CANCELLED_MANUAL_KILL
    output = capsys.readouterr().out
    assert output.index("stopped worker for session") < output.index("killed session")
    assert "signal=sigterm" in output
    assert "signal=already_dead" in output


@pytest.mark.asyncio
async def test_native_running_marker_swap_after_snapshot_conflicts_before_signal(
    tmp_path, monkeypatch
):
    import lionagi.cli.kill as module

    path, sid, generation, spawn_id, markers, history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    original_cancel = StateDB.cancel_session_if_worker_generation
    outcomes = []
    errors = []

    async def swap_then_cancel(self, session_id, generation_id, **kwargs):
        assert session_id == sid
        assert generation_id == generation
        assert kwargs["expected_runner_identity"] == markers
        async with StateDB(path) as other:
            await other.update_session(sid, node_metadata=json.dumps({"pid": 76544}))
        outcome = await original_cancel(self, session_id, generation_id, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(StateDB, "cancel_session_if_worker_generation", swap_then_cancel)
    monkeypatch.setattr(module, "log_error", errors.append)
    result = await module._do_kill(sid, grace_seconds=0)
    assert result == 1
    assert outcomes == ["conflict"]
    assert probes == [76543]
    assert admissions == []
    assert signals == []
    assert unexpected == []
    assert any("changed during cleanup" in error for error in errors)
    async with StateDB(path) as db:
        session = await db.get_session(sid)
        spawn = await db.fetch_one(
            "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
        )
        after_history = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    assert session["status"] == "running"
    assert session["current_phase"] == "executing"
    assert session["cli_worker_generation"] == generation
    assert session["node_metadata"] == {"pid": 76544}
    assert spawn["confirmed_empty_at"] is None
    assert after_history == history


async def _runner_ends_before_cancel(monkeypatch, path, sid, generation):
    """Record the runner's own end after the snapshot and before the guarded cancel."""
    original_cancel = StateDB.cancel_session_if_worker_generation
    outcomes = []

    async def end_then_cancel(self, session_id, generation_id, **kwargs):
        assert (session_id, generation_id) == (sid, generation)
        async with StateDB(path) as other:
            assert await other.update_status(
                "session", sid, new_status="completed", reason_code="run.completed.ok"
            )
        outcome = await original_cancel(self, session_id, generation_id, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(StateDB, "cancel_session_if_worker_generation", end_then_cancel)
    return outcomes


@pytest.mark.asyncio
async def test_native_runner_end_after_snapshot_still_stops_recorded_worker(
    tmp_path, monkeypatch, capsys
):
    import lionagi.cli.kill as module

    path, sid, generation, spawn_id, _markers, _history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    errors = []
    monkeypatch.setattr(module, "log_error", errors.append)
    outcomes = await _runner_ends_before_cancel(monkeypatch, path, sid, generation)
    result = await module._do_kill(sid, grace_seconds=0)
    assert outcomes == ["already_terminal"]
    assert result == 0
    assert probes == [76543]
    assert admissions == [76541]
    assert signals == [(76541, signal.SIGTERM)]
    assert unexpected == []
    assert errors == []
    async with StateDB(path) as db:
        session = await db.get_session(sid)
        spawn = await db.fetch_one(
            "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
        )
        statuses = [
            row["status"]
            for row in await db.fetch_all(
                "SELECT status FROM status_transitions WHERE entity_id = ? ORDER BY created_at",
                (sid,),
            )
        ]
    assert session["status"] == "completed"
    assert spawn["confirmed_empty_at"] is not None
    assert "cancelled" not in statuses
    output = capsys.readouterr().out
    assert "terminal status remains 'completed'" in output


@pytest.mark.asyncio
async def test_native_runner_end_after_snapshot_without_workers_reports_nothing_to_kill(
    tmp_path, monkeypatch
):
    import lionagi.cli.kill as module

    path, sid, generation, spawn_id, _markers, _history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    async with StateDB(path) as db:
        await db.execute("DELETE FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,))
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    errors = []
    monkeypatch.setattr(module, "log_error", errors.append)
    outcomes = await _runner_ends_before_cancel(monkeypatch, path, sid, generation)
    result = await module._do_kill(sid, grace_seconds=0)
    assert outcomes == ["already_terminal"]
    assert result == 1
    assert signals == [] and admissions == [] and unexpected == []
    assert errors == [
        f"session {sid[:12]} is already in terminal state: 'completed' — nothing to kill"
    ]
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_signal", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
async def test_native_guard_refuses_a_signal_after_its_deadline(tmp_path, monkeypatch, late_signal):
    """A slow synchronous witness read must not authorize a late signal."""
    import lionagi.cli.kill as module
    from lionagi.ln.concurrency import fail_after

    path, sid, generation, spawn_id, _markers, history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)

    budget = 0.20
    slow_seconds = 0.50
    guard_entries = []
    probe_phases = []
    slow_elapsed = []
    in_guard = False
    original_guard = StateDB.guard_cli_worker_signal
    old_budget = db_mod._CLI_WORKER_OPERATION_TIMEOUT_SECONDS

    @contextlib.asynccontextmanager
    async def instrumented_guard(self, *args, **kwargs):
        nonlocal in_guard
        next_entry = len(guard_entries) + 1
        is_target = (late_signal == signal.SIGTERM and next_entry == 1) or (
            late_signal == signal.SIGKILL and next_entry == 2
        )
        if is_target:
            monkeypatch.setattr(db_mod, "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS", budget)
        try:
            async with original_guard(self, *args, **kwargs) as verdict:
                guard_entries.append(verdict)
                in_guard = True
                try:
                    yield verdict
                finally:
                    in_guard = False
        finally:
            monkeypatch.setattr(db_mod, "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS", old_budget)

    def match(_witness):
        phase = f"guard-{len(guard_entries)}" if in_guard else "outside"
        probe_phases.append(phase)
        is_target = in_guard and (
            (late_signal == signal.SIGTERM and len(guard_entries) == 1)
            or (late_signal == signal.SIGKILL and len(guard_entries) == 2)
        )
        if is_target:
            started = time.monotonic()
            time.sleep(slow_seconds)
            slow_elapsed.append(time.monotonic() - started)
        return "matches"

    monkeypatch.setattr(StateDB, "guard_cli_worker_signal", instrumented_guard)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", match)
    result = await module._do_kill(sid, grace_seconds=0)

    expected_entries = ["unchanged"] if late_signal == signal.SIGTERM else ["unchanged"] * 2
    expected_phases = (
        ["guard-1"] if late_signal == signal.SIGTERM else ["guard-1", "outside", "guard-2"]
    )
    expected_signals = [] if late_signal == signal.SIGTERM else [(76541, signal.SIGTERM)]
    assert guard_entries == expected_entries
    assert probe_phases == expected_phases
    assert len(slow_elapsed) == 1 and slow_elapsed[0] >= 2 * budget
    assert probes == [76543] and admissions == [76541]
    assert unexpected == []
    assert signals == expected_signals, "an expired guard sent an additional signal"
    assert result == 1
    assert any("could not be confirmed stopped" in warning for warning in warnings)

    # A new database handle must acquire the same writer guard after failure.
    # Calling the original guard avoids shortening this independent check.
    with fail_after(old_budget):
        async with StateDB(path) as fresh:
            async with original_guard(fresh, sid, "cancelled", generation, spawn_id) as verdict:
                assert verdict == "unchanged"
            session = await fresh.get_session(sid)
            spawn = await fresh.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
            )
            after_history = await fresh.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
            )
    assert session["status"] == "cancelled"
    assert session["cli_worker_generation"] == generation
    assert spawn["confirmed_empty_at"] is None
    assert len(after_history) == len(history) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_signal", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
@pytest.mark.parametrize("delivery", ["direct", "threadsafe"])
async def test_native_guard_propagates_cancel_before_checkpoint(
    tmp_path, monkeypatch, cancel_signal, delivery
):
    """A cancellation queued at the guarded read is delivered before the signal."""
    import lionagi.cli.kill as module
    from lionagi.ln.concurrency import fail_after

    path, sid, generation, spawn_id, _markers, _history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    async with StateDB(path) as setup:
        await setup.update_status(
            "session", sid, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        session_before = await setup.get_session(sid)
        history_before = await setup.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    entries = []
    phases = []
    requests = []
    thread_errors = []
    in_guard = False
    original_guard = StateDB.guard_cli_worker_signal
    task = None

    @contextlib.asynccontextmanager
    async def guarded_read(self, *args, **kwargs):
        nonlocal in_guard
        async with original_guard(self, *args, **kwargs) as verdict:
            entries.append(verdict)
            in_guard = True
            try:
                target = (cancel_signal == signal.SIGTERM and len(entries) == 1) or (
                    cancel_signal == signal.SIGKILL and len(entries) == 2
                )
                if target:
                    current = asyncio.current_task()
                    assert current is task
                    if delivery == "direct":
                        requests.append(current.cancel())
                    else:
                        loop = asyncio.get_running_loop()

                        def queue_cancel():
                            try:
                                loop.call_soon_threadsafe(task.cancel)
                            except Exception as exc:
                                thread_errors.append(repr(exc))
                            else:
                                requests.append(True)

                        sender = threading.Thread(target=queue_cancel, daemon=True)
                        sender.start()
                        sender.join(timeout=2.0)
                        assert not sender.is_alive(), "cancellation sender did not finish"
                yield verdict
            finally:
                in_guard = False

    def match(_witness):
        phases.append(f"guard-{len(entries)}" if in_guard else "outside")
        return "matches"

    monkeypatch.setattr(StateDB, "guard_cli_worker_signal", guarded_read)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", match)
    task = asyncio.create_task(module._do_kill(sid, grace_seconds=0))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert entries == (["unchanged"] if cancel_signal == signal.SIGTERM else ["unchanged"] * 2)
    assert requests == [True] and thread_errors == []
    assert signals == ([] if cancel_signal == signal.SIGTERM else [(76541, signal.SIGTERM)])
    assert phases == ([] if cancel_signal == signal.SIGTERM else ["guard-1", "outside"])
    assert probes == [] and admissions == [76541] and unexpected == []
    with fail_after(db_mod._CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
        async with StateDB(path) as fresh:
            async with original_guard(fresh, sid, "failed", generation, spawn_id) as verdict:
                assert verdict == "unchanged"
            session = await fresh.get_session(sid)
            spawn = await fresh.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
            )
            history = await fresh.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
            )
    assert session == session_before and history == history_before
    assert spawn["confirmed_empty_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_signal", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
async def test_native_checkpoint_rechecks_group_identity_before_signal(
    tmp_path, monkeypatch, target_signal
):
    """Replacing a simulated group at the checkpoint cannot authorize a signal."""
    import lionagi.cli.kill as module
    from lionagi.ln.concurrency import fail_after

    path, sid, generation, spawn_id, _markers, _history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    async with StateDB(path) as setup:
        await setup.update_status(
            "session", sid, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        session_before = await setup.get_session(sid)
        history_before = await setup.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    holder = {"identity": (76541, 11.0)}
    entries = []
    reads = []
    events = []
    in_guard = False
    original_guard = StateDB.guard_cli_worker_signal

    def replace():
        holder["identity"] = (76541, 999.0)
        events.append("replacement")

    @contextlib.asynccontextmanager
    async def guarded_read(self, *args, **kwargs):
        nonlocal in_guard
        async with original_guard(self, *args, **kwargs) as verdict:
            entries.append(verdict)
            in_guard = True
            try:
                target = (target_signal == signal.SIGTERM and len(entries) == 1) or (
                    target_signal == signal.SIGKILL and len(entries) == 2
                )
                if target:
                    asyncio.get_running_loop().call_soon(replace)
                yield verdict
            finally:
                in_guard = False

    def match(witness):
        assert witness.identity.pgid == 76541
        reads.append((f"guard-{len(entries)}" if in_guard else "outside", holder["identity"]))
        return "matches" if holder["identity"] == (76541, 11.0) else "missing"

    def fake_signal(pgid, sig):
        assert pgid == 76541 and sig in (signal.SIGTERM, signal.SIGKILL)
        events.append(("signal", holder["identity"], sig))
        signals.append((pgid, sig))

    monkeypatch.setattr(StateDB, "guard_cli_worker_signal", guarded_read)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", match)
    monkeypatch.setattr(module, "_recorded_worker_group_state", lambda _identity: "present")
    monkeypatch.setattr(os, "killpg", fake_signal)
    result = await module._do_kill(sid, grace_seconds=0)

    assert entries == (["unchanged"] if target_signal == signal.SIGTERM else ["unchanged"] * 2)
    assert "replacement" in events and holder["identity"] == (76541, 999.0)
    assert signals == ([] if target_signal == signal.SIGTERM else [(76541, signal.SIGTERM)]), events
    assert reads[-1][1] == (76541, 999.0)
    assert result == 1 and probes == [] and admissions == [76541] and unexpected == []
    with fail_after(db_mod._CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
        async with StateDB(path) as fresh:
            async with original_guard(fresh, sid, "failed", generation, spawn_id) as verdict:
                assert verdict == "unchanged"
            session = await fresh.get_session(sid)
            spawn = await fresh.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
            )
            history = await fresh.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
            )
    assert session == session_before and history == history_before
    assert spawn["confirmed_empty_at"] is None


@pytest.mark.asyncio
async def test_native_cancel_during_identity_read_preserves_one_verified_term(
    tmp_path, monkeypatch
):
    """Cancellation during the synchronous read follows its already verified TERM."""
    import lionagi.cli.kill as module
    from lionagi.ln.concurrency import fail_after

    path, sid, generation, spawn_id, _markers, _history = await _seed_native_running_cleanup(
        tmp_path, monkeypatch
    )
    async with StateDB(path) as setup:
        await setup.update_status(
            "session", sid, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        session_before = await setup.get_session(sid)
        history_before = await setup.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    signals, probes, unexpected, admissions = _mock_native_running_cleanup_processes(
        monkeypatch, module
    )
    entries = []
    requests = []
    original_guard = StateDB.guard_cli_worker_signal
    task = None

    @contextlib.asynccontextmanager
    async def observed_guard(self, *args, **kwargs):
        async with original_guard(self, *args, **kwargs) as verdict:
            entries.append(verdict)
            yield verdict

    def match(_witness):
        current = asyncio.current_task()
        assert current is task and entries == ["unchanged"]
        requests.append(current.cancel())
        return "matches"

    monkeypatch.setattr(StateDB, "guard_cli_worker_signal", observed_guard)
    monkeypatch.setattr(module, "_recorded_worker_group_matches", match)
    task = asyncio.create_task(module._do_kill(sid, grace_seconds=0))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert entries == ["unchanged"] and requests == [True]
    assert signals == [(76541, signal.SIGTERM)]
    assert probes == [] and admissions == [76541] and unexpected == []
    with fail_after(db_mod._CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
        async with StateDB(path) as fresh:
            async with original_guard(fresh, sid, "failed", generation, spawn_id) as verdict:
                assert verdict == "unchanged"
            session = await fresh.get_session(sid)
            spawn = await fresh.fetch_one(
                "SELECT * FROM cli_worker_spawns WHERE spawn_id = ?", (spawn_id,)
            )
            history = await fresh.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_id = ?", (sid,)
            )
    assert session == session_before and history == history_before
    assert spawn["confirmed_empty_at"] is None
