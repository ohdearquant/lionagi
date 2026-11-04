"""Live process observations apply across sessions and invocations."""

from __future__ import annotations

import socket

import pytest

from lionagi.cli import kill as kill_cli
from tests.cli.test_quarantine_sweep import (
    NEXT_GENERATION,
    _row,
    _seed,
    _sweep,
    isolated_store,  # noqa: F401
)


def _probe_session_live(monkeypatch):
    def identity(_pid, _name, *, expected_session_id=None, _reason_out=None, **_kwargs):
        if expected_session_id is not None:
            return "ours"
        if _reason_out is not None:
            _reason_out.append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)


async def test_live_session_suppresses_and_clears_matching_invocation(monkeypatch):
    old_invocation = await _seed("invocation")
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    before = (await _row("invocation", old_invocation))["node_metadata"]
    assert before["unverifiable_quarantined"] is True
    await _seed("session")
    fresh_invocation = await _seed("invocation")
    for _ in range(3):
        _probe_session_live(monkeypatch)
        assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    old = await _row("invocation", old_invocation)
    fresh = await _row("invocation", fresh_invocation)
    assert old["status"] == fresh["status"] == "running"
    assert "unverifiable_count" not in old["node_metadata"], (
        "live process must clear old quarantine"
    )
    assert old["node_metadata"]["unverifiable_epoch"] != before["unverifiable_epoch"]
    assert "unverifiable_count" not in fresh["node_metadata"], (
        "live process must suppress observations"
    )


@pytest.mark.parametrize("difference", ["create_time", "host", "boot"])
async def test_live_session_does_not_clear_invocation_with_different_evidence(
    difference, monkeypatch
):
    invocation = await _seed("invocation")
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    meta = {"pid": 54321, "pid_create_time": 100.0}
    if difference == "create_time":
        meta["pid_create_time"] = 101.0
    elif difference == "host":
        meta["pid_host"] = socket.gethostname()
    else:
        from lionagi.cli import _kill_service

        monkeypatch.setattr(_kill_service.psutil, "boot_time", lambda: 2000.0)
        meta["pid_boot_time"] = 2000.0
    await _seed("session", metadata=meta)
    _probe_session_live(monkeypatch)
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    row = await _row("invocation", invocation)
    assert row["node_metadata"]["unverifiable_count"] == 4
    assert row["node_metadata"]["unverifiable_quarantined"] is True


async def test_live_session_does_not_override_different_session_generation(monkeypatch):
    unknown = await _seed("session")
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    live = await _seed("session", extras={"cli_worker_generation": NEXT_GENERATION})

    def identity(_pid, _name, *, expected_session_id=None, _reason_out=None, **_kwargs):
        if expected_session_id == live:
            return "ours"
        if _reason_out is not None:
            _reason_out.append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    row = await _row("session", unknown)
    assert row["node_metadata"]["unverifiable_count"] == 4
    assert row["node_metadata"]["unverifiable_quarantined"] is True


async def test_live_invocation_clears_matching_session(monkeypatch):
    session = await _seed("session")
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    await _seed("invocation")

    def identity(_pid, _name, *, expected_session_id=None, _reason_out=None, **_kwargs):
        if expected_session_id is None:
            return "ours"
        if _reason_out is not None:
            _reason_out.append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0
    row = await _row("session", session)
    assert row["status"] == "running"
    assert "unverifiable_count" not in row["node_metadata"]
