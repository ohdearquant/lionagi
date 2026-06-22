"""The startup WAL checkpoint is deferred off the readiness path; reconciliation
is not.

The studio lifespan keeps stale-session reconciliation pre-yield (stateful /api
routes read the rows it corrects) but defers the WAL checkpoint to a background
task so /health serves the instant uvicorn binds. These tests pin both halves of
that contract, that the deferred checkpoint still runs with actor='startup', that
a shutdown landing mid-checkpoint cancels it cleanly, and that a checkpoint
failure is logged rather than silently dropped.

The real scheduler is patched out: its first tick runs the same maintenance
functions (and spawns aiosqlite workers), which would otherwise pollute these
assertions and leave a worker posting to a closed loop at teardown.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from lionagi.studio.app import app

_SCHED = "lionagi.studio.scheduler.engine.scheduler"
_RECON = "lionagi.studio.services.lifecycle.run_startup_reconciliation"
_CKPT = "lionagi.studio.services.db_maintenance.checkpoint_state_db"


class _FakeScheduler:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _Gate:
    """A cross-thread, cancellable park point.

    The lifespan runs in TestClient's portal thread; the test thread releases via
    the captured loop. Parking on an asyncio.Event (not a thread-pool blocking
    wait) keeps the park cancellable, so a shutdown can tear it down."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    async def wait(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self.entered.set()
        await self._event.wait()

    def release(self) -> None:
        if self._loop is not None and self._event is not None:
            try:
                self._loop.call_soon_threadsafe(self._event.set)
            except RuntimeError:
                pass  # loop already closed — checkpoint already settled


def _isolate(monkeypatch) -> threading.Event:
    """Replace the lifespan's side-effecting collaborators so a warmup test sees
    only the checkpoint defer: no real scheduler, and reconciliation a fast no-op
    that records that it ran (pre-yield)."""
    monkeypatch.setattr(_SCHED, _FakeScheduler())
    recon_ran = threading.Event()

    async def _recon():
        recon_ran.set()
        return {}

    monkeypatch.setattr(_RECON, _recon)
    return recon_ran


def test_health_serves_before_checkpoint_completes(monkeypatch):
    """/health answers 200 while the WAL checkpoint is still parked, and
    reconciliation has already run. RED if the checkpoint defer is reverted: an
    inline await would hang TestClient entry on the gate."""
    recon_ran = _isolate(monkeypatch)
    gate = _Gate()

    async def _blocking_ckpt(actor=None):
        await gate.wait()

    monkeypatch.setattr(_CKPT, _blocking_ckpt)

    try:
        with TestClient(app) as client:
            assert gate.entered.wait(timeout=5), "warmup never started the checkpoint"
            assert recon_ran.is_set(), "reconciliation must run pre-yield, before serving"
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
            gate.release()  # let the checkpoint finish before shutdown
    finally:
        gate.release()


def test_warmup_runs_checkpoint_with_startup_actor(monkeypatch):
    """The deferred checkpoint still runs, with actor='startup', and
    reconciliation ran pre-yield."""
    recon_ran = _isolate(monkeypatch)
    ckpt_done = threading.Event()
    seen: dict[str, object] = {}

    async def _spy_ckpt(actor=None):
        seen["actor"] = actor
        ckpt_done.set()

    monkeypatch.setattr(_CKPT, _spy_ckpt)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert recon_ran.is_set()
        assert ckpt_done.wait(timeout=5), "deferred checkpoint did not run"

    assert seen["actor"] == "startup"


def test_shutdown_cancels_pending_checkpoint(monkeypatch):
    """A shutdown landing while the checkpoint is parked cancels it and returns —
    no hang, no leaked task. The park is never released, so only cancellation can
    let the context manager exit."""
    _isolate(monkeypatch)
    gate = _Gate()

    async def _never(actor=None):
        await gate.wait()

    monkeypatch.setattr(_CKPT, _never)

    with TestClient(app) as client:
        assert gate.entered.wait(timeout=5), "warmup never started the checkpoint"
        assert client.get("/health").status_code == 200
    # Exiting the context ran shutdown while the checkpoint was still parked.
    # Reaching here means _finalize_warmup cancelled it cleanly.


def test_checkpoint_failure_is_logged_not_fatal(monkeypatch):
    """An unexpected checkpoint failure is logged (not silently swallowed) and
    does not break startup or shutdown."""
    _isolate(monkeypatch)
    logged = threading.Event()

    class _SpyHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "checkpoint" in record.getMessage().lower():
                logged.set()

    async def _boom(actor=None):
        raise RuntimeError("checkpoint boom")

    monkeypatch.setattr(_CKPT, _boom)

    spy = _SpyHandler()
    logger = logging.getLogger("lionagi.studio.app")
    logger.addHandler(spy)
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert logged.wait(timeout=5), "checkpoint failure was not logged"
    finally:
        logger.removeHandler(spy)
