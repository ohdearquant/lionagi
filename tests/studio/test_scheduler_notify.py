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
