# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the schedule declaration `notify` fire path: registering
the existing terminal-callback machinery on a scheduled invocation, scoped
to that invocation's id and filtered to the declared status list."""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.lifecycle.callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EntityRef,
    RunTerminalEnvelope,
)
from lionagi.studio.scheduler.engine import _register_schedule_notify, _unregister_schedule_notify


def _envelope(inv_id: str, status: str) -> RunTerminalEnvelope:
    return RunTerminalEnvelope(
        event_id=uuid.uuid4().hex,
        entity=EntityRef(kind="invocation", id=inv_id),
        previous_status="running",
        terminal_status=status,
        reason_code="test",
        occurred_at=time.time(),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    DEFAULT_TERMINAL_CALLBACKS.clear()


def test_no_notify_declared_registers_nothing():
    assert _register_schedule_notify("inv-1", None, None) is None
    assert _register_schedule_notify("inv-1", [], "notify-run") is None
    assert _register_schedule_notify("inv-1", ["failed"], "") is None


@pytest.mark.asyncio
async def test_status_in_on_fires_command(tmp_path):
    marker = tmp_path / "fired.json"
    inv_id = f"inv-{uuid.uuid4().hex[:8]}"
    command = f"python3 -c \"import sys,json,pathlib; pathlib.Path(r'{marker}').write_text(sys.stdin.read())\""
    name = _register_schedule_notify(inv_id, ["failed", "timed_out"], command)
    assert name is not None
    try:
        await DEFAULT_TERMINAL_CALLBACKS.emit(_envelope(inv_id, "failed"))
    finally:
        _unregister_schedule_notify(name)

    payload = json.loads(marker.read_text())
    assert payload["entity"]["id"] == inv_id
    assert payload["terminal_status"] == "failed"
    # The engine unregisters in a `finally` right after the one terminal
    # transition an invocation ever has -- mirrored here by the test's own
    # finally block above, so the scope is gone once we get here.
    assert name not in DEFAULT_TERMINAL_CALLBACKS


@pytest.mark.asyncio
async def test_status_not_in_on_does_not_fire(tmp_path):
    marker = tmp_path / "fired.json"
    inv_id = f"inv-{uuid.uuid4().hex[:8]}"
    command = f"python3 -c \"import pathlib; pathlib.Path(r'{marker}').write_text('fired')\""
    name = _register_schedule_notify(inv_id, ["failed"], command)
    try:
        await DEFAULT_TERMINAL_CALLBACKS.emit(_envelope(inv_id, "completed"))
    finally:
        _unregister_schedule_notify(name)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_callback_failure_does_not_raise_or_alter_outcome():
    """A failing notify adapter (nonexistent binary) is swallowed by the
    shared terminal-callback machinery -- emit() must never raise, and the
    envelope this test constructs represents the run's already-decided
    outcome, which the callback has no way to touch."""
    inv_id = f"inv-{uuid.uuid4().hex[:8]}"
    name = _register_schedule_notify(inv_id, ["failed"], "definitely-not-a-real-executable-xyz")
    try:
        envelope = _envelope(inv_id, "failed")
        await DEFAULT_TERMINAL_CALLBACKS.emit(envelope)
        assert envelope.terminal_status == "failed"
    finally:
        _unregister_schedule_notify(name)


@pytest.mark.asyncio
async def test_unregistered_scope_never_fires():
    inv_id = f"inv-{uuid.uuid4().hex[:8]}"
    name = _register_schedule_notify(inv_id, ["failed"], "true")
    _unregister_schedule_notify(name)
    # No exception, no registration left to match against.
    await DEFAULT_TERMINAL_CALLBACKS.emit(_envelope(inv_id, "failed"))
    assert name not in DEFAULT_TERMINAL_CALLBACKS


# ---------------------------------------------------------------------------
# _fire_inner registration-lifetime regressions (mock service level)
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402


def _notify_schedule(**overrides) -> dict:
    base = {
        "id": "sched-notify",
        "name": "notify-sched",
        "enabled": 1,
        "trigger_type": "interval",
        "trigger_interval_secs": 60,
        "trigger_cron": None,
        "trigger_at": None,
        "next_fire_at": None,
        "last_fired_at": None,
        "max_runs": None,
        "action_kind": "agent",
        "action_agent": "default",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
        "on_success": None,
        "on_fail": None,
        "overlap_policy": "skip",
        "missed_fire_policy": "skip",
        "notify_on": ["cancelled"],
        "notify_command": "true",
    }
    base.update(overrides)
    return base


def _notify_registration_names() -> list[str]:
    return [
        n
        for n in DEFAULT_TERMINAL_CALLBACKS._registrations
        if n.startswith("notify.schedule.invocation.")
    ]


def _make_engine_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_schedule = AsyncMock(return_value=None)
    svc.update_schedule = AsyncMock()
    svc.create_schedule_run_and_advance = AsyncMock()
    svc.schedule_run_exists_since = AsyncMock(return_value=False)
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.count_schedule_runs = AsyncMock(return_value=0)
    svc.get_invocation = AsyncMock(return_value=None)
    return svc


@pytest.mark.asyncio
async def test_abandoned_recovery_unregisters_only_after_terminal_write():
    """A rejected superseded-recovery fire finalizes its invocation as
    cancelled; the notify registration must still be present at that write
    (so a declared notify on 'cancelled' can fire) and gone afterwards."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_engine_svc()
    # Force the pre-spawn exception branch, then reject the recovery
    # occurrence write (orphan no longer qualifies) so the fire takes the
    # abandoned-superseded-recovery exit.
    svc.tombstone_and_replace_schedule_run = AsyncMock(return_value=False)
    registered_at_terminal_write: list[bool] = []

    async def _update_status(entity_type, entity_id, **kwargs):
        if entity_type == "invocation" and kwargs.get("new_status") == "cancelled":
            registered_at_terminal_write.append(bool(_notify_registration_names()))
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=RuntimeError("bad argv"),
    ):
        await engine._fire(
            _notify_schedule(),
            "run-notify-1",
            trigger_context={"scheduled": True},
            supersedes_run_id="orphan-1",
        )

    assert registered_at_terminal_write == [True]
    assert _notify_registration_names() == []


@pytest.mark.asyncio
async def test_create_invocation_failure_does_not_leak_registration():
    """An exception before the invocation row exists must drop the notify
    registration on the way out instead of leaving it in the process-wide
    registry forever."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_engine_svc()
    svc.create_invocation = AsyncMock(side_effect=RuntimeError("db down"))
    engine = SchedulerEngine(svc=svc)

    with pytest.raises(RuntimeError, match="db down"):
        await engine._fire_inner(
            _notify_schedule(),
            "run-notify-2",
            trigger_context={"scheduled": True},
        )

    assert _notify_registration_names() == []
