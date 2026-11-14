"""A well-formed quarantine from another host is not an unreadable record."""

from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock, Mock

import pytest

from lionhub.serve.cli import kill as kill_cli
from lionhub.serve.cli import monitor

from .quarantine_helpers import (  # noqa: F401
    GENERATION,
    fetch_row,
    isolated_store,
    seed,
)


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("surface", ["table", "detail", "sweep"])
@pytest.mark.parametrize(
    "foreign, count, mode, unreadable",
    [
        (True, 3, "local", False),
        (True, 3, None, False),
        (True, 1, "local", True),
        (False, 1, "local", True),
    ],
    ids=["foreign-valid", "foreign-valid-legacy-mode", "foreign-malformed", "local-malformed"],
)
async def test_foreign_scope_is_not_a_corrupt_quarantine(
    kind, surface, foreign, count, mode, unreadable, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    host = socket.gethostname()
    recorded_host = f"{host}.other-host.invalid" if foreign else host
    generation = GENERATION if kind == "session" else None
    private = "private-metadata-not-for-output"
    metadata = {
        "pid": 123,
        "pid_create_time": 42.0,
        "pid_host": recorded_host,
        "unverifiable_identity": json.dumps([123, 42.0, generation], separators=(",", ":")),
        "unverifiable_count": count,
        "unverifiable_epoch": 0,
        "unverifiable_quarantined": True,
        "private_detail": private,
    }
    if mode is not None:
        metadata["process_identity_mode"] = mode
    entity_id = await seed(kind, metadata=metadata)
    before = await fetch_row(kind, entity_id)
    signal = Mock(side_effect=AssertionError("must not signal"))
    monkeypatch.setattr("lionhub.orchestration.kill.os.kill", signal)
    monkeypatch.setattr(kill_cli, "check_pid_identity", lambda *_args, **_kwargs: "unverifiable")

    assert monitor._is_unverifiable_quarantined(before) is False
    capsys.readouterr()
    caplog.clear()
    output = ""
    if surface == "table":
        output = await monitor._run_table(since=None, entity_type=kind, project=None)
    elif surface == "detail":
        output = await monitor._run_detail(entity_id)
    else:
        assert await kill_cli._do_kill_all_stale(threshold_seconds=3600, dry_run=True) == 0
    captured = capsys.readouterr()
    output += captured.out + "\n" + captured.err + "\n" + caplog.text
    assert ("quarantine record unreadable" in output) is unreadable
    assert private not in output
    assert recorded_host not in output
    assert await fetch_row(kind, entity_id) == before
    signal.assert_not_called()
