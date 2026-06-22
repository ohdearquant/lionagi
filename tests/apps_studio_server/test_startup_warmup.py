"""Startup warmup is backgrounded: readiness must not wait on reconciliation.

The studio lifespan defers stale-session reconciliation and the WAL checkpoint
to a background task so /health serves the instant uvicorn binds. These tests
pin that contract from both ends — readiness doesn't block, the deferred work
still runs, and a shutdown landing mid-warmup cancels it cleanly.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from lionagi.studio.app import app

_RECON = "lionagi.studio.services.lifecycle.run_startup_reconciliation"
_CKPT = "lionagi.studio.services.db_maintenance.checkpoint_state_db"


class _Gate:
    """A cross-thread, cancellable park point.

    The lifespan runs in TestClient's portal thread; the test thread releases
    via the captured loop. Parking on an asyncio.Event (not a thread-pool
    blocking wait) keeps the park cancellable, so a shutdown can tear it down.
    """

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
                pass  # loop already closed — warmup already settled


def test_health_serves_before_reconciliation_completes(monkeypatch):
    """/health answers while reconciliation is still parked — readiness did not
    wait on it. Reverting the defer (awaiting reconciliation before yield) makes
    TestClient entry hang here, so this is the regression guard."""
    gate = _Gate()

    async def _blocking_reconcile():
        await gate.wait()
        return {"reconciled": 0}

    async def _noop_ckpt(actor=None):
        return None

    monkeypatch.setattr(_RECON, _blocking_reconcile)
    monkeypatch.setattr(_CKPT, _noop_ckpt)

    try:
        with TestClient(app) as client:
            assert gate.entered.wait(timeout=5), "warmup never started reconciliation"
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
            gate.release()  # let warmup finish so shutdown is a no-op
    finally:
        gate.release()


def test_warmup_runs_reconciliation_then_checkpoint(monkeypatch):
    """The deferred work still happens: reconciliation runs, then the WAL
    checkpoint with actor='startup'."""
    recon_called = threading.Event()
    ckpt_called = threading.Event()
    order: list[str] = []
    seen: dict[str, object] = {}

    async def _spy_recon():
        order.append("reconcile")
        recon_called.set()
        return {"reconciled": 0}

    async def _spy_ckpt(actor=None):
        order.append("checkpoint")
        seen["actor"] = actor
        ckpt_called.set()

    monkeypatch.setattr(_RECON, _spy_recon)
    monkeypatch.setattr(_CKPT, _spy_ckpt)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert recon_called.wait(timeout=5), "reconciliation did not run"
        assert ckpt_called.wait(timeout=5), "checkpoint did not run"

    assert seen["actor"] == "startup"
    assert order[:2] == ["reconcile", "checkpoint"]


def test_shutdown_cancels_pending_warmup(monkeypatch):
    """A shutdown landing while warmup is parked cancels it and returns — no
    hang, no leaked task. The park is never released, so only cancellation can
    let the context manager exit."""
    gate = _Gate()

    async def _never_finishes():
        await gate.wait()
        return {"reconciled": 0}

    monkeypatch.setattr(_RECON, _never_finishes)

    with TestClient(app) as client:
        assert gate.entered.wait(timeout=5), "warmup never started reconciliation"
        assert client.get("/health").status_code == 200
    # Exiting the context ran shutdown while reconciliation was still parked.
    # Reaching here at all means _finalize_warmup cancelled it cleanly.
