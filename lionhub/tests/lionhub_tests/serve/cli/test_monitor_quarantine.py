# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Process-identity quarantine is visible without changing lifecycle status."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionhub.serve.cli import monitor


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


@pytest.mark.asyncio
async def test_quarantine_visible_for_session_invocation_and_play_session(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "_stdout_is_tty", lambda: False)
    now = time.time()
    metadata = {
        "pid": 123,
        "pid_create_time": 42.0,
        "unverifiable_identity": "[123,42.0,null]",
        "unverifiable_quarantined": True,
        "unverifiable_count": 3,
        "unverifiable_epoch": 0,
    }
    async with StateDB() as db:
        for session_id in ("session-direct", "session-for-play", "session-normal"):
            progression_id = f"progression-{session_id}"
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": progression_id,
                    "status": "running",
                    "started_at": now,
                    "invocation_kind": "agent",
                    "node_metadata": metadata if session_id != "session-normal" else {},
                }
            )
        await db.create_invocation(
            {
                "id": "invocation-quarantine",
                "skill": "test",
                "status": "running",
                "started_at": now,
                "node_metadata": metadata,
            }
        )
        await db.create_show({"id": "show-id", "topic": "test", "show_dir": "/tmp/show"})
        await db.create_play(
            {
                "id": "play-id",
                "show_id": "show-id",
                "name": "play",
                "status": "running",
                "session_id": "session-for-play",
                "started_at": now,
            }
        )

        entities = await monitor._gather_entities(db, since=None, entity_type=None, project=None)
        rows = [monitor._ROW_BUILDERS[kind](row) for kind, row in entities]
        rendered = monitor._format_table(rows)
        recent = await monitor._gather_entities(db, since=now - 60, entity_type=None, project=None)
        future = await monitor._gather_entities(db, since=now + 60, entity_type=None, project=None)

    by_id = {row["id"]: (kind, row) for kind, row in entities}
    assert {"session-direct", "session-for-play", "session-normal", "play-id"} <= set(by_id)
    assert "invocation-quarantine" in by_id
    for entity_id in ("session-direct", "session-for-play", "invocation-quarantine"):
        kind, row = by_id[entity_id]
        assert row["status"] == "running"
        assert monitor._machine_entity(kind, row)["unverifiable_quarantined"] is True
        assert entity_id[:16] in rendered.split("identity unverified, quarantined:", 1)[1]
    assert monitor._machine_entity(*by_id["session-normal"])["unverifiable_quarantined"] is False
    assert {"session-direct", "session-for-play", "invocation-quarantine"} <= {
        row["id"] for _, row in recent
    }
    assert not future


@pytest.mark.parametrize("metadata", [None, "bad json", "[]", {}, {"unverifiable_quarantined": 1}])
def test_only_boolean_true_is_a_quarantine_marker(metadata) -> None:
    row = {"id": "sample", "status": "running", "node_metadata": metadata}
    assert monitor._session_to_row(row)["unverifiable_quarantined"] is False
    assert monitor._machine_entity("session", row)["unverifiable_quarantined"] is False


def test_old_terminal_metadata_is_not_a_current_quarantine() -> None:
    row = {
        "id": "sample",
        "status": "cancelled",
        "node_metadata": json.dumps({"unverifiable_quarantined": True}),
    }
    assert monitor._invocation_to_row(row)["status"] == "cancelled"
    assert monitor._machine_entity("invocation", row)["unverifiable_quarantined"] is False
    assert "identity unverified" not in monitor._format_table([monitor._invocation_to_row(row)])


@pytest.mark.parametrize(
    ("pid", "created", "generation"),
    [(124, 42.0, "g1"), (123, 43.0, "g1"), (123, 42.0, "g2")],
)
def test_changed_process_identity_suppresses_old_quarantine(pid, created, generation) -> None:
    row = {
        "id": "sample",
        "status": "running",
        "cli_worker_generation": generation,
        "node_metadata": {
            "pid": pid,
            "pid_create_time": created,
            "unverifiable_identity": '[123,42.0,"g1"]',
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }
    table_row = monitor._session_to_row(row)
    assert table_row["status"] == "running"
    assert table_row["unverifiable_quarantined"] is False
    assert monitor._machine_entity("session", row)["unverifiable_quarantined"] is False
    assert "identity unverified" not in monitor._format_table([table_row])


def test_missing_creation_time_can_match_current_pid_and_generation() -> None:
    row = {
        "id": "sample",
        "status": "running",
        "cli_worker_generation": "g1",
        "node_metadata": {
            "pid": "123",
            "unverifiable_identity": '[123,null,"g1"]',
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }
    assert monitor._session_to_row(row)["unverifiable_quarantined"] is True


def test_file_only_pid_is_not_assumed_current() -> None:
    row = {
        "id": "sample",
        "status": "running",
        "node_metadata": {
            "unverifiable_identity": "[123,null,null]",
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }
    assert monitor._session_to_row(row)["unverifiable_quarantined"] is False


def test_stale_quarantine_is_labeled_separately() -> None:
    row = {
        "id": "sample",
        "status": "running",
        "node_metadata": {
            "pid": 124,
            "pid_create_time": 42.0,
            "unverifiable_identity": "[123,42.0,null]",
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }

    table_row = monitor._session_to_row(row)
    assert table_row["_quarantine_label"] == "quarantine record stale"
    rendered = monitor._format_table([table_row])
    assert "quarantine record stale: session sample" in rendered
    assert "unreadable" not in rendered


def test_host_probe_failure_is_labeled_as_a_probe_failure(monkeypatch) -> None:
    import socket

    def fail():
        raise OSError("private")

    row = {
        "id": "sample",
        "status": "running",
        "node_metadata": {
            "pid": 123,
            "pid_create_time": 42.0,
            "pid_host": "host",
            "unverifiable_identity": "[123,42.0,null]",
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }
    monkeypatch.setattr(socket, "gethostname", fail)
    rendered = monitor._format_table([monitor._invocation_to_row(row)])
    assert "quarantine host probe failed: invocation sample" in rendered
    assert "unreadable" not in rendered


@pytest.mark.parametrize("identity", [None, "not-json", "[true,42.0,null]", "[123,42.0]"])
def test_invalid_identity_never_displays_quarantine(identity) -> None:
    row = {
        "id": "sample",
        "status": "running",
        "node_metadata": {
            "pid": 123,
            "pid_create_time": 42.0,
            "unverifiable_identity": identity,
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
        },
    }
    assert monitor._session_to_row(row)["unverifiable_quarantined"] is False


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"pid_host": "another-host"}, False),
        ({"process_identity_mode": "in_process"}, False),
        ({"process_identity_mode": "external"}, False),
        ({"pid_host": "test-host", "process_identity_mode": "local"}, True),
        ({}, True),
    ],
)
def test_quarantine_rechecks_local_scope_without_boot_probe(
    scope, expected, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "test-host")

    def refuse_boot_probe():
        raise AssertionError("monitor must not probe boot time")

    monkeypatch.setattr("lionhub.orchestration.kill.psutil.boot_time", refuse_boot_probe)
    row = {
        "id": "sample",
        "status": "running",
        "node_metadata": {
            "pid": 123,
            "pid_create_time": 42.0,
            "pid_boot_time": 100.0,
            "unverifiable_identity": "[123,42.0,null]",
            "unverifiable_quarantined": True,
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
            **scope,
        },
    }
    assert monitor._session_to_row(row)["unverifiable_quarantined"] is expected
    assert monitor._machine_entity("session", row)["unverifiable_quarantined"] is expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"unverifiable_count": 2},
        {"unverifiable_count": True},
        {"unverifiable_count": None},
        {"unverifiable_epoch": -1},
        {"unverifiable_epoch": True},
        {"unverifiable_epoch": None},
    ],
)
def test_invalid_observation_history_is_not_displayed_as_quarantined(overrides):
    metadata = {
        "pid": 123,
        "unverifiable_identity": "[123,null,null]",
        "unverifiable_quarantined": True,
        "unverifiable_count": 3,
        "unverifiable_epoch": 0,
        **overrides,
    }
    row = {"id": "sample", "status": "running", "node_metadata": metadata}
    assert monitor._machine_entity("session", row)["unverifiable_quarantined"] is False
