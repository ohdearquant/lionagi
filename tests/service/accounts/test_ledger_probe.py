# SPDX-License-Identifier: Apache-2.0
"""SQLite admission and disposition for account probes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Quote,
    RejectionZero,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 16, 14, 5, tzinfo=timezone.utc)
ACCOUNT = AccountSpec("alpha", "ALPHA_KEY", 100)
QUOTE = Quote("model-a", 10, "price-a", "a" * 64)


async def _prepare(tmp_path, *, block: str | None = "credit_balance_low"):
    ledger = CredentialLedger(tmp_path / "ledger.sqlite3", clock=lambda: NOW)
    seed = uuid4()
    await ledger.reserve(Scope("branch", uuid4(), "anthropic"), seed, QUOTE, (ACCOUNT,))
    await ledger.settle(seed, ProvenNotSent())
    if block is not None:
        await ledger.block("anthropic", ACCOUNT.name, block)
    return ledger


def _usage(cost: int) -> CompleteUsage:
    return CompleteUsage("model-a", 1, 0, 0, 0, 1, cost, "excluded-request-id")


def _attempt(ledger, attempt):
    with closing(sqlite3.connect(ledger.path)) as connection:
        return connection.execute(
            """
            SELECT state, cost_nusd, outcome, http_status, provider_error_type, rejection_class,
                   request_id, settlement_evidence_json
            FROM attempt WHERE id = ?
            """,
            (str(attempt),),
        ).fetchone()


def _unblocks(ledger):
    with closing(sqlite3.connect(ledger.path)) as connection:
        return connection.execute(
            "SELECT source_ref FROM adjustment WHERE kind = 'unblock' ORDER BY rowid"
        ).fetchall()


async def test_probe_requires_current_block_and_available_budget(tmp_path):
    ledger = await _prepare(tmp_path, block=None)
    attempt = uuid4()
    with pytest.raises(ValueError, match="reserve_probe"):
        await ledger.reserve(Scope("probe", attempt, "anthropic"), attempt, QUOTE, (ACCOUNT,))
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert caught.value.code is RouterErrorCode.PROBE_ACCOUNT_NOT_BLOCKED
    assert _attempt(ledger, attempt) is None

    open_cost = uuid4()
    await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"),
        open_cost,
        Quote("model-a", 91, "price-a", "b" * 64),
        (ACCOUNT,),
    )
    await ledger.block("anthropic", ACCOUNT.name, "credit_balance_low")
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert caught.value.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert _attempt(ledger, attempt) is None

    await ledger.settle(open_cost, ProvenNotSent())
    reservation = await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert reservation.scope == Scope("probe", attempt, "anthropic")
    with closing(sqlite3.connect(ledger.path)) as connection:
        assert connection.execute("SELECT scope_kind FROM binding").fetchall() == [
            ("branch",),
            ("branch",),
        ]
    with pytest.raises(ValueError, match="reservation kind"):
        await ledger.settle(attempt, _usage(1), http_status=200)
    with pytest.raises(ValueError, match="reservation kind"):
        await ledger.settle_probe(open_cost, ProvenNotSent(), http_status=None)
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve(Scope("branch", uuid4(), "anthropic"), uuid4(), QUOTE, (ACCOUNT,))
    assert caught.value.code is RouterErrorCode.ACCOUNT_BLOCKED


async def test_probe_settles_and_clears_atomically_once(tmp_path, monkeypatch):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)

    def fail_unblock(*args, **kwargs):
        raise RuntimeError("injected unblock write failure")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_insert_adjustment", fail_unblock)
        with pytest.raises(RuntimeError, match="injected unblock"):
            await ledger.settle_probe(attempt, _usage(3), http_status=200)
    assert _attempt(ledger, attempt)[:7] == ("open", None, None, None, None, None, None)
    assert (await ledger.status("anthropic"))[0].block_reason == "credit_balance_low"
    assert _unblocks(ledger) == []

    assert await ledger.settle_probe(attempt, _usage(3), http_status=200)
    assert not await ledger.settle_probe(attempt, _usage(3), http_status=200)
    assert _attempt(ledger, attempt)[:7] == ("settled", 3, "complete", 200, None, None, None)
    assert _unblocks(ledger) == [(f"probe:{attempt}",)]
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd, status.open_ceiling_nusd) == (None, 3, 0)


@pytest.mark.parametrize(
    "status, error_type, reason",
    [
        (400, "invalid_request_error", "request_rejected"),
        (529, "overloaded_error", "provider_error"),
    ],
)
async def test_probe_typed_zero_records_type_but_no_rejection_class(
    tmp_path, status, error_type, reason
):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    first = (
        UnknownOutcome(reason, error_type, "credit_balance_low")
        if status == 400
        else (UnknownOutcome(reason))
    )
    assert not await ledger.settle_probe(
        attempt, first, http_status=status, provider_error_type=error_type
    )
    assert _attempt(ledger, attempt)[:7] == ("open", None, reason, status, error_type, None, None)

    evidence = RejectionZero(status, error_type, None, reason)
    assert not await ledger.settle_probe(attempt, evidence, http_status=status)
    assert not await ledger.settle_probe(attempt, evidence, http_status=status)
    row = _attempt(ledger, attempt)
    assert row[:7] == ("settled", 0, "rejection_zero", status, error_type, None, None)
    details = json.loads(row[7])
    assert details["first_failure"]["rejection_class"] is None
    assert details["evidence"]["rejection_class"] is None
    assert (await ledger.status("anthropic"))[0].block_reason == f"probe_{status}_20261116t1405z"
    assert _unblocks(ledger) == []


async def test_probe_no_response_refreshes_the_block_at_zero_when_unsent(tmp_path):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert not await ledger.settle_probe(
        attempt, ProvenNotSent(caller_cancelled=True), http_status=None
    )
    assert _attempt(ledger, attempt)[:7] == ("settled", 0, "not_sent", None, None, None, None)
    assert (await ledger.status("anthropic"))[0].block_reason == "probe_no_response_20261116t1405z"
    assert _unblocks(ledger) == []


async def test_probe_success_missing_usage_clears_block_but_keeps_ceiling(tmp_path):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, UnknownOutcome("usage_incomplete"), http_status=200)
    marked = (await ledger.status("anthropic"))[0]
    assert (marked.block_reason, marked.open_ceiling_nusd) == (None, 10)
    await ledger.block("anthropic", ACCOUNT.name, "settlement_conflict")
    assert not await ledger.settle_probe(attempt, _usage(3), http_status=200)
    assert not await ledger.settle_probe(attempt, _usage(3), http_status=200)
    settled = (await ledger.status("anthropic"))[0]
    assert (settled.block_reason, settled.open_ceiling_nusd, settled.settled_nusd) == (
        "settlement_conflict",
        0,
        3,
    )
    assert _unblocks(ledger) == [(f"probe:{attempt}",)]


@pytest.mark.parametrize("update", ["different_block", "reblock_after_clear", "ceiling_exceeded"])
async def test_probe_does_not_clear_newer_or_stronger_block(tmp_path, update):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    block = "credit_balance_low"
    cost = 3
    audit = []
    if update == "different_block":
        block = "settlement_conflict"
        await ledger.block("anthropic", ACCOUNT.name, block)
    elif update == "reblock_after_clear":
        await ledger.clear_block(uuid4(), "anthropic", ACCOUNT.name, "operator-reviewed")
        await ledger.block("anthropic", ACCOUNT.name, block)
        audit = [("operator-reviewed",)]
    else:
        block = "ceiling_exceeded"
        cost = 11
    assert not await ledger.settle_probe(attempt, _usage(cost), http_status=200)
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd) == (block, cost)
    assert _unblocks(ledger) == audit
