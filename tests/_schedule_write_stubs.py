# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for tests that create and update schedules against a stubbed store."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionhub.serve.daemon.services.schedules import create_schedule, update_schedule


def _create_data(**overrides) -> dict:
    base = {
        "name": "chain-validation-test",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    base.update(overrides)
    return base


def _existing(**overrides) -> dict:
    base = {
        "id": "sid-chain-1",
        "name": "chain-validation-patch",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
    }
    base.update(overrides)
    return base


def _run_create(data: dict):
    """Run create_schedule against a stubbed store; return (store, ValueError | None)."""

    async def _go():
        with patch("lionhub.serve.daemon.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await create_schedule(data)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())


def _run_update(fields: dict, existing: dict | None = None):
    """Run update_schedule against a stubbed store; return (store, ValueError | None)."""
    row = existing if existing is not None else _existing()

    async def _go():
        with patch("lionhub.serve.daemon.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=row)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await update_schedule(row["id"], fields)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())
