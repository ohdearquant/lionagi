# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Readiness reports whether the scheduler is advancing, not only whether the store answers."""

from __future__ import annotations

import time

import pytest


def _probe(monkeypatch, **facts):
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.services import admin as admin_svc

    base = {
        "started_at": None,
        "last_tick_completed_at": None,
        "tick_interval_s": engine_mod._TICK_INTERVAL,
        "restarts": 0,
        "last_failure_at": None,
        "last_failure": None,
    }
    base.update(facts)
    monkeypatch.setattr(engine_mod.scheduler, "liveness", lambda: base)
    return admin_svc.scheduler_probe()


def test_a_scheduler_that_never_started_is_not_reported_as_advancing(monkeypatch):
    assert _probe(monkeypatch)["status"] == "unknown"


def test_a_scheduler_that_just_ticked_is_advancing(monkeypatch):
    result = _probe(monkeypatch, started_at=time.time() - 600, last_tick_completed_at=time.time())
    assert result["status"] == "advancing"
    assert result["seconds_since_advance"] < 5


def test_a_scheduler_whose_last_tick_is_long_past_is_stalled(monkeypatch):
    """The incident this exists for: the store answered while nothing fired for hours."""
    result = _probe(
        monkeypatch,
        started_at=time.time() - 7200,
        last_tick_completed_at=time.time() - 3600,
    )
    assert result["status"] == "stalled"
    assert result["seconds_since_advance"] > 3000


def test_a_scheduler_that_started_and_never_ticked_is_stalled(monkeypatch):
    """Never advancing is a stall too, and there is no last tick to measure from."""
    result = _probe(monkeypatch, started_at=time.time() - 3600)
    assert result["status"] == "stalled"
    assert "since the engine started" in result["detail"]


def test_a_freshly_started_scheduler_is_not_called_stalled_before_its_first_tick(monkeypatch):
    """The control: a false stall on every restart is how a field trains readers to ignore it."""
    result = _probe(monkeypatch, started_at=time.time() - 1)
    assert result["status"] == "advancing"


def test_restarts_are_surfaced_alongside_the_verdict(monkeypatch):
    result = _probe(
        monkeypatch,
        started_at=time.time() - 600,
        last_tick_completed_at=time.time(),
        restarts=3,
        last_failure_at=time.time() - 30,
        last_failure="RuntimeError: db locked",
    )
    assert result["restarts"] == 3
    assert "restarted 3 time(s)" in result["detail"]
    assert result["last_failure"] == "RuntimeError: db locked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_status", "sched_status", "expected_ready"),
    [
        ("healthy", "advancing", True),
        ("healthy", "stalled", False),
        ("slow", "advancing", False),
        ("unavailable", "stalled", False),
    ],
)
async def test_ready_is_true_only_when_both_subjects_are(
    monkeypatch, store_status, sched_status, expected_ready
):
    from lionagi.studio.services import admin as admin_svc

    async def _store(*, timeout_ms):
        return {"status": store_status, "detail": "d", "latency_ms": 1.0}

    monkeypatch.setattr(admin_svc, "store_probe", _store)
    monkeypatch.setattr(admin_svc, "scheduler_probe", lambda: {"status": sched_status})

    payload = await admin_svc.readiness_route(timeout_ms=1000)
    assert payload["ready"] is expected_ready
    # The store's own verdict keeps its meaning and its place, so existing readers still work.
    assert payload["status"] == store_status
    assert payload["latency_ms"] == 1.0
    assert payload["scheduler"]["status"] == sched_status
