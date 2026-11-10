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
    assert (kind, attempt_id, delta, month) == (
        "reconciliation",
        str(held.attempt_id),
        0,
        "2026-01",
    )
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
    assert (
        await ledger.settle(retry.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 1, "r1"))
        == "settled"
    )

    clock.now = T0 + LEASE + timedelta(minutes=1)
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))

    [(_, attempt_id, _, source_ref, _)] = adjustments(ledger.path)
    assert attempt_id == str(held.attempt_id)
    assert "later settled attempt on this scope: yes" in source_ref


@pytest.mark.anyio
async def test_a_hold_is_closed_once(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    await ledger.reserve(scope(), uuid4(), quote(4), (spec(),))
    clock.now = T0 + LEASE + timedelta(minutes=5)
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))

    assert len(adjustments(ledger.path)) == 1


def block_reason(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT block_reason FROM account WHERE name = 'alpha'"
        ).fetchone()[0]


async def closed_hold(tmp_path, ceiling: int = 4):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    held = await ledger.reserve(scope(), uuid4(), quote(ceiling), (spec(),))
    clock.now = T0 + LEASE + timedelta(minutes=5)
    await ledger.reserve(scope(), uuid4(), quote(1), (spec(),))
    return ledger, held


def corrections(path):
    return [row for row in adjustments(path) if row[0] == "correction"]


@pytest.mark.anyio
async def test_late_complete_usage_is_recorded_as_a_correction_without_a_block(tmp_path):
    ledger, held = await closed_hold(tmp_path)

    first = await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 3, "late"))
    again = await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 3, "late"))

    assert (first, again) == ("settled", "already_settled")
    [(kind, attempt_id, delta, source_ref, month)] = corrections(ledger.path)
    assert (kind, attempt_id, delta, month) == ("correction", str(held.attempt_id), 3, "2026-01")
    assert f"attempt {held.attempt_id}" in source_ref and "origin runtime" in source_ref
    assert "lease expiry" in source_ref
    assert block_reason(ledger.path) is None
    assert attempt_row(ledger.path, held.attempt_id)[1] == 0
    assert (await ledger.status("anthropic", "2026-01"))[0].adjustments_nusd == 3


@pytest.mark.anyio
async def test_late_stream_estimate_is_recorded_as_a_correction_without_a_block(tmp_path):
    import json

    from lionagi.service.accounts._pricing import quote_ceiling
    from tests.service.accounts.test_ledger_settlement_evidence import estimate, priced_revision

    revision = priced_revision()
    priced = quote_ceiling(
        revision,
        json.dumps(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 1024,
                "service_tier": "standard_only",
            }
        ).encode(),
    )
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock, price_revisions={revision.id: revision})
    big = AccountSpec("alpha", "ALPHA_KEY", 100_000)
    held = await ledger.reserve(scope(), uuid4(), priced, (big,))
    clock.now = T0 + LEASE + timedelta(minutes=5)
    await ledger.reserve(scope(), uuid4(), quote(1), (big,))
    late = estimate(priced, revision, "stream_error", output_tokens=40)

    assert await ledger.settle(held.attempt_id, late) == "settled"
    assert await ledger.settle(held.attempt_id, late) == "already_settled"

    [(kind, attempt_id, delta, source_ref, _)] = corrections(ledger.path)
    assert (kind, attempt_id, delta) == ("correction", str(held.attempt_id), late.cost_nusd)
    assert "origin runtime" in source_ref
    assert block_reason(ledger.path) is None


@pytest.mark.anyio
async def test_late_reason_only_evidence_stays_already_settled(tmp_path):
    from lionagi.service.accounts._types import UnknownOutcome

    ledger, held = await closed_hold(tmp_path)

    assert await ledger.settle(held.attempt_id, UnknownOutcome("timeout")) == "already_settled"
    assert corrections(ledger.path) == [] and block_reason(ledger.path) is None


@pytest.mark.anyio
async def test_an_operator_reconciled_row_keeps_the_conflict_rule(tmp_path):
    ledger = make_ledger(tmp_path, Clock(T0))
    held = await ledger.reserve(scope(), uuid4(), quote(4), (spec(),))
    await ledger.reconcile(uuid4(), held.attempt_id, 0, "operator closure")

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 3, "late"))

    assert caught.value.code == RouterErrorCode.SETTLEMENT_CONFLICT
    assert block_reason(ledger.path) == "settlement_conflict"
    assert corrections(ledger.path) == []


@pytest.mark.anyio
async def test_a_refused_admission_closes_nothing(tmp_path):
    clock = Clock(T0)
    ledger = make_ledger(tmp_path, clock)
    held = await ledger.reserve(scope(), uuid4(), quote(4), (spec(budget=10),))
    clock.now = T0 + LEASE + timedelta(minutes=5)

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve(scope(), uuid4(), quote(11), (spec(budget=10),))

    assert caught.value.code == RouterErrorCode.BUDGET_EXHAUSTED
    assert attempt_row(ledger.path, held.attempt_id)[0] == "open"
    assert adjustments(ledger.path) == []


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
