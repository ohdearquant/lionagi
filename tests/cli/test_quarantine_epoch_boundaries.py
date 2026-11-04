"""Identity epochs and unbound fallback PID warnings at the sweep boundary."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from lionagi.cli import _kill_service
from lionagi.cli import kill as kill_cli
from lionagi.state.db import StateDB
from tests.cli.test_quarantine_sweep import _row, _seed, _sweep, isolated_store  # noqa: F401


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("observe_intervening_pid", [False, True])
async def test_pid_a_to_b_to_a_epoch_guards_old_disposition(
    kind, observe_intervening_pid, monkeypatch
):
    entity_id = await _seed(kind)
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    old = await _row(kind, entity_id)
    old_meta = old["node_metadata"]
    assert old_meta["unverifiable_quarantined"] is True

    async with StateDB() as db:
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid": 54322})
    if observe_intervening_pid:
        await _sweep(monkeypatch, ["unverifiable"])
        intervening = (await _row(kind, entity_id))["node_metadata"]
        assert intervening["unverifiable_epoch"] != old_meta["unverifiable_epoch"]
        assert intervening["unverifiable_count"] == 1
        assert intervening["unverifiable_quarantined"] is False
    async with StateDB() as db:
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid": 54321})

    if observe_intervening_pid:
        for _ in range(3):
            await _sweep(monkeypatch, ["unverifiable"])
        current = (await _row(kind, entity_id))["node_metadata"]
        assert current["unverifiable_quarantined"] is True
        assert current["unverifiable_epoch"] != old_meta["unverifiable_epoch"]

    async with StateDB() as db:
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=old,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        # A -> B -> A with no B observation leaves the same stored identity and
        # epoch; the guard cannot infer the unobserved change and accepts it.
        assert outcome.result == ("conflict" if observe_intervening_pid else "applied")
    row = await _row(kind, entity_id)
    assert row["status"] == ("running" if observe_intervening_pid else "cancelled")


@pytest.mark.parametrize("fallback", ["pid_file", "run_json"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_unbound_fallback_warns_without_tracking_even_in_dry_run(
    fallback, dry_run, monkeypatch, tmp_path, capsys
):
    warnings = []
    monkeypatch.setattr(kill_cli, "warn", warnings.append)
    no_signal = Mock(side_effect=AssertionError("unexpected signal"))
    monkeypatch.setattr(_kill_service.os, "kill", no_signal)
    if fallback == "pid_file":
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / ".pid").write_text("54321", encoding="utf-8")
        entity_id = await _seed(metadata={}, extras={"artifacts_path": str(artifact_dir)})
        verdicts = ["unverifiable"]
    else:
        manifest = tmp_path / "run.json"
        manifest.write_text(json.dumps({"status": "running", "pid": 54321}))
        entity_id = await _seed(metadata={}, extras={"source_kind": "imported_fs"})
        monkeypatch.setattr(_kill_service, "_run_manifest_path", lambda _id: manifest)
        monkeypatch.setattr(_kill_service, "_pid_alive", lambda _pid: True)
        verdicts = []
    before = await _row("session", entity_id)
    for _ in range(3):
        await _sweep(monkeypatch, verdicts, dry_run=dry_run)
    after = await _row("session", entity_id)
    assert after["status"] == "running"
    assert after["node_metadata"] == before["node_metadata"]
    assert "unverifiable_count" not in after["node_metadata"]
    assert len(warnings) == 3
    assert all("process identity not bound in the row" in w for w in warnings)
    assert capsys.readouterr().out.count("quarantined identity-unverified rows (0)") == 3
    no_signal.assert_not_called()


@pytest.mark.parametrize("same_boot", [False, True])
async def test_live_peer_only_clears_quarantine_with_equivalent_boot_evidence(
    same_boot, monkeypatch
):
    """A live peer cannot decide a row whose recorded Linux boot disagrees."""
    monkeypatch.setattr(_kill_service.psutil, "LINUX", True)
    monkeypatch.setattr(_kill_service.psutil, "boot_time", lambda: 2000.0)
    monkeypatch.setattr(_kill_service, "_pid_alive", lambda _pid: True)
    unknown_boot = 2000.0 if same_boot else 1000.0
    unknown = await _seed(
        metadata={"pid": 54321, "pid_create_time": 100.0, "pid_boot_time": unknown_boot}
    )
    live = None

    def identity(_pid, _name, *, expected_session_id=None, _reason_out=None, **_kwargs):
        if expected_session_id == live:
            return "ours"
        if _reason_out is not None:
            _reason_out.append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)
    for _ in range(3):
        assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    before = (await _row("session", unknown))["node_metadata"]
    assert before["unverifiable_count"] == 3
    assert before["unverifiable_quarantined"] is True
    live = await _seed(metadata={"pid": 54321, "pid_create_time": 100.0, "pid_boot_time": 2000.0})
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    after = (await _row("session", unknown))["node_metadata"]
    if same_boot:
        assert "unverifiable_count" not in after
        assert after.get("unverifiable_quarantined") is not True
        assert after["unverifiable_epoch"] != before["unverifiable_epoch"]
    else:
        assert after["unverifiable_count"] == 4
        assert after["unverifiable_quarantined"] is True
        assert after["unverifiable_epoch"] == before["unverifiable_epoch"]


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_sweep_sees_stale_writer_restoring_old_pid_and_resets_quarantine(kind, monkeypatch):
    entity_id = await _seed(kind)
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    old = await _row(kind, entity_id)
    assert old["node_metadata"]["unverifiable_quarantined"] is True
    async with StateDB() as db:
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid": 54322})
    await _sweep(monkeypatch, ["unverifiable"])
    interim = (await _row(kind, entity_id))["node_metadata"]
    assert interim["unverifiable_count"] == 1
    assert interim["unverifiable_quarantined"] is False
    assert interim["unverifiable_epoch"] != old["node_metadata"]["unverifiable_epoch"]
    # An old writer restores the PID it captured, but not the newer epoch fields.
    async with StateDB() as db:
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid": old["node_metadata"]["pid"]})
    await _sweep(monkeypatch, ["unverifiable"])
    current = (await _row(kind, entity_id))["node_metadata"]
    assert current["unverifiable_count"] == 1
    assert current["unverifiable_quarantined"] is False
    assert current["unverifiable_epoch"] != interim["unverifiable_epoch"]
    async with StateDB() as db:
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=old,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
    assert outcome.result == "conflict"


@pytest.mark.parametrize("extra", ["pid_host", "process_identity_mode"])
async def test_live_peer_with_different_host_or_mode_evidence_cannot_clear(extra, monkeypatch):
    import socket

    unknown = await _seed()
    live = None

    def identity(_pid, _name, *, expected_session_id=None, _reason_out=None, **_kwargs):
        if expected_session_id == live:
            return "ours"
        if _reason_out is not None:
            _reason_out.append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)
    for _ in range(3):
        assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    before = (await _row("session", unknown))["node_metadata"]
    assert before["unverifiable_quarantined"] is True
    value = socket.gethostname() if extra == "pid_host" else "local"
    live = await _seed(metadata={"pid": 54321, "pid_create_time": 100.0, extra: value})
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    after = (await _row("session", unknown))["node_metadata"]
    assert after["unverifiable_count"] == 4
    assert after["unverifiable_quarantined"] is True
    assert after["unverifiable_epoch"] == before["unverifiable_epoch"]
