# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Kill-service platform identity and recorded-worker decisions."

import signal
import uuid
from unittest.mock import MagicMock

import psutil
import pytest

import lionhub.orchestration.kill as kill_service
from lionagi.cli import kill as kill_mod
from lionagi.ln import _proc
from lionagi.state.db import StateDB
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._kill_linux_boot_drift_test_support import (
    _imported_run_manifest as _imported_run_manifest,
)


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
    from lionhub.orchestration import runs as _runs

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


@pytest.mark.parametrize("kind", ["traversal", "linked"])
def test_imported_manifest_outside_its_run_directory_is_not_ours(tmp_path, monkeypatch, kind):
    from lionhub.orchestration import runs as _runs

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
