"""Monitor, sweep and resolution agree about current quarantine records."""

from __future__ import annotations

import json
import socket
import time
from unittest.mock import AsyncMock, Mock

import pytest

from lionagi.cli import kill as kill_cli
from lionagi.cli import monitor
from lionagi.state.db import StateDB
from tests.cli.quarantine_helpers import GENERATION, fetch_row, isolated_store, seed  # noqa: F401

_SECRET = "private-metadata-value-do-not-print"


def _metadata(kind: str, case: str) -> dict:
    generation = GENERATION if kind == "session" else None
    result = {
        "pid": 123,
        "pid_create_time": 42.0,
        "unverifiable_identity": json.dumps([123, 42.0, generation], separators=(",", ":")),
        "unverifiable_count": 3,
        "unverifiable_epoch": 0,
        "unverifiable_quarantined": True,
        "pid_host": socket.gethostname(),
        "process_identity_mode": "local",
        "private_detail": _SECRET,
    }
    if case == "noncanonical":
        result["unverifiable_identity"] = json.dumps([123, 42, generation], separators=(",", ":"))
    elif case == "low_count":
        result["unverifiable_count"] = 1
    elif case == "bool_count":
        result["unverifiable_count"] = True
    elif case == "negative_epoch":
        result["unverifiable_epoch"] = -1
    elif case == "bool_epoch":
        result["unverifiable_epoch"] = True
    elif case == "other_host":
        result["pid_host"] = "a-different-host"
    elif case == "other_mode":
        result["process_identity_mode"] = "remote"
    return result


_CASES = [
    "valid",
    "noncanonical",
    "low_count",
    "bool_count",
    "negative_epoch",
    "bool_epoch",
    "other_host",
    "other_mode",
]


def _assert_unreadable_diagnostic(output: str, kind: str, entity_id: str, length: int) -> None:
    lines = [line for line in output.splitlines() if "quarantine record unreadable" in line]
    assert lines, output
    assert _SECRET not in output
    for line in lines:
        prefix, suffix = line.split("quarantine record unreadable", 1)
        assert (suffix.strip() == f": {kind} {entity_id[:length]}" and not prefix.strip()) or (
            not suffix.strip() and prefix.rstrip().endswith(f"{kind} {entity_id[:length]}:")
        )
        assert _SECRET not in line
        assert "a-different-host" not in line
        assert "unverifiable_identity" not in line


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("case", _CASES)
async def test_current_quarantine_is_consistent_across_views_and_resolvers(
    kind, case, monkeypatch, capsys, caplog
):
    entity_id = await seed(kind, metadata=_metadata(kind, case))
    initial = await fetch_row(kind, entity_id)
    expected_valid = case == "valid"
    monkeypatch.setattr(monitor, "_stdout_is_tty", lambda: False)
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill_cli, "check_pid_identity", lambda *_args, **_kw: "unverifiable")
    signal = Mock(side_effect=AssertionError("resolution must not signal"))
    terminate = Mock(side_effect=AssertionError("resolution must not terminate"))
    cleanup = Mock(side_effect=AssertionError("resolution must not run worker cleanup"))
    monkeypatch.setattr("lionagi.cli._kill_service.os.kill", signal)
    monkeypatch.setattr("lionagi.cli._kill_service._terminate_pid", terminate)
    monkeypatch.setattr(kill_cli, "_try_recorded_worker_cleanup", cleanup)

    async with StateDB() as db:
        entities = await monitor._gather_entities(db, since=None, entity_type=None, project=None)
    selected = [(entity_kind, row) for entity_kind, row in entities if row["id"] == entity_id]
    assert len(selected) == 1
    entity_kind, row = selected[0]
    assert monitor._machine_entity(entity_kind, row)["unverifiable_quarantined"] is expected_valid
    rendered = await monitor._run_table(since=None, entity_type=None, project=None)
    monitor_output = rendered + "\n" + capsys.readouterr().out + "\n" + caplog.text
    assert _SECRET not in monitor_output
    if expected_valid:
        assert "identity unverified, quarantined" in rendered
        assert "quarantine record unreadable" not in monitor_output
    else:
        assert "identity unverified, quarantined" not in rendered
        _assert_unreadable_diagnostic(monitor_output, kind, entity_id, 16)

    caplog.clear()
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600, dry_run=True) == 0
    captured = capsys.readouterr()
    sweep_output = captured.out + "\n" + captured.err + "\n" + caplog.text
    assert _SECRET not in sweep_output
    if expected_valid:
        assert "quarantine record unreadable" not in sweep_output
        assert entity_id in sweep_output
    else:
        _assert_unreadable_diagnostic(sweep_output, kind, entity_id, 12)
    assert await fetch_row(kind, entity_id) == initial

    result = await kill_cli._resolve_quarantine(entity_id, target="failed", reason="reviewed")
    assert result == (0 if expected_valid else 1)
    resolved = await fetch_row(kind, entity_id)
    assert resolved["status"] == ("failed" if expected_valid else "running")
    if not expected_valid:
        assert resolved == initial
    signal.assert_not_called()
    terminate.assert_not_called()
    cleanup.assert_not_called()

    # Exercise the lifecycle guard independently of the CLI precheck.
    lifecycle_id = await seed(kind, metadata=_metadata(kind, case))
    async with StateDB() as db:
        lifecycle_row = await (
            db.get_session(lifecycle_id) if kind == "session" else db.get_invocation(lifecycle_id)
        )
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=lifecycle_id,
            snapshot=lifecycle_row,
            target_status="failed",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == ("applied" if expected_valid else "conflict")
    lifecycle_after = await fetch_row(kind, lifecycle_id)
    if not expected_valid:
        assert lifecycle_after == lifecycle_row


async def test_play_backed_session_with_unreadable_quarantine_remains_visible(
    monkeypatch, capsys, caplog
):
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    entity_id = await seed("session", metadata=_metadata("session", "low_count"))
    async with StateDB() as db:
        await db.create_show({"id": "show", "topic": "test", "show_dir": "/tmp/test"})
        await db.create_play(
            {
                "id": "play",
                "show_id": "show",
                "name": "play",
                "status": "running",
                "session_id": entity_id,
                "started_at": time.time(),
            }
        )
        entities = await monitor._gather_entities(db, since=None, entity_type=None, project=None)
    selected = [(kind, row) for kind, row in entities if row["id"] == entity_id]
    assert len(selected) == 1
    assert selected[0][0] == "session"
    rendered = await monitor._run_table(since=None, entity_type=None, project=None)
    output = rendered + "\n" + capsys.readouterr().out + "\n" + caplog.text
    _assert_unreadable_diagnostic(output, "session", entity_id, 16)
