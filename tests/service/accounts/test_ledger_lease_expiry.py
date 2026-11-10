# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An open reservation older than the ledger's lease is closed at zero by the next admission."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

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


async def closed_priced_hold(tmp_path):
    import json

    from lionagi.service.accounts._pricing import quote_ceiling
    from tests.service.accounts.test_ledger_settlement_evidence import priced_revision

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
    return ledger, held, priced, revision


@pytest.mark.anyio
async def test_late_stream_estimate_is_recorded_as_a_correction_without_a_block(tmp_path):
    from tests.service.accounts.test_ledger_settlement_evidence import estimate

    ledger, held, priced, revision = await closed_priced_hold(tmp_path)
    late = estimate(priced, revision, "stream_error", output_tokens=40)

    assert await ledger.settle(held.attempt_id, late) == "settled"
    assert await ledger.settle(held.attempt_id, late) == "already_settled"

    [(kind, attempt_id, delta, source_ref, _)] = corrections(ledger.path)
    assert (kind, attempt_id, delta) == ("correction", str(held.attempt_id), late.cost_nusd)
    assert "origin runtime" in source_ref
    assert block_reason(ledger.path) is None


@pytest.mark.anyio
async def test_a_mismatched_late_stream_estimate_is_refused_like_one_on_an_open_attempt(tmp_path):
    import dataclasses

    from tests.service.accounts.test_ledger_settlement_evidence import estimate

    ledger, held, priced, revision = await closed_priced_hold(tmp_path)
    late = dataclasses.replace(
        estimate(priced, revision, "stream_error", output_tokens=40), served_model="model-b"
    )

    with pytest.raises(ValueError, match="pricing inputs differ from reservation"):
        await ledger.settle(held.attempt_id, late)

    assert corrections(ledger.path) == [] and block_reason(ledger.path) is None


@pytest.mark.anyio
async def test_late_complete_usage_above_the_ceiling_is_a_correction_and_blocks_nothing(tmp_path):
    ledger, held = await closed_hold(tmp_path, ceiling=4)

    result = await ledger.settle(
        held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, 9, "late")
    )

    assert result == "settled"
    [(_, _, delta, _, _)] = corrections(ledger.path)
    assert delta == 9
    assert block_reason(ledger.path) is None


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["not_sent", "rejection_zero"])
async def test_late_typed_zero_evidence_agrees_with_the_closure_and_changes_nothing(tmp_path, kind):
    from lionagi.service.accounts._types import ProvenNotSent, RejectionZero

    ledger, held = await closed_hold(tmp_path)
    evidence = (
        ProvenNotSent()
        if kind == "not_sent"
        else RejectionZero(400, "invalid_request_error", None, "request_rejected")
    )

    assert await ledger.settle(held.attempt_id, evidence) == "already_settled"

    assert corrections(ledger.path) == [] and block_reason(ledger.path) is None
    assert attempt_row(ledger.path, held.attempt_id)[1] == 0


@pytest.mark.anyio
async def test_a_larger_late_arrival_after_an_estimate_records_the_larger_cost(tmp_path):
    from tests.service.accounts.test_ledger_settlement_evidence import estimate

    ledger, held, priced, revision = await closed_priced_hold(tmp_path)
    first = estimate(priced, revision, "stream_error", output_tokens=40)
    bill = first.cost_nusd + 4

    assert await ledger.settle(held.attempt_id, first) == "settled"
    assert (
        await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, bill, "late"))
        == "settled"
    )

    assert sorted(row[2] for row in corrections(ledger.path)) == sorted([first.cost_nusd, 4])
    assert (await ledger.status("anthropic", "2026-01"))[0].adjustments_nusd == bill
    assert block_reason(ledger.path) is None


@pytest.mark.anyio
async def test_a_late_cost_that_would_overflow_the_account_total_is_refused(tmp_path):
    from lionagi.service.accounts._types import INT64_MAX

    ledger, held = await closed_hold(
        tmp_path
    )  # a second hold of 1 is open, so INT64_MAX more cannot fit

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.settle(
            held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, INT64_MAX, "late")
        )

    assert caught.value.code == RouterErrorCode.BUDGET_EXHAUSTED
    assert corrections(ledger.path) == []
    assert (await ledger.status("anthropic", "2026-01"))[0].adjustments_nusd == 0  # still readable


@pytest.mark.anyio
async def test_the_first_late_correction_keeps_the_id_a_single_correction_was_written_under(
    tmp_path,
):
    ledger, held = await closed_hold(tmp_path)
    evidence = CompleteUsage("model-a", 1, 2, 3, 4, 5, 3, "late")
    namespace = UUID("5f0d3c0e-8a54-5c52-9d63-3b0b6c3f2a11")

    assert await ledger.settle(held.attempt_id, evidence) == "settled"

    with sqlite3.connect(ledger.path) as connection:
        ids = {
            row[0]
            for row in connection.execute("SELECT id FROM adjustment WHERE kind = 'correction'")
        }
    assert ids == {str(uuid5(namespace, f"lease-late:{held.attempt_id}"))}
    assert await ledger.settle(held.attempt_id, evidence) == "already_settled"


@pytest.mark.anyio
async def test_a_smaller_late_arrival_after_the_bill_writes_nothing_and_logs_the_drop(
    tmp_path, caplog
):
    from tests.service.accounts.test_ledger_settlement_evidence import estimate

    ledger, held, priced, revision = await closed_priced_hold(tmp_path)
    small = estimate(priced, revision, "stream_error", output_tokens=40)
    bill = small.cost_nusd + 4
    await ledger.settle(held.attempt_id, CompleteUsage("model-a", 1, 2, 3, 4, 5, bill, "late"))

    with caplog.at_level("INFO", logger="lionagi.service.accounts._ledger"):
        result = await ledger.settle(held.attempt_id, small)

    assert result == "already_settled"
    assert [row[2] for row in corrections(ledger.path)] == [bill]
    drops = [r.getMessage() for r in caplog.records if str(held.attempt_id) in r.getMessage()]
    assert len(drops) == 1 and str(small.cost_nusd) in drops[0] and str(bill) in drops[0]
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
