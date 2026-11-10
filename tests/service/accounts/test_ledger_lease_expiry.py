# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An open reservation older than the ledger's lease is closed at zero by the next admission."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    Quote,
    RouterErrorCode,
    Scope,
)

T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
LEASE = timedelta(hours=2)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def make_ledger(tmp_path, clock: Clock, **options) -> CredentialLedger:
    return CredentialLedger(tmp_path / "ledger.sqlite3", clock=clock, **options)


def spec(budget: int = 10) -> AccountSpec:
    return AccountSpec("alpha", "ALPHA_KEY", budget)


def quote(ceiling: int) -> Quote:
    return Quote("model-a", ceiling, "price-a", "a" * 64)


def scope(identity=None) -> Scope:
    return Scope("branch", identity or uuid4(), "anthropic")


def attempt_row(path, attempt_id):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT state, cost_nusd, outcome, request_id, settled_at FROM attempt WHERE id = ?",
            (str(attempt_id),),
        ).fetchone()


def adjustments(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT kind, attempt_id, delta_nusd, source_ref, month_utc FROM adjustment ORDER BY id"
        ).fetchall()


@pytest.mark.anyio
async def test_an_expired_hold_is_closed_at_zero_and_frees_the_budget(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    held = await ledger.reserve(scope(), uuid4(), quote(8), (spec(),))
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 8

    clock.now = T0 + LEASE + timedelta(seconds=1)
    fresh = await ledger.reserve(scope(), uuid4(), quote(8), (spec(),))

    assert fresh.account == "alpha"
    state, cost, outcome, request_id, settled_at = attempt_row(ledger.path, held.attempt_id)
    assert (state, cost, outcome, request_id) == ("settled", 0, "reconciled", None)
    assert settled_at == clock.now.isoformat()
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.settled_nusd, status.open_ceiling_nusd, status.open_attempts) == (0, 8, 1)
    [(kind, attempt_id, delta, source_ref, month)] = adjustments(ledger.path)
    assert (kind, attempt_id, delta, month) == ("reconciliation", str(held.attempt_id), 0, "2026-01")
    assert f"attempt {held.attempt_id}" in source_ref
    assert "open 7201s, lease 7200s" in source_ref
    assert "later settled attempt on this scope: no" in source_ref


@pytest.mark.anyio
async def test_a_lease_longer_than_the_hold_keeps_the_budget_held(tmp_path):
    # The control for the test above: with no expiry in reach the same second reservation is refused.
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock, open_lease=timedelta(days=30))
    await ledger.reserve(scope(), uuid4(), quote(8), (spec(),))
    clock.now = T0 + LEASE + timedelta(seconds=1)

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve(scope(), uuid4(), quote(8), (spec(),))

    assert caught.value.code == RouterErrorCode.BUDGET_EXHAUSTED
    assert adjustments(ledger.path) == []


@pytest.mark.anyio
async def test_a_hold_inside_the_lease_is_untouched(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    held = await ledger.reserve(scope(), uuid4(), quote(3), (spec(),))
    clock.now = T0 + LEASE  # exactly at the lease is still live
    await ledger.reserve(scope(), uuid4(), quote(3), (spec(),))

    assert attempt_row(ledger.path, held.attempt_id)[0] == "open"
    assert adjustments(ledger.path) == []


@pytest.mark.anyio
async def test_the_closure_names_a_later_settled_attempt_on_the_same_scope(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    shared = scope()
    held = await ledger.reserve(shared, uuid4(), quote(2), (spec(),))
    clock.now = T0 + timedelta(minutes=10)
    retry = await ledger.reserve(shared, uuid4(), quote(2), (spec(),))
    assert await ledger.settle(retry.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 1, "r1")) == "settled"

    clock.now = T0 + LEASE + timedelta(minutes=1)
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))

    [(_, attempt_id, _, source_ref, _)] = adjustments(ledger.path)
    assert attempt_id == str(held.attempt_id)
    assert "later settled attempt on this scope: yes" in source_ref


@pytest.mark.anyio
async def test_a_closed_hold_is_closed_once_and_a_late_settlement_cannot_reopen_it(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    held = await ledger.reserve(scope(), uuid4(), quote(4), (spec(),))
    clock.now = T0 + LEASE + timedelta(minutes=5)
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))

    assert len(adjustments(ledger.path)) == 1
    # A settlement arriving after the closure meets a settled row, so it is a conflict that blocks
    # the account until an operator looks, exactly as for any reconciled row.
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 3, "late"))
    assert caught.value.code == RouterErrorCode.SETTLEMENT_CONFLICT
    assert attempt_row(ledger.path, held.attempt_id)[1] == 0
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT block_reason FROM account WHERE name = 'alpha'").fetchone() == (
            "settlement_conflict",
        )


@pytest.mark.anyio
async def test_only_the_current_month_is_read(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    old = await ledger.reserve(scope(), uuid4(), quote(2), (spec(),))
    clock.now = datetime(2026, 2, 10, tzinfo=timezone.utc)
    await ledger.reserve(scope(), uuid4(), quote(2), (spec(),))

    assert attempt_row(ledger.path, old.attempt_id)[0] == "open"
    assert adjustments(ledger.path) == []


def test_a_lease_must_be_a_positive_duration(tmp_path):
    for bad in (timedelta(0), timedelta(seconds=-1), 7200):
        with pytest.raises(ValueError):
            CredentialLedger(tmp_path / "x.sqlite3", open_lease=bad)
