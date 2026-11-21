# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A leg host starts neither the scheduler engine nor the transcript mirror."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from importlib import import_module
from types import SimpleNamespace

import pytest

from lionhub.serve.daemon.daemon_log import SCHEDULER_LOGGER


def _quiet_lifespan(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[str]]:
    """The app with every startup step but the two under test replaced by a no-op."""
    app = import_module("lionhub.serve.daemon.app")
    identity = import_module("lionhub.serve.diagnostics")
    daemon_log = import_module("lionhub.serve.daemon.daemon_log")
    engine = import_module("lionhub.orchestration.scheduler.engine")
    lifecycle = import_module("lionhub.orchestration.scheduler.lifecycle")
    operator = import_module("lionagi_studio.services.operator")
    launches = import_module("lionhub.serve.daemon.services.launches")
    notify = import_module("lionagi.state.lifecycle.notify_settings")
    started: list[str] = []

    async def noop(*_args):
        pass

    async def scheduler_start():
        started.append("scheduler")

    def mirror_start():
        started.append("mirror")
        return None, None

    monkeypatch.setattr(identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(daemon_log, "daemon_log_handler", nullcontext)
    monkeypatch.setattr(engine.scheduler, "start", scheduler_start)
    monkeypatch.setattr(engine.scheduler, "stop", noop)
    monkeypatch.setattr(lifecycle, "run_startup_reconciliation", noop)
    monkeypatch.setattr(operator, "operator_startup", noop)
    monkeypatch.setattr(operator, "operator_shutdown", noop)
    monkeypatch.setattr(launches, "shutdown_launches", noop)
    monkeypatch.setattr(notify, "register_settings_terminal_callback", lambda: None)
    monkeypatch.setattr(app, "arm_traceback_dump", lambda: None)
    monkeypatch.setattr(app, "disarm_traceback_dump", lambda: None)
    monkeypatch.setattr(app, "_emit_startup_warnings", lambda: None)
    monkeypatch.setattr(app, "_refuse_or_prepare_leg_host", noop)
    monkeypatch.setattr(app, "begin_serving", lambda: None)
    monkeypatch.setattr(app, "_start_reconnect_channel", lambda: None)
    monkeypatch.setattr(app, "_start_claude_mirror", mirror_start)
    monkeypatch.setattr(app, "_startup_warmup", noop)
    return app, started


async def _serve_once(app) -> None:
    instance = SimpleNamespace(state=SimpleNamespace(session_tail_registry=None))
    async with app.lifespan(instance):
        pass


@pytest.mark.asyncio
async def test_a_leg_host_starts_neither_the_scheduler_nor_the_mirror(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.setenv("LIONAGI_STUDIO_LEG_HOST", "1")
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)

    await _serve_once(app)

    assert started == []
    lines = [r.getMessage() for r in caplog.records if r.name == SCHEDULER_LOGGER]
    assert lines == [
        "Leg host: the scheduler engine is not started",
        "Leg host: the transcript mirror is not started",
    ]


@pytest.mark.asyncio
async def test_a_daemon_that_is_not_a_leg_host_starts_both(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The control: the same lifespan, outside leg-host mode, starts both."""
    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)

    await _serve_once(app)

    assert started == ["scheduler", "mirror"]
    assert not [r for r in caplog.records if r.getMessage().startswith("Leg host:")]
