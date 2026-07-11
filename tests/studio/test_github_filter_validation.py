# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the github_filter service-boundary allowlist
(services/schedules._svc_validate_github_filter) on create_schedule and
update_schedule."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from unittest.mock import AsyncMock, patch

from lionagi.studio.services.schedules import (
    _svc_validate_github_filter,
    create_schedule,
    update_schedule,
)


def _create_data(**overrides) -> dict:
    base = {
        "name": "gh-filter-test",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
        "action_prompt": "review",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _svc_validate_github_filter — pure logic
# ---------------------------------------------------------------------------


def test_validate_github_filter_none_is_noop():
    _svc_validate_github_filter(None)


def test_validate_github_filter_empty_dict_ok():
    _svc_validate_github_filter({})


def test_validate_github_filter_known_keys_ok():
    _svc_validate_github_filter({"state": "open", "base": "main", "draft": False})
    _svc_validate_github_filter({"event": "pr_merged"})


def test_validate_github_filter_all_frontend_event_values_ok():
    """The Studio create-schedule form ships four event choices (pr_merged,
    pr_opened, pr_updated, pr_closed) and defaults new schedules to
    pr_updated -- all four must be accepted at the write boundary even
    though only pr_merged has real dispatch semantics in github_poll()
    today, or the shipped default create flow would 400 on every save."""
    for event in ("pr_merged", "pr_opened", "pr_updated", "pr_closed"):
        _svc_validate_github_filter({"event": event})


def test_validate_github_filter_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown key"):
        _svc_validate_github_filter({"labels": ["bug"]})


def test_validate_github_filter_rejects_unknown_key_alongside_known_ones():
    with pytest.raises(ValueError, match="unknown key"):
        _svc_validate_github_filter({"state": "open", "assignee": "octocat"})


def test_validate_github_filter_rejects_unknown_event_value():
    with pytest.raises(ValueError, match="event"):
        _svc_validate_github_filter({"event": "pr_reopened"})


def test_validate_github_filter_rejects_non_dict():
    with pytest.raises(ValueError, match="object"):
        _svc_validate_github_filter("pr_merged")


def test_validate_github_filter_same_repo_only_true_ok():
    _svc_validate_github_filter({"same_repo_only": True})


def test_validate_github_filter_same_repo_only_false_ok():
    _svc_validate_github_filter({"same_repo_only": False})


def test_validate_github_filter_rejects_non_bool_same_repo_only():
    with pytest.raises(ValueError, match="same_repo_only"):
        _svc_validate_github_filter({"same_repo_only": "true"})
    with pytest.raises(ValueError, match="same_repo_only"):
        _svc_validate_github_filter({"same_repo_only": 1})


# ---------------------------------------------------------------------------
# create_schedule / update_schedule — validation fires before any DB write
# ---------------------------------------------------------------------------


def test_create_schedule_rejects_unknown_github_filter_key():
    data = _create_data(github_filter={"labels": ["bug"]})

    async def _run():
        await create_schedule(data)

    with pytest.raises(ValueError, match="unknown key"):
        asyncio.run(_run())


def test_create_schedule_rejects_unsupported_event_value():
    data = _create_data(github_filter={"event": "pr_reopened"})

    async def _run():
        await create_schedule(data)

    with pytest.raises(ValueError, match="event"):
        asyncio.run(_run())


def test_create_schedule_accepts_pr_merged_filter():
    """A valid pr_merged filter reaches the DB write (validation passes)."""
    data = _create_data(github_filter={"event": "pr_merged"})

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            mock_db.create_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_create_schedule_accepts_same_repo_only_true():
    data = _create_data(github_filter={"same_repo_only": True})

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            mock_db.create_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_create_schedule_accepts_same_repo_only_false():
    data = _create_data(github_filter={"same_repo_only": False})

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(data)
            mock_db.create_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_create_schedule_rejects_non_bool_same_repo_only():
    data = _create_data(github_filter={"same_repo_only": "true"})

    async def _run():
        await create_schedule(data)

    with pytest.raises(ValueError, match="same_repo_only"):
        asyncio.run(_run())


def test_update_schedule_rejects_unknown_github_filter_key():
    existing = {
        "id": "sid-gh-1",
        "name": "gh-filter-patch-test",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="unknown key"):
                await update_schedule("sid-gh-1", {"github_filter": {"milestone": "v1"}})
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())


def test_update_schedule_accepts_pr_merged_filter():
    existing = {
        "id": "sid-gh-2",
        "name": "gh-filter-patch-ok",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await update_schedule("sid-gh-2", {"github_filter": {"event": "pr_merged"}})
            assert result is True
            mock_db.update_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_update_schedule_accepts_same_repo_only_true():
    existing = {
        "id": "sid-gh-3",
        "name": "gh-filter-patch-same-repo-true",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await update_schedule("sid-gh-3", {"github_filter": {"same_repo_only": True}})
            assert result is True
            mock_db.update_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_update_schedule_accepts_same_repo_only_false():
    existing = {
        "id": "sid-gh-4",
        "name": "gh-filter-patch-same-repo-false",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await update_schedule("sid-gh-4", {"github_filter": {"same_repo_only": False}})
            assert result is True
            mock_db.update_schedule.assert_awaited_once()

    asyncio.run(_run())


def test_update_schedule_rejects_non_bool_same_repo_only():
    existing = {
        "id": "sid-gh-5",
        "name": "gh-filter-patch-same-repo-bad",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "action_kind": "agent",
    }

    async def _run():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=existing)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="same_repo_only"):
                await update_schedule("sid-gh-5", {"github_filter": {"same_repo_only": 1}})
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())
