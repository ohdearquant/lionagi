# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A cross-interpreter Linux boot-time drift must not cancel a live session."""

import json
import signal
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

import lionagi.cli._kill_service as kill_service
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


@pytest.mark.parametrize(("drift", "reason"), [(1.0, "start_mismatch"), (6.0, "boot_disagreement")])
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
    monkeypatch.setattr(_proc.socket, "gethostname", lambda: "test-host")
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
    patched_pid_alive = lambda pid: pid == 4242
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)

    def unexpected_signal(*args, **kwargs):
        raise AssertionError("the test must never signal a process")

    monkeypatch.setattr(kill_service, "_terminate_pid", unexpected_signal)
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
        assert f"skipped_linux_{reason}=1" in out
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
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 102.0)
    reasons = []
    assert (
        kill_mod.check_pid_identity(
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
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 102.0)
    assert kill_mod.check_pid_identity(4242, "lionagi", expected_create_time=100.0) == "not_ours"


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
    patched_pid_alive = lambda pid: pid == 4242
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 1006.0)
    monkeypatch.setattr(
        kill_service, "_terminate_pid", lambda *args, **kwargs: pytest.fail("unexpected signal")
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
    assert kill_service.imported_degraded_process_is_live(sid)
    verdict, reason = kill_mod._imported_degraded_process_identity(sid)
    assert (verdict, reason) == (
        "unverifiable",
        "start_mismatch" if kind == "start" else "boot_disagreement",
    )
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "running"
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(t["status"] == "cancelled" for t in transitions)
    out = capsys.readouterr().out
    assert "skipped_unverifiable_pid=1" in out
    reason = "start_mismatch" if kind == "start" else "boot_disagreement"
    assert f"skipped_linux_{reason}=1" in out


@pytest.mark.parametrize("reason", ["permission_denied", "identity_unknown"])
@pytest.mark.asyncio
async def test_imported_process_whose_identity_cannot_be_checked_is_retained(
    tmp_path, monkeypatch, capsys, reason
):
    """A live recorded process that cannot be inspected is neither ours nor a stranger's:
    the sweep keeps the session running and unsettled."""
    from lionagi.cli import _runs

    path, runs_root = tmp_path / "state.db", tmp_path / "runs"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda pid: pid == 4242
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(
        kill_service, "_terminate_pid", lambda *args, **kwargs: pytest.fail("unexpected signal")
    )
    if reason == "permission_denied":

        def deny(_pid):
            raise psutil.AccessDenied(pid=4242)

        monkeypatch.setattr(psutil, "Process", deny)
    else:

        def unknown(*args, _reason_out=None, **kwargs):
            _reason_out.append("identity_unknown")
            return "unverifiable"

        monkeypatch.setattr(kill_mod, "check_pid_identity", unknown)
        monkeypatch.setattr(kill_service, "check_pid_identity", unknown)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = {
        "status": "running",
        _runs.PERSISTENCE_DEGRADED_REASON_FIELD: "database locked",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0,
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
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        row = await db.get_session(sid)
        assert row["status"] == "running"
        assert row["node_metadata"]["unverifiable_count"] == 1
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(t["status"] == "cancelled" for t in transitions)
    out = capsys.readouterr().out
    assert "skipped_unverifiable_pid=1" in out
    assert "cancelled stale session" not in out
    assert kill_mod._imported_degraded_process_identity(sid) == ("unverifiable", reason)
    assert kill_service.imported_degraded_process_is_live(sid)


def test_permission_denial_has_its_own_observation(monkeypatch):
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())

    def deny(_proc):
        raise psutil.AccessDenied(pid=4242)

    monkeypatch.setattr(kill_service, "raw_create_time", deny)
    reasons = []
    assert (
        kill_mod.check_pid_identity(
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
    patched_pid_alive = lambda _pid: True
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(pid=pid))
    probes = []

    def read_start(proc):
        probes.append(proc.pid)
        if proc.pid == 4243:
            raise psutil.AccessDenied(pid=proc.pid)
        return 1001.0

    monkeypatch.setattr(kill_service, "raw_create_time", read_start)
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
    assert "skipped_linux_boot_disagreement=0" in out
    assert "skipped_permission_denied=1" in out
    assert sorted(probes) == [4242, 4243]


@pytest.mark.asyncio
async def test_stale_sweep_counts_identity_unknown(tmp_path, monkeypatch, capsys):
    """A live process whose identity cannot be checked is kept, and the sweep summary
    counts it under its own reason like the other unverifiable reasons."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda pid: pid == 4242
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(
        kill_service, "_terminate_pid", lambda *args, **kwargs: pytest.fail("unexpected signal")
    )

    def unknown(*args, _reason_out=None, **kwargs):
        if _reason_out is not None:
            _reason_out.append("identity_unknown")
        return "unverifiable"

    monkeypatch.setattr(kill_mod, "check_pid_identity", unknown)
    monkeypatch.setattr(kill_service, "check_pid_identity", unknown)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "started_at": time.time() - 7200,
                "node_metadata": {"pid": 4242, "pid_create_time": 1005.0, "pid_boot_time": 1000.0},
            }
        )
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    out = capsys.readouterr().out
    assert "skipped_identity_unknown=1" in out


@pytest.mark.parametrize(
    ("manifest", "expected", "expected_reason"),
    [
        ('{"status": "running"}', "unverifiable", "no_recorded_process"),
        ('{"status": "completed"}', "not_ours", None),
        ('{"status":', "unverifiable", "manifest_unreadable"),
        (None, "not_ours", None),
    ],
    ids=["ordinary-running", "ordinary-completed", "unreadable", "missing"],
)
def test_ordinary_and_unreadable_imported_manifests_are_distinct(
    tmp_path, monkeypatch, manifest, expected, expected_reason
):
    from lionagi.cli import _runs

    root = tmp_path / "runs"
    monkeypatch.setattr(_runs, "RUNS_ROOT", root)
    if manifest is not None:
        run_dir = root / "test-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(manifest)
    verdict, reason = kill_mod._imported_degraded_process_identity("test-run")
    assert (verdict, reason) == (expected, expected_reason)
    assert kill_service.imported_degraded_process_is_live("test-run") is (
        expected == "unverifiable"
    )


@pytest.mark.parametrize("kind", ["start", "boot"])
@pytest.mark.asyncio
async def test_direct_kill_refuses_ambiguous_linux_identity_without_signal_or_terminal(
    tmp_path, monkeypatch, kind
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda _pid: True
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 102.0)
    sent, warnings = [], []
    monkeypatch.setattr(kill_service.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
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
        result = await kill_mod.kill_one(db, "session", sid, row, user_reason="")
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
    patched_pid_alive = lambda _pid: True
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_is_zombie", lambda _pid: False)
    verdicts = iter(["ours", "unverifiable"])

    def identity(*args, **kwargs):
        verdict = next(verdicts)
        if verdict == "unverifiable":
            kwargs["_reason_out"].append("start_mismatch")
        return verdict

    monkeypatch.setattr(kill_mod, "check_pid_identity", identity)
    monkeypatch.setattr(kill_service, "check_pid_identity", identity)
    sent, warnings = [], []
    monkeypatch.setattr(kill_service.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
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
        result = await kill_mod.kill_one(
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
    patched_pid_alive = lambda _pid: True
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_is_zombie", lambda _pid: False)
    verdicts = iter(["unverifiable"] if phase == "initial" else ["ours", "unverifiable"])
    patched_check_pid_identity = lambda *args, **kw: next(verdicts)
    monkeypatch.setattr(kill_mod, "check_pid_identity", patched_check_pid_identity)
    monkeypatch.setattr(kill_service, "check_pid_identity", patched_check_pid_identity)
    sent = []
    monkeypatch.setattr(kill_service.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    result = kill_service._terminate_pid(4242, grace_seconds=0, expected_cmd="lionagi")
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
        kill_service, "raw_create_time", lambda _proc: pytest.fail("unexpected start-time probe")
    )
    assert (
        kill_mod.check_pid_identity(4242, "lionagi", expected_session_id="session-marker") == "ours"
    )


@pytest.mark.parametrize("kind", ["missing_manifest", "ordinary", "gone"])
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
    patched_pid_alive = lambda _pid: kind != "gone"
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "check_pid_identity", lambda *_args, **_kwargs: "not_ours")
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = {
        "status": "running",
        _runs.PERSISTENCE_DEGRADED_REASON_FIELD: "database locked",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0,
    }
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
    assert not kill_service.imported_degraded_process_is_live(sid)
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "cancelled"


def _imported_run_manifest(**overrides) -> str:
    """A running manifest recording a live process; a ``None`` override drops that field."""
    manifest = {
        "status": "running",
        "pid": 4242,
        "pid_create_time": 1005.0,
        "pid_boot_time": 1000.0,
    }
    manifest.update(overrides)
    return json.dumps({key: value for key, value in manifest.items() if value is not None})


async def _seed_stale_imported_session(
    tmp_path, monkeypatch, *, manifest_text, alive, identity, via_import=False
):
    """Seed one stale imported session whose run.json holds ``manifest_text``.

    The pid probe and the process identity check are stubbed; ``identity`` is the
    ``(result, reason)`` pair the identity check answers. With ``via_import`` the session row is
    created by importing the run, as ``li state import`` does; otherwise it is inserted directly
    with a start time two hours old. Returns the state db path and session id.
    """
    from lionagi.cli import _runs

    path, runs_root = tmp_path / "state.db", tmp_path / "runs"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda _pid: alive
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)

    def patched_identity(_pid, _cmd, **kwargs):
        if identity[1] is not None and kwargs.get("_reason_out") is not None:
            kwargs["_reason_out"].append(identity[1])
        return identity[0]

    monkeypatch.setattr(kill_mod, "check_pid_identity", patched_identity)
    monkeypatch.setattr(kill_service, "check_pid_identity", patched_identity)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    run_dir = runs_root / sid
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(manifest_text)
    async with StateDB(path) as db:
        if via_import:
            from lionagi.cli.state import _import_one_run

            await _import_one_run(db, sid, run_dir, json.loads(manifest_text))
        else:
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
    return path, sid


@pytest.mark.parametrize(
    ("manifest_text", "alive", "identity", "expected_status", "expected_counter"),
    [
        pytest.param(
            _imported_run_manifest(),
            True,
            ("ours", None),
            "running",
            "skipped_live_pid=1",
            id="live-process-ours",
        ),
        pytest.param(
            _imported_run_manifest(),
            True,
            ("unverifiable", "start_mismatch"),
            "running",
            "skipped_linux_start_mismatch=1",
            id="live-process-start-mismatch",
        ),
        pytest.param(
            _imported_run_manifest(),
            True,
            ("unverifiable", "permission_denied"),
            "running",
            "skipped_permission_denied=1",
            id="live-process-permission-denied",
        ),
        pytest.param(
            _imported_run_manifest(pid_boot_time=900.0),
            True,
            ("ours", None),
            "running",
            "skipped_linux_boot_disagreement=1",
            id="live-process-boot-disagreement",
        ),
        pytest.param(
            _imported_run_manifest(pid_create_time=None),
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="live-process-without-start-time",
        ),
        pytest.param(
            _imported_run_manifest(pid=None, pid_create_time=None),
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="no-recorded-process",
        ),
        pytest.param(
            '{"status":',
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="unreadable-manifest",
        ),
        pytest.param(
            "[]",
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="manifest-not-an-object",
        ),
        # A recorded pid that cannot be judged from this host keeps the run, whether or not a
        # process with that number is alive here.
        pytest.param(
            _imported_run_manifest(pid_host="another-host.invalid"),
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="pid-on-another-host",
        ),
        pytest.param(
            _imported_run_manifest(process_identity_mode="container"),
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="identity-mode-not-signallable",
        ),
        pytest.param(
            _imported_run_manifest(process_identity_mode="in_process"),
            True,
            ("ours", None),
            "running",
            "skipped_unverifiable_pid=1",
            id="in-process-host-alive",
        ),
        pytest.param(
            _imported_run_manifest(process_identity_mode="in_process"),
            False,
            ("ours", None),
            "cancelled",
            "skipped_unverifiable_pid=0",
            id="in-process-host-gone",
        ),
        pytest.param(
            _imported_run_manifest(),
            True,
            ("not_ours", None),
            "cancelled",
            "skipped_unverifiable_pid=0",
            id="live-process-not-ours",
        ),
        pytest.param(
            _imported_run_manifest(),
            False,
            ("ours", None),
            "cancelled",
            "skipped_unverifiable_pid=0",
            id="dead-process",
        ),
        pytest.param(
            _imported_run_manifest(status="completed"),
            True,
            ("ours", None),
            "cancelled",
            "skipped_unverifiable_pid=0",
            id="finished-run",
        ),
    ],
)
@pytest.mark.asyncio
async def test_sweep_judges_imported_run_by_its_recorded_process(
    tmp_path,
    monkeypatch,
    capsys,
    manifest_text,
    alive,
    identity,
    expected_status,
    expected_counter,
):
    path, sid = await _seed_stale_imported_session(
        tmp_path, monkeypatch, manifest_text=manifest_text, alive=alive, identity=identity
    )
    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
    assert expected_counter in capsys.readouterr().out
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == expected_status


@pytest.mark.parametrize("kind", ["traversal", "linked"])
def test_imported_manifest_outside_its_run_directory_is_not_ours(tmp_path, monkeypatch, kind):
    from lionagi.cli import _runs

    root, outside = tmp_path / "runs", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "run.json").write_text(_imported_run_manifest(pid=None))
    if kind == "linked":
        (root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(_runs, "RUNS_ROOT", root)
    run_id = "../outside" if kind == "traversal" else "linked"
    assert kill_mod._imported_degraded_process_identity(run_id) == ("not_ours", None)


@pytest.mark.asyncio
async def test_sweep_probes_the_process_of_an_imported_running_run(tmp_path, monkeypatch, capsys):
    """A running manifest without the degraded marker is still judged by its recorded process."""
    path, sid = await _seed_stale_imported_session(
        tmp_path,
        monkeypatch,
        manifest_text=_imported_run_manifest(started_at=time.time() - 7200),
        alive=True,
        identity=("ours", None),
        via_import=True,
    )
    probed: list[int] = []

    def alive_spy(pid):
        probed.append(pid)
        return True

    monkeypatch.setattr(kill_mod, "_pid_alive", alive_spy)
    monkeypatch.setattr(kill_service, "_pid_alive", alive_spy)

    assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0

    assert set(probed) == {4242}
    assert "skipped_live_pid=1" in capsys.readouterr().out
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == "running"


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
    patched_pid_alive = lambda _pid: False
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
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


@pytest.mark.parametrize(
    ("alive", "expected_status"),
    [(False, "aborted"), (True, "running")],
    ids=["dead-pid", "live-pid"],
)
@pytest.mark.asyncio
async def test_doctor_boot_disagreement_checks_pid_liveness(
    tmp_path, monkeypatch, alive, expected_status
):
    from lionagi.cli import _util, state

    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(_util, "pid_alive", lambda pid: alive and pid == 4242)
    patched_pid_alive = lambda pid: alive and pid == 4242
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service.os, "kill", lambda *args: pytest.fail("unexpected OS signal"))
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 1006.0)
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
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
                    "pid_create_time": 1005.0,
                    "pid_boot_time": 900.0,
                },
            }
        )
    result = await state._doctor(stale_hours=1, dry_run=False)
    assert result["swept"] == (0 if alive else 1)
    async with StateDB(path) as db:
        assert (await db.get_session(sid))["status"] == expected_status


@pytest.mark.asyncio
async def test_direct_kill_dead_pid_keeps_non_linux_boot_refusal_path(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda _pid: False
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(
        kill_service, "_terminate_pid", lambda *a, **kw: pytest.fail("unexpected signal")
    )
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    async with StateDB(path) as db:
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "node_metadata": {"pid": 4242, "pid_boot_time": 900.0},
            }
        )
        result = await kill_mod.kill_one(
            db, "session", sid, await db.get_session(sid), user_reason=""
        )
        assert result["signal"] == "boot_mismatch"
        assert (await db.get_session(sid))["status"] == "running"


@pytest.mark.asyncio
async def test_direct_kill_refuses_foreign_host_before_parsing_infinite_pid(monkeypatch):
    monkeypatch.setattr(_proc.socket, "gethostname", lambda: "local-host")
    patched_pid_alive = lambda _pid: pytest.fail("unexpected pid probe")
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(
        kill_service, "_terminate_pid", lambda *a, **kw: pytest.fail("unexpected signal")
    )
    warnings = []
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    row = {
        "id": "foreign-session",
        "status": "running",
        "node_metadata": {"pid": float("inf"), "pid_host": "other-host"},
    }
    result = await kill_mod.kill_one(object(), "session", row["id"], row, user_reason="")
    assert result["signal"] == "host_mismatch"
    assert result["pid"] is None
    assert any("different host" in message for message in warnings)


@pytest.mark.parametrize("alive", [False, True], ids=["dead-pid", "live-pid"])
@pytest.mark.asyncio
async def test_recorded_worker_cleanup_distinguishes_dead_and_live_boot_disagreement(
    monkeypatch, alive
):
    from tests.cli.test_kill_recorded_workers import _CleanupDB

    db = _CleanupDB(status="running")
    db.session["node_metadata"]["pid_boot_time"] = 900.0
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    probes = []

    def pid_alive(pid):
        probes.append(pid)
        return alive

    monkeypatch.setattr(kill_mod, "_pid_alive", pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", pid_alive)
    monkeypatch.setattr(
        kill_service,
        "_runner_process_create_time",
        lambda _pid: pytest.fail("unexpected runner identity probe"),
    )
    warnings = []
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    monkeypatch.setattr(kill_service.os, "kill", lambda *args: pytest.fail("unexpected signal"))
    monkeypatch.setattr(
        kill_service.os, "killpg", lambda *args: pytest.fail("unexpected group signal")
    )
    result = await kill_mod._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert db.cancelled == [] and db.settled == []
    assert probes == [76543]
    unhandled = [message for message in warnings if "not handled" in message]
    assert bool(unhandled) is alive


@pytest.mark.parametrize("pid", [float("inf"), 0, "invalid"], ids=["inf", "zero", "non-int"])
@pytest.mark.asyncio
async def test_recorded_workers_invalid_pid_and_boot_disagreement_keep_legacy_refusal(
    monkeypatch, pid
):
    from tests.cli.test_kill_recorded_workers import _CleanupDB

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": pid, "pid_boot_time": 900.0}
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000.0)
    patched_pid_alive = lambda _pid: pytest.fail("unexpected pid probe")
    monkeypatch.setattr(kill_mod, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(kill_service, "_pid_alive", patched_pid_alive)
    monkeypatch.setattr(
        kill_service,
        "_runner_process_create_time",
        lambda _pid: pytest.fail("unexpected runner identity probe"),
    )
    monkeypatch.setattr(kill_service.os, "kill", lambda *args: pytest.fail("unexpected signal"))
    monkeypatch.setattr(
        kill_service.os, "killpg", lambda *args: pytest.fail("unexpected group signal")
    )
    warnings = []
    monkeypatch.setattr(kill_mod, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    result = await kill_mod._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert db.cancelled == [] and db.settled == []
    assert not any("could not be confirmed stopped" in message for message in warnings)


def test_unset_magicmock_linux_flag_keeps_non_linux_semantics(monkeypatch):
    fake_psutil = MagicMock()
    fake_psutil.boot_time.return_value = 1000.0
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    fake_psutil.ZombieProcess = type("ZombieProcess", (fake_psutil.NoSuchProcess,), {})
    monkeypatch.setattr(kill_service, "psutil", fake_psutil)
    monkeypatch.setattr(kill_service, "raw_create_time", lambda _proc: 102.0)
    assert kill_mod.unaddressable_pid_reason({"pid_boot_time": 900.0}) == "boot_mismatch"
    assert kill_mod.check_pid_identity(4242, "lionagi", expected_create_time=100.0) == "not_ours"
