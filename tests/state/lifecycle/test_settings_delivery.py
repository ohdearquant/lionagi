# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Explicit delivery outcomes for settings-driven terminal notifications."""

from __future__ import annotations

import pytest

from lionagi.state.lifecycle.callbacks import EntityRef, RunTerminalEnvelope
from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery
from lionagi.state.lifecycle.settings_delivery import prepare_resolved_delivery


def _event() -> RunTerminalEnvelope:
    return RunTerminalEnvelope(
        event_id="transition-1",
        entity=EntityRef(kind="session", id="session-1"),
        previous_status="running",
        terminal_status="failed",
        reason_code="session.failed",
        occurred_at=1.0,
    )


def test_settings_filter_is_not_applicable_without_building_handler(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a filtered notification must not build its handler")

    monkeypatch.setattr("lionagi.state.lifecycle.settings_delivery.build_handler", unexpected)
    resolved = ResolvedNotifyHandler(python_ref="example:hook", filter_kinds=("play",))
    assert prepare_resolved_delivery(resolved, _event()) is DeliveryOutcome.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DeliveryOutcome.SUCCESS),
        (False, DeliveryOutcome.FAILURE),
        (True, DeliveryOutcome.SUCCESS),
    ],
)
def test_python_hook_uses_its_own_result(monkeypatch, value, expected):
    monkeypatch.setattr(
        "lionagi.state.lifecycle.settings_delivery.build_handler", lambda *a, **kw: lambda _: value
    )
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(python_ref="example:hook"), _event())
    assert isinstance(prepared, PreparedDelivery)
    assert prepared.call() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ok", "expected"),
    [
        (True, DeliveryOutcome.SUCCESS),
        (False, DeliveryOutcome.FAILURE),
        (None, DeliveryOutcome.FAILURE),
    ],
)
async def test_exec_requires_explicit_process_outcome(monkeypatch, ok, expected):
    def build(_resolved, *, outcome_fn):
        async def handler(_event):
            if ok is not None:
                outcome_fn(ok=ok, exit_code=0 if ok else 1, stderr_text=None)

        return handler

    monkeypatch.setattr("lionagi.state.lifecycle.settings_delivery.build_handler", build)
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(argv=("hook",)), _event())
    assert isinstance(prepared, PreparedDelivery)
    assert await prepared.call() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 2])
async def test_real_exec_adapter_reports_process_exit(monkeypatch, returncode):
    from lionagi.state.lifecycle import notify_settings

    class Process:
        pid = None

        async def communicate(self, payload):
            assert payload
            return b"", b"failed" if returncode else b""

        @property
        def returncode(self):
            return returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(notify_settings.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *a, **k: None)
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(argv=("notify-hook",)), _event())
    assert isinstance(prepared, PreparedDelivery)
    expected = DeliveryOutcome.SUCCESS if returncode == 0 else DeliveryOutcome.FAILURE
    assert await prepared.call() is expected


@pytest.mark.asyncio
async def test_real_exec_adapter_spawn_failure_is_failure(monkeypatch):
    from lionagi.state.lifecycle import notify_settings

    async def spawn(*args, **kwargs):
        raise OSError("not available")

    monkeypatch.setattr(notify_settings.asyncio, "create_subprocess_exec", spawn)
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(argv=("notify-hook",)), _event())
    assert isinstance(prepared, PreparedDelivery)
    assert await prepared.call() is DeliveryOutcome.FAILURE


@pytest.mark.asyncio
async def test_async_python_hook_normal_return_and_explicit_false(monkeypatch):
    from lionagi.state.lifecycle import notify_settings

    async def hook(envelope):
        assert envelope.event_id == "transition-1"
        return False

    monkeypatch.setattr(notify_settings, "_make_python_handler", lambda _ref: hook)
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(python_ref="example:hook"), _event())
    assert isinstance(prepared, PreparedDelivery)
    assert await prepared.call() is DeliveryOutcome.FAILURE


@pytest.mark.asyncio
async def test_real_exec_adapter_timeout_is_failure(monkeypatch):
    import asyncio

    from lionagi.state.lifecycle import notify_settings

    class Process:
        pid = None

        async def communicate(self, payload):
            raise asyncio.TimeoutError

    async def spawn(*args, **kwargs):
        return Process()

    async def finished(*args, **kwargs):
        return None

    monkeypatch.setattr(notify_settings.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(notify_settings, "aterminate_process_group", finished)
    monkeypatch.setattr(notify_settings, "_await_proc_dead", finished)
    prepared = prepare_resolved_delivery(ResolvedNotifyHandler(argv=("notify-hook",)), _event())
    assert isinstance(prepared, PreparedDelivery)
    assert await prepared.call() is DeliveryOutcome.FAILURE
