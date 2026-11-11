# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the flow_yaml schedule action kind: build_argv, validation, DB persistence, and lifecycle."""

from __future__ import annotations

import os
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import lionagi.state.db as state_db_mod

# subprocess.build_argv tests


def _minimal_schedule(**kwargs) -> dict:
    base = {
        "id": "sched-abc",
        "name": "test-sched",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_model": "",
        "action_prompt": "",
        "action_flow_yaml": "prompt: hello world\n",
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(kwargs)
    return base


# Creation-time YAML validation tests


def test_validate_flow_yaml_spec_accepts_valid_spec():
    """A valid flow spec returns None (no error)."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    yaml_text = textwrap.dedent("""\
        prompt: Run a quick summary
        workers: 2
        effort: medium
    """)
    assert _validate_flow_yaml_spec(yaml_text) is None


def test_validate_flow_yaml_spec_rejects_invalid_yaml():
    """Malformed YAML is rejected with a clear error."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec("key: [unclosed")
    assert result is not None
    assert "not valid YAML" in result


def test_validate_flow_yaml_spec_rejects_non_mapping():
    """A YAML list is rejected because a flow spec must be a mapping."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec("- item1\n- item2\n")
    assert result is not None
    assert "mapping" in result or "dict" in result


def test_validate_flow_yaml_spec_rejects_invalid_field():
    """Out-of-range spec field is rejected at validation time."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    # workers > 32 is invalid
    result = _validate_flow_yaml_spec("workers: 999\n")
    assert result is not None
    assert "workers" in result


@pytest.mark.parametrize(
    "yaml_text",
    [
        'bare: "true"\n',
        "prompt: 42\n",
        "save: 7\n",
        "model: 42\n",
        "artifacts: null\n",
    ],
)
def test_validate_flow_yaml_spec_rejects_fields_execution_rejects(yaml_text):
    """Fields the CLI execution validator rejects must also fail Studio validation."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec(yaml_text)
    assert result is not None, f"{yaml_text!r} should be rejected"


def test_validate_flow_yaml_spec_rejects_non_string_key():
    """A YAML mapping with a non-string key must return an error, not raise TypeError."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec("1: value\n")
    assert result is not None
    assert "string" in result


def test_create_schedule_rejects_empty_flow_yaml():
    """create_schedule raises ValueError when action_flow_yaml is missing."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "bad-sched",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "flow_yaml",
        # action_flow_yaml intentionally absent
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.run(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "action_flow_yaml" in str(exc)


def test_create_schedule_rejects_malformed_flow_yaml():
    """create_schedule raises ValueError on malformed YAML spec."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "bad-yaml-sched",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "flow_yaml",
        "action_flow_yaml": "key: [unclosed",
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.run(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "flow_yaml" in str(exc).lower() or "YAML" in str(exc)


def _minimal_command_schedule(name: str) -> dict:
    return {
        "name": name,
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "say hi",
    }


def test_create_schedule_route_duplicate_name_returns_409(tmp_path, monkeypatch):
    """Creating a schedule with a name that already exists must return 409,
    not a generic server error."""
    import lionagi.studio.services.schedules as schedules_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    body = _minimal_command_schedule("dup-schedule")
    r1 = client.post("/api/schedules/", json=body)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/api/schedules/", json=body)
    assert r2.status_code == 409


def test_create_schedule_route_storage_failure_is_not_409(tmp_path, monkeypatch):
    """A non-conflict exception from the storage layer (e.g. RuntimeError)
    must not be reported to the client as a 409 name conflict."""
    import lionagi.studio.services.schedules as schedules_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    async def _boom(self, schedule):
        raise RuntimeError("storage offline")

    monkeypatch.setattr(state_db_mod.StateDB, "create_schedule", _boom)

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    client = TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")
    r = client.post("/api/schedules/", json=_minimal_command_schedule("will-fail-schedule"))
    assert r.status_code != 409
    assert r.status_code == 500


# Lifecycle / status parity test


# CLI parser tests


def test_cli_flow_yaml_choice_accepted():
    """--action-kind flow_yaml is a recognized choice."""
    import argparse

    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "create", "my-sched", "--action-kind", "flow_yaml"])
    assert args.action_kind == "flow_yaml"


# Persistence round-trip test


def test_flow_yaml_db_roundtrip():
    """action_flow_yaml is persisted by create_schedule and survives get_schedule."""
    import asyncio
    import tempfile
    import uuid

    from lionagi.state.db import StateDB

    yaml_spec = "prompt: round-trip check\nworkers: 1\n"
    schedule_id = str(uuid.uuid4())

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            async with StateDB(db_path) as db:
                await db.create_schedule(
                    {
                        "id": schedule_id,
                        "name": "rt-test",
                        "trigger_type": "cron",
                        "action_kind": "flow_yaml",
                        "action_flow_yaml": yaml_spec,
                    }
                )
                row = await db.get_schedule(schedule_id)
                assert row is not None, "schedule not found after create"
                assert row["action_flow_yaml"] == yaml_spec, (
                    f"action_flow_yaml lost on INSERT: got {row['action_flow_yaml']!r}"
                )

                # UPDATE path
                updated_spec = "prompt: updated spec\nworkers: 2\n"
                await db.update_schedule(schedule_id, action_flow_yaml=updated_spec)
                row2 = await db.get_schedule(schedule_id)
                assert row2 is not None
                assert row2["action_flow_yaml"] == updated_spec, (
                    f"action_flow_yaml lost on UPDATE: got {row2['action_flow_yaml']!r}"
                )
        finally:
            import os

            os.unlink(db_path)

    asyncio.run(_run())


# Regression tests: tmp-file lifecycle under cancellation / exception


# Regression tests: legacy schedules table migration


def test_legacy_schedules_table_upgraded_and_flow_yaml_insert_succeeds():
    """Old schedules table (4-value CHECK, no action_flow_yaml) is upgraded by StateDB on open."""
    import asyncio
    import tempfile
    import uuid

    import aiosqlite

    from lionagi.state.db import StateDB

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Build a pre-PR schedules table: old 4-value CHECK, no action_flow_yaml
            async with aiosqlite.connect(db_path) as raw:
                raw.row_factory = aiosqlite.Row
                await raw.execute("""
                    CREATE TABLE schedules (
                        id           TEXT PRIMARY KEY,
                        name         TEXT NOT NULL UNIQUE,
                        trigger_type TEXT NOT NULL,
                        action_kind  TEXT NOT NULL
                            CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play')),
                        created_at   REAL NOT NULL,
                        updated_at   REAL NOT NULL
                    )
                """)
                await raw.commit()

            # Open through StateDB — _reconcile_columns and
            # _drop_legacy_action_kind_check should fire and upgrade the table
            async with StateDB(db_path) as db:
                schedule_id = uuid.uuid4().hex[:12]
                await db.create_schedule(
                    {
                        "id": schedule_id,
                        "name": "legacy-upgrade-test",
                        "trigger_type": "cron",
                        "action_kind": "flow_yaml",
                        "action_flow_yaml": "prompt: legacy upgrade test\n",
                    }
                )
                row = await db.get_schedule(schedule_id)

            assert row is not None, "schedule not found after legacy-table upgrade"
            assert row["action_flow_yaml"] == "prompt: legacy upgrade test\n"
            assert row["action_kind"] == "flow_yaml"
        finally:
            os.unlink(db_path)

    asyncio.run(_run())


def test_legacy_rebuild_yields_full_canonical_columns():
    """Legacy-table rebuild must reproduce the FULL schema_meta column set.

    Guards the drift risk the old hand-written ``schedules_new`` literal carried:
    the rebuild DDL is now derived from ``schema_meta.schedules``, so a legacy
    table (missing later-added columns) must come out of ``StateDB`` open with
    every canonical column — including the self-health poller columns — and a
    ``github_poll`` schedule carrying those columns must roundtrip.
    """
    import asyncio
    import tempfile
    import uuid

    import aiosqlite

    from lionagi.state.db import StateDB
    from lionagi.state.schema_meta import schedules as schedules_table

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Pre-PR minimal schedules table: old CHECK, none of the columns
            # added after the original schema (no self-health columns).
            async with aiosqlite.connect(db_path) as raw:
                await raw.execute("""
                    CREATE TABLE schedules (
                        id           TEXT PRIMARY KEY,
                        name         TEXT NOT NULL UNIQUE,
                        trigger_type TEXT NOT NULL,
                        action_kind  TEXT NOT NULL
                            CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play')),
                        github_repo  TEXT,
                        created_at   REAL NOT NULL,
                        updated_at   REAL NOT NULL
                    )
                """)
                await raw.commit()

            async with StateDB(db_path) as db:
                async with db._engine.connect() as conn:
                    from sqlalchemy import text as _text

                    rows = (
                        (await conn.execute(_text("PRAGMA table_info(schedules)"))).mappings().all()
                    )
                got = {r["name"] for r in rows}
                expected = {c.name for c in schedules_table.columns}
                assert got == expected, (
                    f"rebuilt table drifted from schema_meta; "
                    f"missing={expected - got} extra={got - expected}"
                )

                # A github_poll schedule using the self-health columns roundtrips.
                sid = uuid.uuid4().hex[:12]
                await db.create_schedule(
                    {
                        "id": sid,
                        "name": "poller-health-roundtrip",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "github_repo": "owner/repo",
                    }
                )
                await db.update_schedule(sid, last_healthy_poll_at=123.0, poller_consecutive_401=2)
                row = await db.get_schedule(sid)
            assert row is not None
            assert row["last_healthy_poll_at"] == 123.0
            assert row["poller_consecutive_401"] == 2
        finally:
            os.unlink(db_path)

    asyncio.run(_run())


# Regression tests: PATCH validation


def test_update_schedule_rejects_patch_to_flow_yaml_without_yaml():
    """PATCH action_kind=flow_yaml with no action_flow_yaml raises ValueError (before DB write)."""
    import asyncio

    from lionagi.studio.services.schedules import update_schedule

    # Existing schedule has action_kind=agent and no action_flow_yaml
    existing = {
        "id": "sid-001",
        "name": "patch-reject-test",
        "trigger_type": "cron",
        "action_kind": "agent",
        "action_model": "sonnet",
        "action_prompt": "hello",
        "action_flow_yaml": None,
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            try:
                # PATCH only action_kind → merges to flow_yaml with no YAML
                await update_schedule("sid-001", {"action_kind": "flow_yaml"})
                raise AssertionError("Should have raised ValueError")
            except ValueError as exc:
                assert "action_flow_yaml" in str(exc)
            # Verify the DB write was never reached
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())


def test_update_schedule_rejects_patch_with_malformed_yaml():
    """PATCH action_flow_yaml with malformed YAML raises ValueError (before DB write)."""
    import asyncio

    from lionagi.studio.services.schedules import update_schedule

    # Existing schedule is already flow_yaml with valid spec
    existing = {
        "id": "sid-002",
        "name": "patch-bad-yaml-test",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_flow_yaml": "prompt: valid spec\n",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            try:
                await update_schedule("sid-002", {"action_flow_yaml": "key: [unclosed"})
                raise AssertionError("Should have raised ValueError")
            except ValueError as exc:
                assert "flow_yaml" in str(exc).lower() or "YAML" in str(exc)
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())


def test_create_schedule_github_poll_requires_repo():
    """create_schedule raises ValueError when a github_poll trigger has no github_repo.

    Mirrors the cron/interval required-field guards — the check fires before any
    DB write, so no state fixture is needed."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "gh-no-repo",
        "trigger_type": "github_poll",
        "action_kind": "agent",
        "action_prompt": "review",
        # github_repo intentionally absent
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.run(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "github_repo" in str(exc)


def test_create_schedule_rejects_non_positive_poll_interval():
    """create_schedule raises ValueError when poll_interval_sec < 1 (fires pre-DB-write)."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "gh-bad-poll",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "poll_interval_sec": -1,
        "action_kind": "agent",
        "action_prompt": "review",
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.run(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "poll_interval_sec" in str(exc)
