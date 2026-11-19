# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A start that fails part way stops what it had started, and nothing it had not.\n\nThe attached application is Studio, whose hooks are its Operator startup and shutdown.\n"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from importlib import import_module
from types import SimpleNamespace

import pytest

from lionagi.ln.concurrency import ExceptionGroup

# The normal stop, in the order the lifespan runs it.
FULL_STOP = [
    "disarm",
    "reconnect.stop",
    "warmup.finish",
    "mirror.stop",
    "operator.shutdown",
    "launches.shutdown",
    "scheduler.stop",
]


class _Boom(Exception):
    pass


def _recording_lifespan(monkeypatch: pytest.MonkeyPatch, *, fail_at: str | None = None):
    """The app with each startup and shutdown step replaced by one that records its name.

    ``fail_at`` names the startup step that raises ``_Boom`` instead of recording.
    """
    app = import_module("lionhub.serve.daemon.app")
    identity = import_module("lionhub.serve.diagnostics")
    daemon_log = import_module("lionhub.serve.daemon.daemon_log")
    engine = import_module("lionhub.orchestration.scheduler.engine")
    lifecycle = import_module("lionhub.orchestration.scheduler.lifecycle")
    operator = import_module("lionagi.studio.services.operator")
    launches = import_module("lionhub.serve.daemon.services.launches")
    notify = import_module("lionagi.state.lifecycle.notify_settings")
    events: list[str] = []

    def step(name: str):
        def run(*_args):
            if name == fail_at:
                raise _Boom(name)
            events.append(name)

        return run

    def async_step(name: str):
        sync = step(name)

        async def run(*_args):
            sync()

        return run

    class _Channel:
        key = b"k"

        def stop(self) -> None:
            events.append("reconnect.stop")

    def reconnect_start():
        step("reconnect.start")()
        return _Channel()

    def mirror_start():
        step("mirror.start")()
        return asyncio.Event(), object()

    async def mirror_stop(stop, task):
        if stop is not None:
            events.append("mirror.stop")

    async def finalize_warmup(task):
        if task is not None:
            events.append("warmup.finish")

    monkeypatch.setattr(identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(daemon_log, "daemon_log_handler", nullcontext)
    monkeypatch.setattr(engine.scheduler, "start", async_step("scheduler.start"))
    monkeypatch.setattr(engine.scheduler, "stop", async_step("scheduler.stop"))
    monkeypatch.setattr(lifecycle, "run_startup_reconciliation", async_step("reconcile"))
    monkeypatch.setattr(operator, "operator_startup", async_step("operator.startup"))
    monkeypatch.setattr(operator, "operator_shutdown", async_step("operator.shutdown"))
    monkeypatch.setattr(launches, "shutdown_launches", async_step("launches.shutdown"))
    monkeypatch.setattr(notify, "register_settings_terminal_callback", step("notify"))
    monkeypatch.setattr(app, "arm_traceback_dump", step("arm"))
    monkeypatch.setattr(app, "disarm_traceback_dump", step("disarm"))
    monkeypatch.setattr(app, "_emit_startup_warnings", step("warnings"))
    monkeypatch.setattr(app, "_refuse_or_prepare_leg_host", async_step("leg_host"))
    monkeypatch.setattr(app, "begin_serving", step("begin_serving"))
    monkeypatch.setattr(app, "_start_reconnect_channel", reconnect_start)
    monkeypatch.setattr(app, "_start_claude_mirror", mirror_start)
    monkeypatch.setattr(app, "_stop_claude_mirror", mirror_stop)

    async def warmup() -> None:
        # Runs as a background task whenever the loop gets to it, so it records nothing.
        pass

    monkeypatch.setattr(app, "_startup_warmup", warmup)
    monkeypatch.setattr(app, "_finalize_warmup", finalize_warmup)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    return app, events


class _Root:
    """A root application stand-in: its state, and weak references like a real application."""

    def __init__(self, state: SimpleNamespace) -> None:
        self.state = state


def _instance() -> _Root:
    """A root application's state with Studio attached, its hooks read when called."""
    from lionhub.serve.daemon.app import Attachment

    operator = import_module("lionagi.studio.services.operator")

    async def startup():
        return await operator.operator_startup()

    async def shutdown():
        await operator.operator_shutdown()

    attachment = Attachment(route_modules=(), startup=startup, shutdown=shutdown)
    return _Root(SimpleNamespace(session_tail_registry=None, attachment=attachment))


async def _serve_once(app, instance=None) -> None:
    async with app.lifespan(instance or _instance()):
        pass


def _after(events: list[str], name: str) -> list[str]:
    """The events recorded after ``name``, that is, the shutdown the failure ran."""
    return events[events.index(name) + 1 :]


@pytest.mark.asyncio
async def test_a_normal_stop_keeps_the_shutdown_order(monkeypatch: pytest.MonkeyPatch) -> None:
    app, events = _recording_lifespan(monkeypatch)

    await _serve_once(app)

    assert _after(events, "begin_serving") == ["reconnect.start", "mirror.start", *FULL_STOP]


@pytest.mark.asyncio
async def test_a_failed_reconciliation_stops_the_scheduler_and_disarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch, fail_at="reconcile")

    with pytest.raises(_Boom, match="reconcile"):
        await _serve_once(app)

    assert _after(events, "scheduler.start") == ["disarm", "launches.shutdown", "scheduler.stop"]


@pytest.mark.asyncio
async def test_a_failed_operator_recovery_is_shut_down_with_the_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch, fail_at="operator.startup")

    with pytest.raises(_Boom, match="operator.startup"):
        await _serve_once(app)

    assert _after(events, "reconcile") == [
        "disarm",
        "operator.shutdown",
        "launches.shutdown",
        "scheduler.stop",
    ]


@pytest.mark.asyncio
async def test_a_failed_mirror_start_closes_the_reconnect_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch, fail_at="mirror.start")
    instance = _instance()

    with pytest.raises(_Boom, match="mirror.start"):
        await _serve_once(app, instance)

    assert _after(events, "reconnect.start") == [
        "disarm",
        "reconnect.stop",
        "operator.shutdown",
        "launches.shutdown",
        "scheduler.stop",
    ]
    assert instance not in import_module("lionhub.serve.daemon.app")._reconnect_keys


@pytest.mark.asyncio
async def test_a_refused_leg_host_disarms_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch, fail_at="leg_host")

    with pytest.raises(_Boom, match="leg_host"):
        await _serve_once(app)

    assert events == ["arm", "warnings", "disarm"]


@pytest.mark.asyncio
async def test_a_failed_arm_is_disarmed_in_case_it_had_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arming can set the timer and then fail to say so; disarming one that never armed is safe.
    app, events = _recording_lifespan(monkeypatch, fail_at="arm")

    with pytest.raises(_Boom, match="arm"):
        await _serve_once(app)

    assert events == ["disarm"]


@pytest.mark.asyncio
async def test_a_log_handler_that_cannot_install_disarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)
    daemon_log = import_module("lionhub.serve.daemon.daemon_log")

    class _Refuses:
        def __enter__(self):
            raise _Boom("log handler")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(daemon_log, "daemon_log_handler", _Refuses)

    with pytest.raises(_Boom, match="log handler"):
        await _serve_once(app)

    assert events == ["arm", "warnings", "leg_host", "notify", "disarm"]


@pytest.mark.asyncio
async def test_a_failed_disarm_before_the_daemon_block_is_reported_beside_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _events = _recording_lifespan(monkeypatch, fail_at="leg_host")

    def disarm_fails() -> None:
        raise RuntimeError("disarm failed")

    monkeypatch.setattr(app, "disarm_traceback_dump", disarm_fails)

    with pytest.raises(ExceptionGroup, match="Studio startup failed") as info:
        await _serve_once(app)

    assert [type(exc) for exc in info.value.exceptions] == [_Boom, RuntimeError]


@pytest.mark.asyncio
async def test_a_failed_warmup_start_stops_the_mirror_it_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)

    def warmup_cannot_start():
        raise _Boom("warmup")

    monkeypatch.setattr(app, "_startup_warmup", warmup_cannot_start)

    with pytest.raises(_Boom, match="warmup"):
        await _serve_once(app)

    assert _after(events, "mirror.start") == [
        "disarm",
        "reconnect.stop",
        "mirror.stop",
        "operator.shutdown",
        "launches.shutdown",
        "scheduler.stop",
    ]


@pytest.mark.asyncio
async def test_a_failure_while_serving_is_a_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _events = _recording_lifespan(monkeypatch)
    engine = import_module("lionhub.orchestration.scheduler.engine")

    async def stop_fails() -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(engine.scheduler, "stop", stop_fails)

    with pytest.raises(ExceptionGroup, match="Studio shutdown failed") as info:
        async with app.lifespan(_instance()):
            raise ValueError("served, then failed")

    assert [type(exc) for exc in info.value.exceptions] == [RuntimeError]


@pytest.mark.asyncio
async def test_a_cleanup_failure_is_reported_beside_the_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch, fail_at="reconcile")
    engine = import_module("lionhub.orchestration.scheduler.engine")

    async def stop_fails() -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(engine.scheduler, "stop", stop_fails)

    with pytest.raises(ExceptionGroup, match="Studio startup failed") as info:
        await _serve_once(app)

    kinds = [type(exc) for exc in info.value.exceptions]
    assert kinds == [_Boom, RuntimeError]
    assert _after(events, "scheduler.start") == ["disarm", "launches.shutdown"]


@pytest.mark.asyncio
async def test_a_cancelled_start_stops_what_it_started_and_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)
    lifecycle = import_module("lionhub.orchestration.scheduler.lifecycle")
    entered = asyncio.Event()

    async def reconcile_hangs() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(lifecycle, "run_startup_reconciliation", reconcile_hangs)

    task = asyncio.create_task(_serve_once(app))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _after(events, "scheduler.start") == ["disarm", "launches.shutdown", "scheduler.stop"]


@pytest.mark.asyncio
async def test_a_cancelled_start_logs_what_its_cleanup_hit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, _events = _recording_lifespan(monkeypatch)
    lifecycle = import_module("lionhub.orchestration.scheduler.lifecycle")
    engine = import_module("lionhub.orchestration.scheduler.engine")
    entered = asyncio.Event()

    async def reconcile_hangs() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def stop_fails() -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(lifecycle, "run_startup_reconciliation", reconcile_hangs)
    monkeypatch.setattr(engine.scheduler, "stop", stop_fails)

    task = asyncio.create_task(_serve_once(app))
    await entered.wait()
    with caplog.at_level(logging.ERROR, logger=app.__name__):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    logged = [
        record
        for record in caplog.records
        if record.getMessage() == "Cleanup after an interrupted Studio start failed"
    ]
    assert [str(record.exc_info[1]) for record in logged] == ["stop failed"]


@pytest.mark.asyncio
async def test_a_cancellation_raised_by_a_shutdown_step_does_not_skip_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)

    async def mirror_stop_cancelled(stop, task):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "_stop_claude_mirror", mirror_stop_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _serve_once(app)

    assert _after(events, "warmup.finish") == [
        "operator.shutdown",
        "launches.shutdown",
        "scheduler.stop",
    ]


@pytest.mark.asyncio
async def test_an_interrupted_disarm_still_runs_every_later_step_and_drops_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)
    instance = _instance()
    held: list[bool] = []

    def disarm_interrupted() -> None:
        held.append(instance in app._reconnect_keys)
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "disarm_traceback_dump", disarm_interrupted)

    with pytest.raises(KeyboardInterrupt):
        async with app.lifespan(instance):
            held.append(instance in app._reconnect_keys)

    assert _after(events, "mirror.start") == FULL_STOP[1:]
    # Held while serving, dropped before the interrupted step.
    assert held == [True, False]
    assert instance not in app._reconnect_keys


@pytest.mark.asyncio
async def test_a_cancellation_during_the_shutdown_still_runs_every_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events = _recording_lifespan(monkeypatch)
    launches = import_module("lionhub.serve.daemon.services.launches")
    entered = asyncio.Event()

    async def launches_shutdown_hangs() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(launches, "shutdown_launches", launches_shutdown_hangs)

    task = asyncio.create_task(_serve_once(app))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert events[-1] == "scheduler.stop"


@pytest.mark.asyncio
async def test_a_mirror_tail_cancelled_elsewhere_is_stopped_quietly() -> None:
    app = import_module("lionhub.serve.daemon.app")
    tail = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    tail.cancel()

    await app._stop_claude_mirror(asyncio.Event(), tail)

    assert tail.cancelled()


@pytest.mark.asyncio
async def test_finalizing_the_warmup_does_not_swallow_its_callers_cancellation() -> None:
    app = import_module("lionhub.serve.daemon.app")
    release = asyncio.Event()

    async def warmup_finishes_its_write() -> None:
        # Holds on through its own cancellation until released, as a write in flight would.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    warmup = asyncio.create_task(warmup_finishes_its_write())
    await asyncio.sleep(0)
    caller = asyncio.create_task(app._finalize_warmup(warmup))
    await asyncio.sleep(0)
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, timeout=5)

    release.set()
    await asyncio.wait({warmup}, timeout=5)
    assert warmup.done()


@pytest.mark.asyncio
async def test_a_start_failure_is_logged_when_its_cleanup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, _events = _recording_lifespan(monkeypatch, fail_at="reconcile")

    async def mirror_stop_cancelled(stop, task):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "_stop_claude_mirror", mirror_stop_cancelled)

    with caplog.at_level(logging.ERROR, logger=app.__name__):
        with pytest.raises(asyncio.CancelledError):
            await _serve_once(app)

    logged = [
        record
        for record in caplog.records
        if record.getMessage() == "Studio startup failed and its cleanup was interrupted"
    ]
    assert [type(record.exc_info[1]) for record in logged] == [_Boom]
