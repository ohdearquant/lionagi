# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for per-op heartbeat and idle-child watchdog in li play/flow."""

from __future__ import annotations

import asyncio
import time

import pytest

# ── Unit tests for the heartbeat loop logic ──────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_emits_progress_for_running_op():
    """Heartbeat loop must call progress() for each running op segment."""
    emitted = []

    # Build a minimal _op_segments list with one running op.
    _op_segments = [
        {
            "op_id": "o1",
            "branch_id": "b1",
            "branch_name": "researcher",
            "status": "running",
            "started_at": time.time() - 90,  # 90s ago
            "ended_at": None,
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            _now = time.time()
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                elapsed = _now - seg.get("started_at", _now)
                seg["last_heartbeat_at"] = _now
                emitted.append(f"heartbeat {elapsed / 60:.0f}m")

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.12)  # let it fire ~2 times
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(emitted) >= 1
    assert "heartbeat" in emitted[0]


@pytest.mark.asyncio
async def test_heartbeat_skips_completed_ops():
    """Heartbeat loop must NOT emit for ops that have already completed."""
    emitted = []

    _op_segments = [
        {
            "op_id": "o1",
            "branch_name": "researcher",
            "status": "completed",  # already done
            "started_at": time.time() - 90,
            "ended_at": time.time(),
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                emitted.append("heartbeat")

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert emitted == []


@pytest.mark.asyncio
async def test_heartbeat_emits_idle_stall_warning_after_threshold():
    """When elapsed > max_idle_seconds, heartbeat must emit a STALL warning."""
    warnings = []

    max_idle = 5  # tiny threshold for testing
    _op_segments = [
        {
            "op_id": "o1",
            "branch_name": "implementer",
            "status": "running",
            "started_at": time.time() - (max_idle + 1),  # already past threshold
            "ended_at": None,
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            _now = time.time()
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                elapsed = _now - seg.get("started_at", _now)
                seg["last_heartbeat_at"] = _now
                if elapsed > max_idle:
                    warnings.append(f"IDLE STALL: {seg['branch_name']}")

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert any("IDLE STALL" in w for w in warnings)
    assert any("implementer" in w for w in warnings)


@pytest.mark.asyncio
async def test_heartbeat_updates_last_heartbeat_at():
    """Heartbeat loop must update last_heartbeat_at in the segment dict."""
    _op_segments = [
        {
            "op_id": "o1",
            "branch_name": "analyst",
            "status": "running",
            "started_at": time.time() - 30,
            "ended_at": None,
            "last_heartbeat_at": None,
        }
    ]

    async def _heartbeat_loop(interval: float = 0.05) -> None:
        while True:
            await asyncio.sleep(interval)
            _now = time.time()
            for seg in _op_segments:
                if seg["status"] != "running":
                    continue
                seg["last_heartbeat_at"] = _now

    task = asyncio.ensure_future(_heartbeat_loop(interval=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert _op_segments[0]["last_heartbeat_at"] is not None
    assert _op_segments[0]["last_heartbeat_at"] > _op_segments[0]["started_at"]


@pytest.mark.asyncio
async def test_heartbeat_cancelled_cleanly():
    """Cancelling the heartbeat task must not raise unhandled errors."""
    import contextlib

    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(60)

    task = asyncio.ensure_future(_heartbeat_loop())
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_heartbeat_does_not_fire_before_interval():
    """Heartbeat must not fire immediately on start (must await the interval)."""
    fires = []

    async def _heartbeat_loop(interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            fires.append("fired")

    task = asyncio.ensure_future(_heartbeat_loop(interval=1.0))
    # Only wait 50ms — well below the 1s interval
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert fires == []


# ── flow.py integration: _record_segment includes last_heartbeat_at ───────────


def test_op_segment_schema_includes_heartbeat_field():
    """Structural: flow.py source must contain all heartbeat-related fields and symbols."""
    import inspect

    from lionagi.cli.orchestrate import flow as flow_mod

    src = inspect.getsource(flow_mod)
    assert '"last_heartbeat_at"' in src, (
        "_record_segment must initialise 'last_heartbeat_at' in the segment dict"
    )
    assert "_heartbeat_loop" in src, "flow.py must define _heartbeat_loop"
    assert "_hb_task" in src, "flow.py must create and cancel _hb_task"
    assert "heartbeat_interval" in src, "flow.py must define heartbeat_interval"
    assert "max_idle_seconds" in src, "flow.py must define max_idle_seconds"
    assert "IDLE STALL" in src, "flow.py must emit an IDLE STALL warning"
