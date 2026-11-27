# SPDX-License-Identifier: Apache-2.0
"""SQLite admission and disposition for account probes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

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
from lionhub.providers.anthropic.accounts._ledger import CredentialLedger

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 16, 14, 5, tzinfo=UTC)
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


@pytest.mark.parametrize("reason", ["ceiling_exceeded", "settlement_conflict"])
async def test_probe_refuses_nonbilling_blocks_without_reserving(tmp_path, reason):
    ledger = await _prepare(tmp_path, block=reason)
    before = _block_state(ledger)
    attempt = uuid4()

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)

    assert caught.value.code is RouterErrorCode.PROBE_BLOCK_NOT_BILLING
    assert caught.value.block_reason == reason
    assert caught.value.billing_block is None
    assert _block_state(ledger) == before
    assert _attempt(ledger, attempt) is None
    assert _unblocks(ledger) == []
    assert _failures(ledger) == []


@pytest.mark.parametrize("reason", ["credit_balance_low", "spend_limit_reached"])
async def test_probe_reserves_and_clears_a_billing_block(tmp_path, reason):
    ledger = await _prepare(tmp_path, block=reason)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, _usage(3), http_status=200)
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd) == (None, 3)
    assert _unblocks(ledger) == [(f"probe:{attempt}",)]


async def test_probe_does_not_clear_an_intervening_ceiling_block(tmp_path):
    ledger = await _prepare(tmp_path, block=None)
    ordinary_attempt = uuid4()
    await ledger.reserve(Scope("branch", uuid4(), "anthropic"), ordinary_attempt, QUOTE, (ACCOUNT,))
    await ledger.block("anthropic", ACCOUNT.name, "credit_balance_low")
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    await ledger.settle(ordinary_attempt, _usage(11))
    before = _block_state(ledger)
    assert before[0] == "ceiling_exceeded"

    assert not await ledger.settle_probe(attempt, _usage(3), http_status=200)
    assert _block_state(ledger) == before
    assert _attempt(ledger, attempt)[:4] == ("settled", 3, "complete", 200)
    assert _unblocks(ledger) == []
    assert _failures(ledger) == []


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
    reason = "credit_balance_low"
    await ledger.block("anthropic", ACCOUNT.name, reason)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    _, generation = _block_state(ledger)

    if refresh == "operator":
        await ledger.block("anthropic", ACCOUNT.name, reason)
    elif refresh == "billing":
        evidence = RejectionZero(
            400, "invalid_request_error", "spend_limit_reached", "request_rejected"
        )
        assert (await ledger.settle_billing_rejection(ordinary_attempt, evidence))[1] == reason
    else:
        await ledger.settle(ordinary_attempt, ProvenNotSent())

    cleared = refresh == "untouched"
    assert _block_state(ledger) == (reason, generation + (not cleared))
    assert (await ledger.settle_probe(attempt, _usage(3), http_status=200)) is cleared
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd) == (
        None if cleared else reason,
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


async def test_a_bill_after_the_lease_closed_the_probe_records_spend_without_a_block(tmp_path):
    now = [NOW]
    ledger = await _prepare(tmp_path)
    ledger._clock = lambda: now[0]
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, UnknownOutcome("usage_incomplete"), http_status=200)
    assert _block_state(ledger)[0] is None

    # The next admission after the lease closes the open probe hold at zero.
    now[0] = NOW + ledger.open_lease + timedelta(seconds=1)
    later = uuid4()
    await ledger.reserve(Scope("branch", uuid4(), "anthropic"), later, QUOTE, (ACCOUNT,))
    await ledger.settle(later, ProvenNotSent())
    assert _attempt(ledger, attempt)[:3] == ("settled", 0, "reconciled")

    await ledger.settle_probe(attempt, _usage(11), http_status=200)
    status = (await ledger.status("anthropic"))[0]
    assert (
        status.block_reason,
        status.settled_nusd,
        status.adjustments_nusd,
        status.open_ceiling_nusd,
    ) == (None, 0, 11, 0)
    with closing(sqlite3.connect(ledger.path)) as connection:
        corrections = connection.execute(
            "SELECT delta_nusd FROM adjustment WHERE kind = 'correction'"
        ).fetchall()
    assert corrections == [(11,)]
    assert _unblocks(ledger) == [(f"probe:{attempt}",)]


async def test_a_bill_after_the_lease_expired_with_no_admission_since_still_blocks(tmp_path):
    now = [NOW]
    ledger = await _prepare(tmp_path)
    ledger._clock = lambda: now[0]
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, UnknownOutcome("usage_incomplete"), http_status=200)

    # The lease has run out, but no admission has run since, so nothing has closed the attempt.
    now[0] = NOW + ledger.open_lease + timedelta(seconds=1)
    assert _attempt(ledger, attempt)[0] == "open"

    _, generation = _block_state(ledger)
    assert not await ledger.settle_probe(attempt, _usage(11), http_status=200)
    assert _block_state(ledger) == ("ceiling_exceeded", generation + 1)
    assert _unblocks(ledger) == [(f"probe:{attempt}",)]


async def test_a_bill_for_an_earlier_period_probe_blocks_after_an_admission_in_the_next(tmp_path):
    # The credit period keyed 2026-11 ends at 2026-11-30T00:00Z.
    now = [datetime(2026, 11, 29, 23, 0, tzinfo=UTC)]
    ledger = await _prepare(tmp_path)
    ledger._clock = lambda: now[0]
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert await ledger.settle_probe(attempt, UnknownOutcome("usage_incomplete"), http_status=200)

    # An admission in the next credit period closes only that period's expired attempts.
    now[0] = now[0] + ledger.open_lease + timedelta(seconds=1)
    assert now[0] > datetime(2026, 11, 30, tzinfo=UTC)
    later = uuid4()
    await ledger.reserve(Scope("branch", uuid4(), "anthropic"), later, QUOTE, (ACCOUNT,))
    await ledger.settle(later, ProvenNotSent())
    assert _attempt(ledger, attempt)[0] == "open"

    _, generation = _block_state(ledger)
    assert not await ledger.settle_probe(attempt, _usage(11), http_status=200)
    assert _block_state(ledger) == ("ceiling_exceeded", generation + 1)
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


def _probe_snapshot(ledger, attempt):
    """Capture a probe's row, its account's block, and the failure and unblock records."""
    return (
        _attempt(ledger, attempt),
        _block_state(ledger),
        _failures(ledger),
        _unblocks(ledger),
    )


@pytest.mark.parametrize(
    "first_status, first_error_type, repeat_status",
    [(503, "api_error", 200), (200, None, 201)],
    ids=["after_a_failure", "after_a_clear"],
)
async def test_probe_repeat_write_with_a_different_http_status_is_a_settlement_conflict(
    tmp_path, first_status, first_error_type, repeat_status
):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    reason = "provider_error" if first_error_type else "usage_incomplete"
    await ledger.settle_probe(
        attempt,
        UnknownOutcome(reason),
        http_status=first_status,
        provider_error_type=first_error_type,
    )
    before = _probe_snapshot(ledger, attempt)
    assert before[0][:5] == ("open", None, reason, first_status, first_error_type)

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.settle_probe(attempt, _usage(3), http_status=repeat_status)

    assert caught.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert (caught.value.account, caught.value.attempt_id) == (ACCOUNT.name, attempt)
    # The refused write leaves the attempt open and the block, failures and unblocks as they were.
    assert _probe_snapshot(ledger, attempt) == before
    status = (await ledger.status("anthropic"))[0]
    assert (status.settled_nusd, status.open_ceiling_nusd) == (0, 10)


@pytest.mark.parametrize(
    "first, repeat",
    [
        pytest.param(
            (UnknownOutcome("cancelled"), None, None),
            (UnknownOutcome("provider_error"), 503, "api_error"),
            id="status_stays_unrecorded",
        ),
        pytest.param(
            (UnknownOutcome("provider_error"), 503, "api_error"),
            (UnknownOutcome("usage_incomplete"), 200, None),
            id="success_status_does_not_clear_the_block",
        ),
    ],
)
async def test_probe_repeat_write_with_an_unknown_outcome_leaves_the_probe_open(
    tmp_path, first, repeat
):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    evidence, http_status, error_type = first
    assert not await ledger.settle_probe(
        attempt, evidence, http_status=http_status, provider_error_type=error_type
    )
    before = _probe_snapshot(ledger, attempt)
    assert before[0][:5] == ("open", None, evidence.reason_code, http_status, error_type)
    assert before[1][0] == "credit_balance_low"

    evidence, http_status, error_type = repeat
    assert not await ledger.settle_probe(
        attempt, evidence, http_status=http_status, provider_error_type=error_type
    )

    # The repeat is neither recorded on the attempt nor treated as a result that clears or marks.
    assert _probe_snapshot(ledger, attempt) == before
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd, status.open_ceiling_nusd) == (
        "credit_balance_low",
        0,
        10,
    )


@pytest.mark.parametrize(
    "first_reason, first_error_type",
    [("provider_error", "api_error"), ("rate_limited", None)],
    ids=["different_provider_error_type", "different_outcome"],
)
async def test_probe_rejection_that_disagrees_with_the_open_attempt_is_a_settlement_conflict(
    tmp_path, first_reason, first_error_type
):
    ledger = await _prepare(tmp_path)
    attempt = uuid4()
    await ledger.reserve_probe(ACCOUNT, attempt, QUOTE)
    assert not await ledger.settle_probe(
        attempt,
        UnknownOutcome(first_reason),
        http_status=529,
        provider_error_type=first_error_type,
    )
    before = _probe_snapshot(ledger, attempt)
    assert before[0][:5] == ("open", None, first_reason, 529, first_error_type)

    rejection = RejectionZero(529, "overloaded_error", None, "provider_error")
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.settle_probe(attempt, rejection, http_status=529)

    assert caught.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert (caught.value.account, caught.value.attempt_id) == (ACCOUNT.name, attempt)
    # The conflict is raised at once: the attempt stays open and the block is not rewritten.
    assert _probe_snapshot(ledger, attempt) == before
    status = (await ledger.status("anthropic"))[0]
    assert (status.block_reason, status.settled_nusd, status.open_ceiling_nusd) == (
        "credit_balance_low",
        0,
        10,
    )
