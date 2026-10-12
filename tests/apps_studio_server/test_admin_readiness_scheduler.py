# SPDX-License-Identifier: Apache-2.0
"""Readiness reflects both the store probe and scheduler state."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lionagi.studio.scheduler import engine as engine_mod
from lionagi.studio.services import admin as admin_svc


async def _healthy_probe(*, timeout_ms: int) -> dict:
    return {"status": "healthy", "detail": "bounded read passed"}


@pytest.mark.asyncio
async def test_readiness_reports_a_stalled_scheduler_beside_a_healthy_store(monkeypatch) -> None:
    """A stalled scheduler makes readiness unhealthy even when the store answers."""
    monkeypatch.setattr(admin_svc, "store_probe", _healthy_probe)
    stalled = {
        "ready": False,
        "status": "stalled",
        "detail": "no scheduler tick has completed for 900s",
        "seconds_since_last_tick": 900.0,
    }
    monkeypatch.setattr(engine_mod.scheduler, "health", lambda: stalled)

    result = await admin_svc.readiness_route(timeout_ms=100)

    assert result["status"] == "unhealthy"
    assert "scheduler" in result["detail"]
    assert result["scheduler"] == stalled


@pytest.mark.asyncio
async def test_readiness_reports_a_scheduler_this_process_never_started_as_offline(
    monkeypatch,
) -> None:
    """A host that runs no scheduler is not failing to schedule."""
    monkeypatch.setattr(admin_svc, "store_probe", _healthy_probe)
    monkeypatch.setattr(engine_mod, "scheduler", engine_mod.SchedulerEngine(svc=AsyncMock()))

    result = await admin_svc.readiness_route(timeout_ms=100)

    report = result["scheduler"]
    assert report["status"] == "offline"
    assert report["ready"] is True
    assert report["last_tick_completed_at"] is None
    assert report["seconds_since_last_tick"] is None
    assert report["tick_loop_restarts"] == 0
