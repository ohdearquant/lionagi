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


def _failures(ledger):
    with closing(sqlite3.connect(ledger.path)) as connection:
        return connection.execute(
            """
            SELECT attempt_id, provider, account, recorded_at, http_status, provider_error_type
            FROM probe_failure ORDER BY seq
            """
        ).fetchall()


def _block_state(ledger):
    with closing(sqlite3.connect(ledger.path)) as connection:
        return connection.execute(
            "SELECT block_reason, block_generation FROM account WHERE provider = ? AND name = ?",
            ("anthropic", ACCOUNT.name),
        ).fetchone()


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
    assert _failures(ledger) == []
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
    failures = [(str(attempt), "anthropic", ACCOUNT.name, NOW.isoformat(), status, error_type)]
    assert _failures(ledger) == failures

    evidence = RejectionZero(status, error_type, None, reason)
    assert not await ledger.settle_probe(attempt, evidence, http_status=status)
    assert not await ledger.settle_probe(attempt, evidence, http_status=status)
    row = _attempt(ledger, attempt)
    assert row[:7] == ("settled", 0, "rejection_zero", status, error_type, None, None)
    details = json.loads(row[7])
    assert details["first_failure"]["rejection_class"] is None
    assert details["evidence"]["rejection_class"] is None
    assert (await ledger.status("anthropic"))[0].block_reason == "credit_balance_low"
    assert _failures(ledger) == failures
    assert _unblocks(ledger) == []


async def test_probe_no_response_records_failure_at_zero_when_unsent(tmp_path):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert not await ledger.settle_probe(
        attempt, ProvenNotSent(caller_cancelled=True), http_status=None
    )
    assert _attempt(ledger, attempt)[:7] == ("settled", 0, "not_sent", None, None, None, None)
    assert (await ledger.status("anthropic"))[0].block_reason == "credit_balance_low"
    assert _failures(ledger) == [
        (str(attempt), "anthropic", ACCOUNT.name, NOW.isoformat(), None, None)
    ]
    assert _unblocks(ledger) == []


async def test_probe_upgrades_version_one_and_preserves_bound_billing_diagnostic(tmp_path):
    ledger = await _prepare(tmp_path, block=None)
    bound_scope = Scope("branch", uuid4(), "anthropic")
    seed = uuid4()
    await ledger.reserve(bound_scope, seed, QUOTE, (ACCOUNT,))
    await ledger.settle(seed, ProvenNotSent())
    await ledger.block("anthropic", ACCOUNT.name, "credit_balance_low")
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("DROP TABLE probe_failure")
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)

    ledger = CredentialLedger(ledger.path, clock=lambda: NOW)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    held = UnknownOutcome("provider_error")
    for _ in range(2):
        assert not await ledger.settle_probe(
            attempt, held, http_status=503, provider_error_type="api_error"
        )
    assert _attempt(ledger, attempt)[:6] == ("open", None, "provider_error", 503, "api_error", None)
    assert _failures(ledger) == [
        (str(attempt), "anthropic", ACCOUNT.name, NOW.isoformat(), 503, "api_error")
    ]
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.open_ceiling_nusd) == ("credit_balance_low", 10)

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve(bound_scope, uuid4(), QUOTE, (ACCOUNT,))
    assert caught.value.code is RouterErrorCode.ACCOUNT_BLOCKED
    assert caught.value.billing_block == "credit_balance_low"
    with closing(sqlite3.connect(ledger.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


async def test_probe_success_preserves_a_newer_same_status_failure_in_the_same_minute(tmp_path):
    ledger = await _prepare(tmp_path)
    initial, stale, newer = uuid4(), uuid4(), uuid4()
    rejection = RejectionZero(529, "overloaded_error", None, "provider_error")

    await ledger.reserve_probe(ACCOUNT, initial, QUOTE)
    assert not await ledger.settle_probe(initial, rejection, http_status=529)
    assert (await ledger.status("anthropic"))[0].block_reason == "credit_balance_low"
    await ledger.reserve_probe(ACCOUNT, stale, QUOTE)
    await ledger.reserve_probe(ACCOUNT, newer, QUOTE)

    assert not await ledger.settle_probe(newer, rejection, http_status=529)
    assert not await ledger.settle_probe(stale, _usage(3), http_status=200)

    assert _attempt(ledger, stale)[:4] == ("settled", 3, "complete", 200)
    status = (await ledger.status("anthropic"))[0]
    assert status.block_reason == "credit_balance_low"
    assert _failures(ledger) == [
        (str(attempt), "anthropic", ACCOUNT.name, NOW.isoformat(), 529, "overloaded_error")
        for attempt in (initial, newer)
    ]
    assert _unblocks(ledger) == []

    retry = uuid4()
    await ledger.reserve_probe(ACCOUNT, retry, QUOTE)
    assert await ledger.settle_probe(retry, _usage(3), http_status=200)
    assert _unblocks(ledger) == [(f"probe:{retry}",)]


async def test_probe_failure_after_a_winning_success_is_recorded_without_reblocking(tmp_path):
    ledger = await _prepare(tmp_path)
    winner, failure = uuid4(), uuid4()
    await ledger.reserve_probe(ACCOUNT, winner, QUOTE)
    await ledger.reserve_probe(ACCOUNT, failure, QUOTE)
    assert await ledger.settle_probe(winner, _usage(3), http_status=200)
    assert not await ledger.settle_probe(
        failure,
        UnknownOutcome("provider_error"),
        http_status=503,
        provider_error_type="api_error",
    )
    assert (await ledger.status("anthropic"))[0].block_reason is None
    assert _unblocks(ledger) == [(f"probe:{winner}",)]
    assert _failures(ledger) == [
        (str(failure), "anthropic", ACCOUNT.name, NOW.isoformat(), 503, "api_error")
    ]


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


@pytest.mark.parametrize("refresh", ["untouched", "operator", "billing"])
async def test_probe_distinguishes_a_block_refresh_that_keeps_the_reason(tmp_path, refresh):
    ledger = await _prepare(tmp_path, block=None)
    ordinary_attempt = uuid4()
    await ledger.reserve(Scope("branch", uuid4(), "anthropic"), ordinary_attempt, QUOTE, (ACCOUNT,))
    await ledger.block("anthropic", ACCOUNT.name, "auth_required")
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    _, generation = _block_state(ledger)

    if refresh == "operator":
        await ledger.block("anthropic", ACCOUNT.name, "auth_required")
    elif refresh == "billing":
        evidence = RejectionZero(
            400, "invalid_request_error", "credit_balance_low", "request_rejected"
        )
        assert (await ledger.settle_billing_rejection(ordinary_attempt, evidence))[1] == (
            "auth_required"
        )
    else:
        await ledger.settle(ordinary_attempt, ProvenNotSent())

    cleared = refresh == "untouched"
    assert _block_state(ledger) == ("auth_required", generation + (not cleared))
    assert (await ledger.settle_probe(attempt, _usage(3), http_status=200)) is cleared
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd) == (
        None if cleared else "auth_required",
        3,
    )
    assert _unblocks(ledger) == ([(f"probe:{attempt}",)] if cleared else [])


async def test_probe_without_a_generation_preserves_the_block_and_records_usage(tmp_path):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    snapshot = json.loads(_attempt(ledger, attempt)[7])
    with closing(sqlite3.connect(ledger.path)) as connection, connection:
        connection.execute(
            "UPDATE attempt SET settlement_evidence_json = ? WHERE id = ?",
            (json.dumps(snapshot[:4]), str(attempt)),
        )

    assert not await ledger.settle_probe(
        attempt, UnknownOutcome("usage_incomplete"), http_status=200
    )
    assert (await ledger.status("anthropic"))[0].open_ceiling_nusd == 10
    assert not await ledger.settle_probe(attempt, _usage(3), http_status=200)
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd, status.open_ceiling_nusd) == (
        "credit_balance_low",
        3,
        0,
    )
    assert _unblocks(ledger) == []


@pytest.mark.parametrize("new_block", [None, "settlement_conflict"])
async def test_probe_usage_replay_applies_ceiling_safety_once(tmp_path, new_block):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, UnknownOutcome("usage_incomplete"), http_status=200)
    assert (await ledger.status("anthropic"))[0].block_reason is None

    if new_block is not None:
        await ledger.block("anthropic", ACCOUNT.name, new_block)
    _, generation = _block_state(ledger)
    assert not await ledger.settle_probe(attempt, _usage(11), http_status=200)
    assert _block_state(ledger) == ("ceiling_exceeded", generation + 1)
    assert not await ledger.settle_probe(attempt, _usage(11), http_status=200)
    assert _block_state(ledger) == ("ceiling_exceeded", generation + 1)
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd, status.open_ceiling_nusd) == (
        "ceiling_exceeded",
        11,
        0,
    )
    assert _failures(ledger) == []
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
