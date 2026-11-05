# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The credential ledger's bounded wait for a worker thread, and its timing record."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import anyio
import pytest

from lionagi.service.accounts import _ledger as ledger_module
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    Quote,
    Reservation,
    RouterErrorCode,
    Scope,
)

_LOGGER_NAME = ledger_module.__name__
_BOUND_S = 0.2
_MARGIN_S = 1.5
_TEST_DEADLINE_S = 3.0


def make_ledger(tmp_path, **options) -> CredentialLedger:
    return CredentialLedger(
        tmp_path / "ledger.sqlite3",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        **options,
    )


def ledger_events(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == logging.DEBUG
    ]


async def _hold_worker(release: threading.Event) -> None:
    await anyio.to_thread.run_sync(release.wait, 30)


@asynccontextmanager
async def saturated_default_limiter():
    """Hold every token of AnyIO's default thread limiter until the block ends."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    release = threading.Event()
    try:
        async with anyio.create_task_group() as group:
            for _ in range(int(limiter.total_tokens)):
                group.start_soon(_hold_worker, release)
            with anyio.fail_after(5):
                while limiter.borrowed_tokens < limiter.total_tokens:
                    await anyio.sleep(0.01)
            try:
                yield
            finally:
                release.set()
    finally:
        release.set()


async def refused_by_admission_bound(ledger: CredentialLedger) -> AccountRoutingError:
    """Make a shielded ledger call while the worker pool is full and return its error."""
    with anyio.CancelScope(shield=True):
        with anyio.fail_after(_TEST_DEADLINE_S):
            with pytest.raises(AccountRoutingError) as caught:
                await ledger.status("anthropic", "2026-01")
    return caught.value


@pytest.mark.anyio
async def test_saturated_worker_pool_raises_ledger_busy_within_bound(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")
    monkeypatch.setattr(ledger_module, "_ADMISSION_BOUND_S", _BOUND_S, raising=False)

    async with saturated_default_limiter():
        started = time.monotonic()
        error = await refused_by_admission_bound(ledger)
        elapsed = time.monotonic() - started

    assert error.code is RouterErrorCode.LEDGER_BUSY
    assert not isinstance(error, TimeoutError)
    assert elapsed < _BOUND_S + _MARGIN_S


@pytest.mark.anyio
async def test_operation_admitted_before_bound_returns_its_commit(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")
    monkeypatch.setattr(ledger_module, "_ADMISSION_BOUND_S", _BOUND_S, raising=False)
    original_connect = CredentialLedger._connect

    def slow_connect(self):
        time.sleep(_BOUND_S * 3)
        return original_connect(self)

    monkeypatch.setattr(CredentialLedger, "_connect", slow_connect)
    scope = Scope("branch", uuid4(), "anthropic")

    reservation = await ledger.reserve(
        scope,
        uuid4(),
        Quote("model-a", 1, "price-a", "a" * 64),
        (AccountSpec("alpha", "ALPHA_KEY", 10),),
    )
    monkeypatch.undo()

    assert isinstance(reservation, Reservation)
    status = await ledger.status("anthropic", "2026-01")
    assert (status[0].open_attempts, status[0].open_ceiling_nusd) == (1, 1)


@pytest.mark.anyio
async def test_committed_call_emits_one_timing_event(tmp_path, caplog):
    ledger = make_ledger(tmp_path)
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        await ledger.status("anthropic", "2026-01")

    events = ledger_events(caplog)
    assert len(events) == 1
    assert events[0].outcome == "committed"
    assert events[0].admission_wait_s >= 0
    assert events[0].sqlite_wait_s >= 0


@pytest.mark.anyio
async def test_admission_trip_emits_one_event_without_sqlite_wait(tmp_path, monkeypatch, caplog):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")
    monkeypatch.setattr(ledger_module, "_ADMISSION_BOUND_S", _BOUND_S, raising=False)

    async with saturated_default_limiter():
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            await refused_by_admission_bound(ledger)

    events = ledger_events(caplog)
    assert len(events) == 1
    assert events[0].outcome == "ledger_busy"
    assert events[0].admission_wait_s >= _BOUND_S / 2
    assert not hasattr(events[0], "sqlite_wait_s")


@pytest.mark.anyio
async def test_sqlite_lock_timeout_emits_one_event_with_sqlite_wait(tmp_path, caplog):
    ledger = make_ledger(tmp_path, busy_timeout_s=0.2)
    await ledger.status("anthropic", "2026-01")
    lock = sqlite3.connect(tmp_path / "ledger.sqlite3", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    caplog.clear()
    try:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(AccountRoutingError) as caught:
                await ledger.reserve(
                    Scope("branch", uuid4(), "anthropic"),
                    uuid4(),
                    Quote("model-a", 1, "price-a", "a" * 64),
                    (AccountSpec("alpha", "ALPHA_KEY", 10),),
                )
    finally:
        lock.execute("ROLLBACK")
        lock.close()

    assert caught.value.code is RouterErrorCode.LEDGER_BUSY
    events = ledger_events(caplog)
    assert len(events) == 1
    assert events[0].outcome == "ledger_busy"
    assert events[0].sqlite_wait_s >= 0.1


@pytest.mark.anyio
async def test_caller_cancelled_while_operation_runs_still_commits(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")
    original_connect = CredentialLedger._connect

    def slow_connect(self):
        time.sleep(_BOUND_S * 3)
        return original_connect(self)

    monkeypatch.setattr(CredentialLedger, "_connect", slow_connect)
    returned: list[Reservation] = []

    with anyio.fail_after(_TEST_DEADLINE_S):
        with anyio.move_on_after(_BOUND_S / 2) as caller_scope:
            returned.append(
                await ledger.reserve(
                    Scope("branch", uuid4(), "anthropic"),
                    uuid4(),
                    Quote("model-a", 1, "price-a", "a" * 64),
                    (AccountSpec("alpha", "ALPHA_KEY", 10),),
                )
            )
            await anyio.sleep(_TEST_DEADLINE_S)
    monkeypatch.undo()

    # The write committed and its result came back, and the caller's cancellation
    # was still delivered at its next checkpoint.
    assert len(returned) == 1
    assert caller_scope.cancelled_caught
    status = await ledger.status("anthropic", "2026-01")
    assert (status[0].open_attempts, status[0].open_ceiling_nusd) == (1, 1)


@pytest.mark.anyio
async def test_caller_cancelled_while_waiting_for_a_worker_never_runs_the_operation(tmp_path):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")

    async with saturated_default_limiter():
        with anyio.fail_after(_TEST_DEADLINE_S):
            with anyio.move_on_after(_BOUND_S) as caller_scope:
                await ledger.reserve(
                    Scope("branch", uuid4(), "anthropic"),
                    uuid4(),
                    Quote("model-a", 1, "price-a", "a" * 64),
                    (AccountSpec("alpha", "ALPHA_KEY", 10),),
                )

    # The caller's own cancellation surfaced, not a ledger_busy refusal, and once
    # the worker pool is free again the abandoned operation still has not run.
    assert caller_scope.cancelled_caught
    await anyio.sleep(_BOUND_S)
    status = await ledger.status("anthropic", "2026-01")
    assert sum(item.open_attempts for item in status) == 0


@pytest.mark.anyio
async def test_bound_cancellation_delivered_after_admission_still_returns_the_commit(
    tmp_path, monkeypatch
):
    ledger = make_ledger(tmp_path)
    await ledger.status("anthropic", "2026-01")
    monkeypatch.setattr(ledger_module, "_ADMISSION_BOUND_S", _BOUND_S, raising=False)
    original_connect = CredentialLedger._connect
    real_run_sync = ledger_module.run_sync

    def slow_connect(self):
        time.sleep(_BOUND_S * 3)
        return original_connect(self)

    async def run_sync_with_late_checkpoint(function, *args, **kwargs):
        # The worker is admitted and still running when the bound fires; the bound's
        # cancellation is then delivered at the first checkpoint after the worker is done.
        result = await real_run_sync(function, *args, **kwargs)
        await anyio.sleep(0)
        return result

    monkeypatch.setattr(CredentialLedger, "_connect", slow_connect)
    monkeypatch.setattr(ledger_module, "run_sync", run_sync_with_late_checkpoint)

    with anyio.fail_after(_TEST_DEADLINE_S):
        reservation = await ledger.reserve(
            Scope("branch", uuid4(), "anthropic"),
            uuid4(),
            Quote("model-a", 1, "price-a", "a" * 64),
            (AccountSpec("alpha", "ALPHA_KEY", 10),),
        )
    monkeypatch.undo()

    # The operation had started, so its commit is returned, not a ledger_busy refusal.
    assert isinstance(reservation, Reservation)
    status = await ledger.status("anthropic", "2026-01")
    assert (status[0].open_attempts, status[0].open_ceiling_nusd) == (1, 1)
