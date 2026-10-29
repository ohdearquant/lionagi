# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A cross-interpreter Linux boot-time drift must not cancel a live session."""

import json
import signal
import time
import uuid
from types import SimpleNamespace

import psutil
import pytest

from lionagi.cli import kill as kill_mod
from lionagi.ln import _proc
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


class _LinuxBackend:
    def __init__(self, boot_clock):
        self.boot_clock = boot_clock

    def create_time(self, monotonic=False):
        return 5.0 if monotonic else self.boot_clock() + 5.0


class _Process:
    def __init__(self, boot_clock, session_id):
        self._proc = _LinuxBackend(boot_clock)
        self.session_id = session_id

    def create_time(self):
        return self._proc.create_time()

    def environ(self):
        return {"LIONAGI_SESSION_ID": self.session_id}

    def cmdline(self):
        return ["python", "-m", "lionagi.cli"]


@pytest.mark.parametrize(("drift", "reason"), [(1.0, "start"), (6.0, "boot")])
@pytest.mark.asyncio
async def test_stale_sweep_does_not_cancel_live_linux_process_after_boot_clock_drift(
    tmp_path, monkeypatch, capsys, drift, reason
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    clock = {"boot": 1000.0}

    def boot_time():
        return clock["boot"]

    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(
        psutil, "_psplatform", SimpleNamespace(Process=_LinuxBackend, boot_time=boot_time)
    )
    monkeypatch.setattr(psutil, "boot_time", boot_time)
    monkeypatch.setattr(kill_mod.socket, "gethostname", lambda: "test-host")
    live_process = _Process(boot_time, sid)

    # Select a reader in each simulated interpreter. Both observe the same PID
    # and 5-second boot-relative birth; only their frozen boot epoch differs.
    recording_reader = _proc._select_raw_create_time_reader()
    recorded_start = recording_reader(live_process)
    clock["boot"] = 1000.0 + drift
    checking_reader = _proc._select_raw_create_time_reader()
    assert recorded_start == 1005.0 and checking_reader(live_process) == 1005.0 + drift
    assert live_process._proc.create_time(monotonic=True) == 5.0
    monkeypatch.setattr(_proc, "_read_raw_create_time", checking_reader)

    def process(pid):
        assert pid == 4242
        return live_process

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda pid: pid == 4242)

    def unexpected_signal(*args, **kwargs):
        raise AssertionError("the test must never signal a process")

    monkeypatch.setattr(kill_mod, "_terminate_pid", unexpected_signal)
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "started_at": time.time() - 7200,
                "node_metadata": {
                    "pid": 4242,
                    "pid_create_time": recorded_start,
                    "pid_boot_time": 1000.0,
                    "pid_host": "test-host",
                    "process_identity_mode": "local",
                },
            }
        )

    for expected_count in (1, 2):
        assert await kill_mod._do_kill_all_stale(threshold_seconds=3600, dry_run=False) == 0
        async with StateDB(path) as db:
            row = await db.get_session(sid)
            assert row["status"] == "running"
            assert row["node_metadata"]["pid"] == 4242
            assert row["node_metadata"]["pid_create_time"] == recorded_start
            assert row["node_metadata"]["unverifiable_count"] == expected_count
            transitions = await db.fetch_all(
                "SELECT status, source FROM status_transitions WHERE entity_id = ?", (sid,)
            )
            assert not any(t["status"] == "cancelled" for t in transitions)
        out = capsys.readouterr().out
        assert f"skipped_linux_{reason}_mismatch=1" in out
        assert "skipped_permission_denied=0" in out
        assert "cancelled stale session" not in out


def test_inherited_marker_cannot_rescue_a_linux_start_mismatch(monkeypatch):
    monkeypatch.setattr(psutil, "LINUX", True)
    reads = []

    class Descendant:
        def environ(self):
            reads.append("environment")
            return {"LIONAGI_SESSION_ID": "inherited"}

    monkeypatch.setattr(psutil, "Process", lambda _pid: Descendant())
    monkeypatch.setattr(kill_mod, "raw_create_time", lambda _proc: 102.0)
    reasons = []
    assert (
        kill_mod._check_pid_identity(
            4242,
            "lionagi",
            expected_session_id="inherited",
            expected_create_time=100.0,
            _reason_out=reasons,
        )
        == "unverifiable"
    )
    assert reasons == ["start_mismatch"]
    assert reads == []


def test_non_linux_mismatch_keeps_previous_verdict(monkeypatch):
    monkeypatch.setattr(psutil, "LINUX", False)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_mod, "raw_create_time", lambda _proc: 102.0)
    assert kill_mod._check_pid_identity(4242, "lionagi", expected_create_time=100.0) == "not_ours"


@pytest.mark.parametrize("kind", ["start", "boot"])
@pytest.mark.asyncio
async def test_imported_ambiguous_process_is_retained(tmp_path, monkeypatch, capsys, kind):
    from lionagi.cli import _runs

    path, runs_root = tmp_path / "state.db", tmp_path / "runs"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_mod, "raw_create_time", lambda _proc: 1006.0)
    monkeypatch.setattr(
        kill_mod, "_terminate_pid", lambda *args, **kwargs: pytest.fail("unexpected signal")
    )
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = {
        "status": "running",
        _runs.PERSISTENCE_DEGRADED_REASON_FIELD: "database locked",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0 if kind != "boot" else 900.0,
    }
    run_dir = runs_root / sid
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(manifest))
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "source_kind": "imported_fs",
                "started_at": time.time() - 7200,
            }
        )
    assert kill_mod._imported_degraded_process_is_live(sid)
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "running"
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(t["status"] == "cancelled" for t in transitions)
    out = capsys.readouterr().out
    assert "skipped_unverifiable_pid=1" in out
    if kind in ("start", "boot"):
        assert f"skipped_linux_{kind}_mismatch=1" in out


def test_permission_denial_has_its_own_observation(monkeypatch):
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())

    def deny(_proc):
        raise psutil.AccessDenied(pid=4242)

    monkeypatch.setattr(kill_mod, "raw_create_time", deny)
    reasons = []
    assert (
        kill_mod._check_pid_identity(
            4242, "lionagi", expected_create_time=100.0, _reason_out=reasons
        )
        == "unverifiable"
    )
    assert reasons == ["permission_denied"]


@pytest.mark.asyncio
async def test_sweep_reports_start_mismatch_and_permission_denial_separately(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(pid=pid))
    probes = []

    def read_start(proc):
        probes.append(proc.pid)
        if proc.pid == 4243:
            raise psutil.AccessDenied(pid=proc.pid)
        return 1001.0

    monkeypatch.setattr(kill_mod, "raw_create_time", read_start)
    async with StateDB(path) as db:
        for pid in (4242, 4243):
            sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
            await db.create_progression(progression)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": progression,
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": {"pid": pid, "pid_create_time": 1000.0},
                }
            )
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    out = capsys.readouterr().out
    assert "skipped_linux_start_mismatch=1" in out
    assert "skipped_linux_boot_mismatch=0" in out
    assert "skipped_permission_denied=1" in out
    assert sorted(probes) == [4242, 4243]


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ('{"status": "running"}', "not_ours"),
        ('{"status": "completed"}', "not_ours"),
        ('{"status":', "not_ours"),
        (None, "not_ours"),
    ],
    ids=["ordinary-running", "ordinary-completed", "unreadable", "missing"],
)
def test_ordinary_and_unreadable_imported_manifests_are_distinct(
    tmp_path, monkeypatch, manifest, expected
):
    from lionagi.cli import _runs

    root = tmp_path / "runs"
    monkeypatch.setattr(_runs, "RUNS_ROOT", root)
    if manifest is not None:
        run_dir = root / "test-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(manifest)
    verdict, _ = kill_mod._imported_degraded_process_identity("test-run")
    assert verdict == expected
    assert kill_mod._imported_degraded_process_is_live("test-run") is (expected == "unverifiable")


@pytest.mark.parametrize("kind", ["start", "boot"])
@pytest.mark.asyncio
async def test_direct_kill_refuses_ambiguous_linux_identity_without_signal_or_terminal(
    tmp_path, monkeypatch, kind
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill_mod, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_mod, "raw_create_time", lambda _proc: 102.0)
    sent, warnings = [], []
    monkeypatch.setattr(kill_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "node_metadata": {
                    "pid": 4242,
                    "pid_create_time": 100.0,
                    "pid_boot_time": 900.0 if kind == "boot" else 1000.0,
                },
            }
        )
        row = await db.get_session(sid)
        result = await kill_mod._kill_one(db, "session", sid, row, user_reason="")
        assert result["signal"] == (
            "boot_unverifiable" if kind == "boot" else "identity_unverifiable"
        )
        assert (await db.get_session(sid))["status"] == "running"
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(t["status"] == "cancelled" for t in transitions)
    assert sent == []
    if kind == "start":
        assert any("kill skipped" in text for text in warnings)
    assert any(
        "boot time" in text if kind == "boot" else "identified reliably" in text
        for text in warnings
    )


@pytest.mark.asyncio
async def test_unknown_on_escalation_reports_sent_sigterm_without_a_terminal_write(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill_mod, "_pid_is_zombie", lambda _pid: False)
    verdicts = iter(["ours", "unverifiable"])

    def identity(*args, **kwargs):
        verdict = next(verdicts)
        if verdict == "unverifiable":
            kwargs["_reason_out"].append("start_mismatch")
        return verdict

    monkeypatch.setattr(kill_mod, "_check_pid_identity", identity)
    sent, warnings = [], []
    monkeypatch.setattr(kill_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "node_metadata": {"pid": 4242, "pid_create_time": 100.0},
            }
        )
        result = await kill_mod._kill_one(
            db, "session", sid, await db.get_session(sid), user_reason="", grace_seconds=0
        )
        assert result["signal"] == "identity_unverifiable_after_sigterm"
        assert (await db.get_session(sid))["status"] == "running"
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(t["status"] == "cancelled" for t in transitions)
    assert sent == [(4242, signal.SIGTERM)]
    assert any("received SIGTERM" in text and "escalation skipped" in text for text in warnings)


@pytest.mark.parametrize("phase", ["initial", "escalation"])
@pytest.mark.parametrize("linux", [True, False])
def test_non_epoch_unknown_kill_verdict_keeps_previous_reason(monkeypatch, phase, linux):
    monkeypatch.setattr(psutil, "LINUX", linux)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill_mod, "_pid_is_zombie", lambda _pid: False)
    verdicts = iter(["unverifiable"] if phase == "initial" else ["ours", "unverifiable"])
    monkeypatch.setattr(kill_mod, "_check_pid_identity", lambda *args, **kw: next(verdicts))
    sent = []
    monkeypatch.setattr(kill_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    result = kill_mod._terminate_pid(4242, grace_seconds=0, expected_cmd="lionagi")
    assert result == "identity_mismatch"
    assert sent == ([] if phase == "initial" else [(4242, signal.SIGTERM)])


@pytest.mark.parametrize("linux", [True, False])
def test_matching_session_marker_without_start_keeps_legacy_positive(monkeypatch, linux):
    monkeypatch.setattr(psutil, "LINUX", linux)

    class Process:
        def environ(self):
            return {"LIONAGI_SESSION_ID": "session-marker"}

    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())
    monkeypatch.setattr(
        kill_mod, "raw_create_time", lambda _proc: pytest.fail("unexpected start-time probe")
    )
    assert (
        kill_mod._check_pid_identity(4242, "lionagi", expected_session_id="session-marker")
        == "ours"
    )


@pytest.mark.parametrize("kind", ["missing_identity", "missing_manifest", "ordinary", "gone"])
@pytest.mark.asyncio
async def test_imported_non_epoch_gaps_keep_legacy_sweep_behavior(tmp_path, monkeypatch, kind):
    from lionagi.cli import _runs

    path, runs_root = tmp_path / "state.db", tmp_path / "runs"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: kind != "gone")
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = {
        "status": "running",
        _runs.PERSISTENCE_DEGRADED_REASON_FIELD: "database locked",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0,
    }
    if kind == "missing_identity":
        del manifest["pid_create_time"]
    if kind == "ordinary":
        del manifest[_runs.PERSISTENCE_DEGRADED_REASON_FIELD]
    if kind != "missing_manifest":
        run_dir = runs_root / sid
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps(manifest))
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "source_kind": "imported_fs",
                "started_at": time.time() - 7200,
            }
        )
    assert not kill_mod._imported_degraded_process_is_live(sid)
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "cancelled"


@pytest.mark.parametrize("pid", [None, 4242])
@pytest.mark.asyncio
async def test_boot_disagreement_without_live_pid_keeps_legacy_sweep(tmp_path, monkeypatch, pid):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(kill_mod, "_pid_alive", lambda _pid: False)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "started_at": time.time() - 7200,
                "node_metadata": {"pid": pid, "pid_boot_time": 900.0},
            }
        )
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "cancelled"
