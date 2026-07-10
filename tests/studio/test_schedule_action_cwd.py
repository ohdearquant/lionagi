# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the persisted per-schedule execution root (ADR-0070 delta 1):
services/schedules._svc_validate_action_cwd, and the create_schedule /
update_schedule derivation + validation paths."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from unittest.mock import AsyncMock, patch

from lionagi.studio.services.schedules import (
    _svc_validate_action_cwd,
    create_schedule,
    update_schedule,
)


def _create_data(**overrides) -> dict:
    base = {
        "name": "action-cwd-test",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _svc_validate_action_cwd — pure logic
# ---------------------------------------------------------------------------


def test_validate_action_cwd_none_is_noop():
    _svc_validate_action_cwd(None)


def test_validate_action_cwd_empty_string_is_noop():
    _svc_validate_action_cwd("")


def test_validate_action_cwd_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute"):
        _svc_validate_action_cwd("relative/path")


def test_validate_action_cwd_rejects_nonexistent_directory():
    with pytest.raises(ValueError, match="does not exist"):
        _svc_validate_action_cwd("/no/such/directory/at/all")


def test_validate_action_cwd_accepts_existing_absolute_directory(tmp_path):
    _svc_validate_action_cwd(str(tmp_path))


def test_validate_action_cwd_rejects_file_not_directory(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    with pytest.raises(ValueError, match="does not exist"):
        _svc_validate_action_cwd(str(f))


# ---------------------------------------------------------------------------
# create_schedule — explicit action_cwd wins, validated before the DB write
# ---------------------------------------------------------------------------


def test_create_schedule_rejects_invalid_action_cwd():
    data = _create_data(action_cwd="relative/path")

    async def _run():
        await create_schedule(data)

    with pytest.raises(ValueError, match="absolute"):
        asyncio.run(_run())


def test_create_schedule_persists_explicit_action_cwd(tmp_path):
    data = _create_data(action_cwd=str(tmp_path))

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            mock_db.create_schedule.assert_awaited_once()
            (written,), _kwargs = mock_db.create_schedule.await_args
            assert written["action_cwd"] == str(tmp_path)

    asyncio.run(_run())


def test_create_schedule_derives_action_cwd_from_action_project(tmp_path, monkeypatch):
    """No explicit action_cwd, but action_project resolves to a real,
    existing path -- that path is snapshotted into action_cwd at creation."""
    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    data = _create_data(action_project="myproj")

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            (written,), _kwargs = mock_db.create_schedule.await_args
            assert written["action_cwd"] == str(project_dir)

    asyncio.run(_run())
    fake_get_project.assert_awaited_once_with("myproj")


def test_create_schedule_explicit_action_cwd_wins_over_action_project(tmp_path, monkeypatch):
    """Explicit action_cwd is never overridden by an action_project lookup."""
    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    explicit_dir = tmp_path / "explicit-root"
    explicit_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    data = _create_data(action_project="myproj", action_cwd=str(explicit_dir))

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            (written,), _kwargs = mock_db.create_schedule.await_args
            assert written["action_cwd"] == str(explicit_dir)

    asyncio.run(_run())
    fake_get_project.assert_not_awaited()


def test_create_schedule_no_cwd_no_resolvable_project_stays_none(monkeypatch):
    """Neither action_cwd nor a resolvable action_project: action_cwd stays
    None (a pure-API creation path has no meaningful client cwd)."""
    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    data = _create_data(action_project="unregistered")

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            (written,), _kwargs = mock_db.create_schedule.await_args
            assert written["action_cwd"] is None

    asyncio.run(_run())


def test_create_schedule_without_action_project_stays_none_without_lookup(monkeypatch):
    """No action_cwd and no action_project at all: action_cwd stays None and
    the project registry is never consulted."""
    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    data = _create_data()

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            (written,), _kwargs = mock_db.create_schedule.await_args
            assert written["action_cwd"] is None

    asyncio.run(_run())
    fake_get_project.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_schedule — action_cwd validated at the PATCH boundary
# ---------------------------------------------------------------------------


def test_update_schedule_rejects_invalid_action_cwd():
    existing = {
        "id": "sid-cwd-1",
        "name": "action-cwd-patch-test",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="does not exist"):
                await update_schedule("sid-cwd-1", {"action_cwd": "/no/such/directory"})
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())


def test_update_schedule_accepts_valid_action_cwd(tmp_path):
    existing = {
        "id": "sid-cwd-2",
        "name": "action-cwd-patch-ok",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await update_schedule("sid-cwd-2", {"action_cwd": str(tmp_path)})
            assert result is True
            mock_db.update_schedule.assert_awaited_once()

    asyncio.run(_run())
