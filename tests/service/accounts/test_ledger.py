# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The private credential ledger: admission, settlement, corrections and locking."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest

from lionagi.service.accounts._ledger import (
    AccountStatus,
    CredentialLedger,
    default_ledger_path,
)
from lionagi.service.accounts._types import (
    INT64_MAX,
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Quote,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)


def make_scope(identity: UUID | None = None) -> Scope:
    return Scope("branch", identity or uuid4(), "anthropic")


def make_quote(ceiling: int = 1) -> Quote:
    return Quote("model-a", ceiling, "price-a", "a" * 64)


def make_spec(name: str = "alpha", budget: int = 10, key_env: str | None = None) -> AccountSpec:
    return AccountSpec(name, key_env or f"{name.upper()}_KEY", budget)


def make_usage(cost: int, *, request_id: str | None = None) -> CompleteUsage:
    return CompleteUsage("model-a", 1, 2, 3, 4, 5, cost, request_id)


def read_attempt(path: Path, attempt_id: UUID) -> tuple[object, ...] | None:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT state, cost_nusd, usage_json, request_id, outcome, month_utc
            FROM attempt WHERE id = ?
            """,
            (str(attempt_id),),
        ).fetchone()


def read_binding(path: Path, scope: Scope) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT account FROM binding
            WHERE scope_kind = ? AND scope_id = ? AND provider = ?
            """,
            (scope.kind, str(scope.id), scope.provider),
        ).fetchone()
    return None if row is None else str(row[0])


def read_adjustments(path: Path, provider: str, account: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT kind, attempt_id, month_utc, delta_nusd, source_ref
            FROM adjustment WHERE provider = ? AND account = ? ORDER BY id
            """,
            (provider, account),
        ).fetchall()


async def reserve_one(
    ledger: CredentialLedger,
    *,
    scope: Scope | None = None,
    attempt_id: UUID | None = None,
    quote: Quote | None = None,
    accounts: tuple[AccountSpec, ...] | None = None,
):
    return await ledger.reserve(
        scope or make_scope(),
        attempt_id or uuid4(),
        quote or make_quote(),
        accounts or (make_spec(),),
    )


@pytest.mark.anyio
async def test_default_path_uses_home_settings_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_ledger_path() == tmp_path / ".lionagi" / "credential-ledger.sqlite3"


@pytest.mark.anyio
async def test_first_reservation_creates_private_schema(tmp_path):
    path = tmp_path / "private" / "credential-ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 3, 2, tzinfo=timezone.utc))

    reservation = await reserve_one(ledger)
    rows = await ledger.status("anthropic", "2026-03")

    assert reservation.month_utc == "2026-03"
    assert rows == (AccountStatus("anthropic", "alpha", "2026-03", 10, 0, 0, 1, 1, None),)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert names == {"account", "month", "binding", "attempt", "adjustment", "warm_prefix"}
    assert read_attempt(path, reservation.attempt_id)[0:2] == ("open", None)


@pytest.mark.anyio
async def test_unknown_schema_version_refuses_without_changing_bytes(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 74")
    before = path.read_bytes()

    with pytest.raises(AccountRoutingError) as caught:
        await CredentialLedger(path).status("anthropic")

    assert caught.value.code is RouterErrorCode.LEDGER_UNAVAILABLE
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_non_database_file_refuses_without_changing_bytes(tmp_path):
    path = tmp_path / "notes.sqlite3"
    path.write_bytes(b"preserve this data\x00\xff")
    before = path.read_bytes()

    with pytest.raises(AccountRoutingError) as caught:
        await CredentialLedger(path).status("anthropic")

    assert caught.value.code is RouterErrorCode.LEDGER_UNAVAILABLE
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_admission_holds_ceiling_and_settlement_replaces_it(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 15, tzinfo=timezone.utc))
    spec = make_spec(budget=10)
    first = await reserve_one(ledger, quote=make_quote(8), accounts=(spec,))

    with pytest.raises(AccountRoutingError) as caught:
        await reserve_one(ledger, quote=make_quote(3), accounts=(spec,))
    assert caught.value.code is RouterErrorCode.BUDGET_EXHAUSTED
    held = (await ledger.status("anthropic", "2026-01"))[0]
    assert (held.open_ceiling_nusd, held.open_attempts) == (8, 1)

    result = await ledger.settle(first.attempt_id, UnknownOutcome("timeout"))
    assert result == "still_open"
    assert read_attempt(path, first.attempt_id)[4] == "timeout"
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 8

    result = await ledger.settle(first.attempt_id, make_usage(4))
    assert result == "settled"
    settled = (await ledger.status("anthropic", "2026-01"))[0]
    assert (settled.settled_nusd, settled.open_ceiling_nusd, settled.open_attempts) == (
        4,
        0,
        0,
    )
    assert read_attempt(path, first.attempt_id)[0:2] == ("settled", 4)

    second = await reserve_one(ledger, quote=make_quote(6), accounts=(spec,))
    assert second.account == "alpha"
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 6
    assert await ledger.settle(second.attempt_id, ProvenNotSent()) == "settled"
    row = read_attempt(path, second.attempt_id)
    assert row[0:2] == ("settled", 0)
    assert row[4] == "not_sent"
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.settled_nusd, status.open_ceiling_nusd, status.open_attempts) == (4, 0, 0)


@pytest.mark.anyio
async def test_a_later_unknown_outcome_keeps_the_reason_already_recorded(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 15, tzinfo=timezone.utc))
    attempt = await reserve_one(ledger, quote=make_quote(8), accounts=(make_spec(budget=10),))

    assert await ledger.settle(attempt.attempt_id, UnknownOutcome("rate_limited")) == "still_open"
    assert await ledger.settle(attempt.attempt_id, UnknownOutcome("cancelled")) == "still_open"

    assert read_attempt(path, attempt.attempt_id)[4] == "rate_limited"
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 8


@pytest.mark.anyio
async def test_budget_equality_admits_and_one_more_nusd_refuses(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    spec = make_spec(budget=10)
    admitted = await reserve_one(ledger, quote=make_quote(10), accounts=(spec,))
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 10

    with pytest.raises(AccountRoutingError) as caught:
        await reserve_one(ledger, quote=make_quote(1), accounts=(spec,))

    assert caught.value.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert read_attempt(tmp_path / "ledger.sqlite3", admitted.attempt_id)[0] == "open"
    row = (await ledger.status("anthropic", "2026-01"))[0]
    assert (row.open_ceiling_nusd, row.open_attempts) == (10, 1)


@pytest.mark.anyio
async def test_quote_validation_and_empty_accounts(tmp_path):
    ledger = CredentialLedger(tmp_path / "ledger.sqlite3")
    invalid_quotes = (
        Quote("model-a", True, "price-a", "a" * 64),
        Quote("model-a", 0, "price-a", "a" * 64),
        Quote("model-a", INT64_MAX + 1, "price-a", "a" * 64),
        Quote("", 1, "price-a", "a" * 64),
        Quote("model-a", 1, "", "a" * 64),
        Quote("model-a", 1, "price-a", "A" * 64),
    )
    for quote in invalid_quotes:
        with pytest.raises(AccountRoutingError) as caught:
            await reserve_one(ledger, quote=quote)
        assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING

    with pytest.raises(ValueError):
        await ledger.reserve(make_scope(), uuid4(), make_quote(), ())
    assert await ledger.status("anthropic", "2026-01") == ()


@pytest.mark.anyio
async def test_integer_ratio_orders_values_that_float_division_ties(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    n = 2**53
    assert (n + 1) / (n + 2) == n / (n + 1)
    account_a = make_spec("alpha", n + 2)
    account_b = make_spec("beta", n + 1)
    first = await reserve_one(ledger, quote=make_quote(n + 2), accounts=(account_a,))
    second = await reserve_one(
        ledger,
        scope=make_scope(),
        quote=make_quote(n + 1),
        accounts=(account_b,),
    )
    await ledger.settle(first.attempt_id, make_usage(n + 1))
    await ledger.settle(second.attempt_id, make_usage(n))

    selected = await reserve_one(
        ledger,
        quote=make_quote(1),
        accounts=(account_a, account_b),
    )

    assert selected.account == "beta"
    assert read_binding(tmp_path / "ledger.sqlite3", selected.scope) == "beta"


@pytest.mark.anyio
async def test_open_scope_count_breaks_equal_ratio_before_name(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    account_a = make_spec("alpha", 10)
    account_b = make_spec("beta", 10)
    scope_a1 = make_scope()
    scope_a2 = make_scope()
    scope_b1 = make_scope()
    await reserve_one(ledger, scope=scope_a1, accounts=(account_a,))
    await reserve_one(ledger, scope=scope_a2, accounts=(account_a,))
    await reserve_one(ledger, scope=scope_b1, accounts=(account_b,))
    await ledger.adjust(uuid4(), "anthropic", "beta", "2026-01", 1, "seed")

    selected = await reserve_one(ledger, quote=make_quote(1), accounts=(account_a, account_b))

    assert selected.account == "beta"
    assert (await ledger.status("anthropic", "2026-01"))[1].open_attempts == 2


@pytest.mark.anyio
async def test_open_count_uses_distinct_scopes(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    account_a = make_spec("alpha", 10)
    account_b = make_spec("beta", 10)
    repeated_scope = make_scope()
    await reserve_one(ledger, scope=repeated_scope, accounts=(account_a,))
    await reserve_one(ledger, scope=repeated_scope, accounts=(account_a,))
    await reserve_one(ledger, scope=make_scope(), accounts=(account_b,))
    await ledger.adjust(uuid4(), "anthropic", "beta", "2026-01", 1, "seed")

    selected = await reserve_one(ledger, quote=make_quote(1), accounts=(account_a, account_b))

    assert selected.account == "alpha"
    rows = await ledger.status("anthropic", "2026-01")
    assert (rows[0].open_attempts, rows[1].open_attempts) == (3, 1)


@pytest.mark.anyio
async def test_name_breaks_equal_ratio_and_scope_count(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    selected = await reserve_one(
        ledger,
        accounts=(make_spec("zeta", 10), make_spec("alpha", 10)),
    )

    assert selected.account == "alpha"
    assert read_binding(tmp_path / "ledger.sqlite3", selected.scope) == "alpha"


@pytest.mark.anyio
async def test_binding_stays_even_when_another_account_has_more_room(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    account_a = make_spec("alpha", 10)
    account_b = make_spec("beta", 10)
    scope = make_scope()
    first = await reserve_one(ledger, scope=scope, quote=make_quote(8), accounts=(account_a,))
    await ledger.settle(first.attempt_id, make_usage(8))

    second = await reserve_one(
        ledger,
        scope=scope,
        quote=make_quote(1),
        accounts=(account_a, account_b),
    )

    assert second.account == "alpha"
    assert read_binding(tmp_path / "ledger.sqlite3", scope) == "alpha"
    assert (await ledger.status("anthropic", "2026-01"))[1].settled_nusd == 0


@pytest.mark.anyio
async def test_blocked_or_removed_binding_is_not_reassigned(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    account_a = make_spec("alpha", 10)
    account_b = make_spec("beta", 10)
    scope = make_scope()
    original = await reserve_one(ledger, scope=scope, accounts=(account_a,))
    await ledger.settle(original.attempt_id, ProvenNotSent())
    await ledger.block("anthropic", "alpha", "auth_required")

    with pytest.raises(AccountRoutingError) as blocked:
        await reserve_one(
            ledger,
            scope=scope,
            accounts=(account_a, account_b),
        )
    assert blocked.value.code is RouterErrorCode.ACCOUNT_BLOCKED
    assert blocked.value.account == "alpha"

    await ledger.clear_block(uuid4(), "anthropic", "alpha", "operator-clear")
    with pytest.raises(AccountRoutingError) as removed:
        await reserve_one(ledger, scope=scope, accounts=(account_b,))
    assert removed.value.code is RouterErrorCode.ACCOUNT_BLOCKED
    assert removed.value.account == "alpha"
    assert read_binding(tmp_path / "ledger.sqlite3", scope) == "alpha"
    rows = await ledger.status("anthropic", "2026-01")
    assert len(rows) == 1
    assert (rows[0].block_reason, rows[0].open_attempts) == (None, 0)


@pytest.mark.anyio
async def test_all_blocked_and_no_capacity_have_distinct_errors(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    account_a = make_spec("alpha", 10)
    original = await reserve_one(ledger, accounts=(account_a,))
    await ledger.settle(original.attempt_id, ProvenNotSent())
    await ledger.block("anthropic", "alpha", "auth_required")

    with pytest.raises(AccountRoutingError) as all_blocked:
        await reserve_one(ledger, accounts=(account_a,))
    assert all_blocked.value.code is RouterErrorCode.ACCOUNT_BLOCKED
    assert (await ledger.status("anthropic", "2026-01"))[0].block_reason == "auth_required"

    account_b = make_spec("beta", 1)
    with pytest.raises(AccountRoutingError) as no_capacity:
        await reserve_one(
            ledger,
            quote=make_quote(2),
            accounts=(account_a, account_b),
        )
    assert no_capacity.value.code is RouterErrorCode.BUDGET_EXHAUSTED
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempt").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM binding").fetchone() == (1,)


@pytest.mark.anyio
async def test_month_rollover_keeps_attempt_month_and_detects_clock_regression(tmp_path):
    # The period keyed 2026-02 starts at 00:00 UTC on 2026-01-31, the last day of January.
    now = [datetime(2026, 1, 30, 23, 59, tzinfo=timezone.utc)]
    ledger = CredentialLedger(tmp_path / "ledger.sqlite3", clock=lambda: now[0])
    january = await reserve_one(ledger, quote=make_quote(4), accounts=(make_spec(budget=10),))
    now[0] = datetime(2026, 1, 31, 0, 1, tzinfo=timezone.utc)
    february = await reserve_one(
        ledger,
        quote=make_quote(2),
        accounts=(make_spec(budget=20),),
    )
    assert february.month_utc == "2026-02"
    feb_status = await ledger.status("anthropic", "2026-02")
    assert feb_status[0].budget_nusd == 20

    assert await ledger.settle(january.attempt_id, make_usage(3)) == "settled"
    old_row = read_attempt(tmp_path / "ledger.sqlite3", january.attempt_id)
    assert old_row[1] == 3
    assert old_row[5] == "2026-01"
    assert (await ledger.status("anthropic", "2026-01"))[0].settled_nusd == 3

    now[0] = datetime(2026, 1, 30, tzinfo=timezone.utc)
    with pytest.raises(AccountRoutingError) as caught:
        await reserve_one(ledger, accounts=(make_spec(budget=10),))
    assert caught.value.code is RouterErrorCode.LEDGER_UNAVAILABLE
    assert read_attempt(tmp_path / "ledger.sqlite3", february.attempt_id)[0] == "open"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("at", "month"),
    [
        (datetime(2026, 10, 30, 23, 59, 59, tzinfo=timezone.utc), "2026-10"),
        (datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc), "2026-11"),
        (datetime(2026, 12, 30, 23, 59, tzinfo=timezone.utc), "2026-12"),
        (datetime(2026, 12, 31, 0, 0, tzinfo=timezone.utc), "2027-01"),
        (datetime(2028, 2, 28, 23, 59, tzinfo=timezone.utc), "2028-02"),
        (datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc), "2028-03"),
    ],
)
async def test_a_month_starts_at_midnight_utc_on_the_previous_months_last_day(tmp_path, at, month):
    ledger = CredentialLedger(tmp_path / "ledger.sqlite3", clock=lambda: at)
    reservation = await reserve_one(ledger, accounts=(make_spec(budget=10),))
    assert reservation.month_utc == month
    assert (await ledger.status("anthropic"))[0].open_attempts == 1


@pytest.mark.anyio
async def test_changed_key_reference_refuses(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    await reserve_one(ledger, accounts=(make_spec(key_env="FIRST_KEY"),))

    with pytest.raises(AccountRoutingError) as caught:
        await reserve_one(ledger, accounts=(make_spec(key_env="SECOND_KEY"),))

    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT
    assert (await ledger.status("anthropic", "2026-01"))[0].open_attempts == 1


@pytest.mark.anyio
async def test_changed_month_budget_refuses(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    await reserve_one(ledger, accounts=(make_spec(budget=10),))

    with pytest.raises(AccountRoutingError) as caught:
        await reserve_one(ledger, accounts=(make_spec(budget=11),))

    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT
    assert (await ledger.status("anthropic", "2026-01"))[0].budget_nusd == 10


@pytest.mark.anyio
async def test_duplicate_attempt_and_conflicting_settlement_preserve_first_cost(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    scope = make_scope()
    attempt_id = uuid4()
    original = await reserve_one(ledger, scope=scope, attempt_id=attempt_id, quote=make_quote(5))

    with pytest.raises(AccountRoutingError) as duplicate:
        await reserve_one(ledger, scope=scope, attempt_id=attempt_id, quote=make_quote(5))
    assert duplicate.value.code is RouterErrorCode.DUPLICATE_ATTEMPT
    assert await ledger.settle(attempt_id, make_usage(2, request_id="bad id")) == "settled"
    assert await ledger.settle(attempt_id, make_usage(2, request_id="bad id")) == (
        "already_settled"
    )
    first_row = read_attempt(path, attempt_id)
    assert first_row[0:2] == ("settled", 2)
    assert first_row[3] is None
    assert first_row[4] == "complete"
    assert json.loads(first_row[2]) == {
        "cache_read_input_tokens": 2,
        "cache_write_1h_tokens": 4,
        "cache_write_5m_tokens": 3,
        "input_tokens": 1,
        "output_tokens": 5,
        "served_model": "model-a",
    }

    with pytest.raises(AccountRoutingError) as conflict:
        await ledger.settle(attempt_id, make_usage(3, request_id="different"))
    assert conflict.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert conflict.value.attempt_id == attempt_id
    final_row = read_attempt(path, attempt_id)
    assert final_row[0:2] == ("settled", 2)
    assert final_row[4] == "complete"
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.settled_nusd, status.block_reason) == (2, "settlement_conflict")
    assert read_binding(path, original.scope) == "alpha"


@pytest.mark.anyio
async def test_invalid_usage_writes_nothing(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    reservation = await reserve_one(ledger)

    with pytest.raises(ValueError):
        await ledger.settle(reservation.attempt_id, CompleteUsage("model-a", True, 0, 0, 0, 0, 1))
    with pytest.raises(ValueError):
        await ledger.settle(reservation.attempt_id, UnknownOutcome("Not-A-Code"))

    row = read_attempt(path, reservation.attempt_id)
    assert row[0:2] == ("open", None)
    assert row[4] is None
    assert (await ledger.status("anthropic", "2026-01"))[0].open_ceiling_nusd == 1


@pytest.mark.anyio
async def test_cost_above_ceiling_is_kept_and_blocks_account(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    reservation = await reserve_one(ledger, quote=make_quote(1), accounts=(make_spec(budget=1),))

    assert await ledger.settle(reservation.attempt_id, make_usage(4)) == "settled"

    row = read_attempt(path, reservation.attempt_id)
    assert row[0:2] == ("settled", 4)
    assert row[4] == "complete"
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.settled_nusd, status.block_reason) == (4, "ceiling_exceeded")


@pytest.mark.anyio
async def test_reconcile_adjust_and_clear_block_are_sourced_and_repeatable(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    reservation = await reserve_one(ledger, quote=make_quote(6))
    reconciliation_id = uuid4()
    await ledger.reconcile(reconciliation_id, reservation.attempt_id, 4, "provider-record")
    await ledger.reconcile(reconciliation_id, reservation.attempt_id, 4, "provider-record")
    row = read_attempt(path, reservation.attempt_id)
    assert row[0:2] == ("settled", 4)
    assert row[4] == "reconciled"
    assert read_adjustments(path, "anthropic", "alpha") == [
        ("reconciliation", str(reservation.attempt_id), "2026-01", 0, "provider-record")
    ]

    with pytest.raises(AccountRoutingError) as reconcile_conflict:
        await ledger.reconcile(reconciliation_id, reservation.attempt_id, 5, "provider-record")
    assert reconcile_conflict.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert read_attempt(path, reservation.attempt_id)[1] == 4

    correction_id = uuid4()
    await ledger.adjust(correction_id, "anthropic", "alpha", "2026-01", 3, "invoice-a")
    await ledger.adjust(correction_id, "anthropic", "alpha", "2026-01", 3, "invoice-a")
    correction_id_2 = uuid4()
    await ledger.adjust(correction_id_2, "anthropic", "alpha", "2026-01", -3, "invoice-b")
    with pytest.raises(ValueError):
        await ledger.adjust(uuid4(), "anthropic", "alpha", "2026-01", -5, "invoice-c")
    with pytest.raises(AccountRoutingError) as adjustment_conflict:
        await ledger.adjust(correction_id, "anthropic", "alpha", "2026-01", 2, "invoice-a")
    assert adjustment_conflict.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.settled_nusd, status.adjustments_nusd) == (4, 0)

    await ledger.block("anthropic", "alpha", "auth_required")
    unblock_id = uuid4()
    await ledger.clear_block(unblock_id, "anthropic", "alpha", "operator-record")
    await ledger.clear_block(unblock_id, "anthropic", "alpha", "operator-record")
    assert (await ledger.status("anthropic", "2026-01"))[0].block_reason is None
    assert (
        "unblock",
        None,
        None,
        0,
        "operator-record",
    ) in read_adjustments(path, "anthropic", "alpha")
    with pytest.raises(AccountRoutingError) as unblock_conflict:
        await ledger.clear_block(unblock_id, "anthropic", "alpha", "different-record")
    assert unblock_conflict.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert read_attempt(path, reservation.attempt_id)[4] == "reconciled"


@pytest.mark.anyio
async def test_adjustment_requires_an_existing_account_and_valid_source(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    await reserve_one(ledger)

    with pytest.raises(ValueError):
        await ledger.adjust(uuid4(), "anthropic", "alpha", "2026-01", 0, "")
    with pytest.raises(ValueError):
        await ledger.adjust(uuid4(), "anthropic", "alpha", "2026-13", 0, "source")
    with pytest.raises(LookupError):
        await ledger.adjust(uuid4(), "anthropic", "missing", "2026-01", 0, "source")

    assert (await ledger.status("anthropic", "2026-01"))[0].adjustments_nusd == 0


@pytest.mark.anyio
async def test_corrections_count_toward_admission_in_both_directions(tmp_path):
    ledger = CredentialLedger(
        tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    first = await reserve_one(ledger)
    await ledger.settle(first.attempt_id, make_usage(0))
    await ledger.adjust(uuid4(), "anthropic", "alpha", "2026-01", 10, "invoice-a")

    # A new scope picks among candidates; the bound scope checks its account directly.
    for scope in (make_scope(), first.scope):
        with pytest.raises(AccountRoutingError) as caught:
            await reserve_one(ledger, scope=scope)
        assert caught.value.code is RouterErrorCode.BUDGET_EXHAUSTED

    await ledger.adjust(uuid4(), "anthropic", "alpha", "2026-01", -1, "credit-a")
    await reserve_one(ledger, scope=first.scope)


@pytest.mark.anyio
async def test_reconciling_a_settled_attempt_conflicts_and_keeps_its_cost(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    reservation = await reserve_one(ledger, quote=make_quote(6))
    await ledger.settle(reservation.attempt_id, make_usage(2))

    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reconcile(uuid4(), reservation.attempt_id, 4, "provider-record")
    assert caught.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert read_attempt(path, reservation.attempt_id)[0:2] == ("settled", 2)
    assert read_adjustments(path, "anthropic", "alpha") == []


@pytest.mark.anyio
async def test_a_ledger_created_by_a_peer_while_this_one_opens_is_used(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    clock = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)  # noqa: E731
    ledger, peer = CredentialLedger(path, clock=clock), CredentialLedger(path, clock=clock)
    real_connect = sqlite3.connect
    opened: list[bool] = []

    class Row:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class PeerCreatesAfterTheVersionRead:
        # The peer commits a whole schema between this connection's first unlocked
        # read of the version and anything it reads next.
        def __init__(self, connection):
            self.connection = connection
            self.fired = False

        def execute(self, statement, parameters=()):
            cursor = self.connection.execute(statement, parameters)
            if statement == "PRAGMA user_version" and not self.fired:
                self.fired = True
                row = cursor.fetchone()
                with closing(peer._connect()):
                    pass
                return Row(row)
            return cursor

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if opened:
            return connection
        opened.append(True)
        return PeerCreatesAfterTheVersionRead(connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    reservation = await reserve_one(ledger)
    assert read_attempt(path, reservation.attempt_id)[0] == "open"


@pytest.mark.anyio
async def test_two_connections_admit_only_one_reservation(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    spec = make_spec(budget=10)
    results: list[object] = []

    async def reserve(scope_id: UUID) -> None:
        try:
            result = await ledger.reserve(make_scope(scope_id), uuid4(), make_quote(6), (spec,))
        except AccountRoutingError as exc:
            results.append(exc.code)
        else:
            results.append(result)

    async with anyio.create_task_group() as group:
        group.start_soon(reserve, uuid4())
        group.start_soon(reserve, uuid4())

    assert sum(not isinstance(result, RouterErrorCode) for result in results) == 1
    assert results.count(RouterErrorCode.BUDGET_EXHAUSTED) == 1
    status = (await ledger.status("anthropic", "2026-01"))[0]
    assert (status.open_ceiling_nusd, status.open_attempts) == (6, 1)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM binding").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM attempt").fetchone() == (1,)


@pytest.mark.anyio
async def test_two_processes_admit_only_one_reservation(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    outcomes = (tmp_path / "one.outcome", tmp_path / "two.outcome")
    scope_ids = (uuid4(), uuid4())
    attempt_ids = (uuid4(), uuid4())
    source = """\
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import AccountRoutingError, AccountSpec, Quote, Scope

async def run():
    ledger = CredentialLedger(Path(sys.argv[1]), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    scope = Scope('branch', UUID(sys.argv[3]), 'anthropic')
    quote = Quote('model-a', 6, 'price-a', 'a' * 64)
    try:
        await ledger.reserve(
            scope,
            UUID(sys.argv[4]),
            quote,
            (AccountSpec('alpha', 'ALPHA_KEY', 10),),
        )
    except AccountRoutingError as exc:
        outcome = exc.code.value
    else:
        outcome = 'admitted'
    Path(sys.argv[2]).write_text(outcome)

asyncio.run(run())
"""

    def run_processes() -> None:
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    source,
                    str(path),
                    str(outcome),
                    str(scope_id),
                    str(attempt_id),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for outcome, scope_id, attempt_id in zip(outcomes, scope_ids, attempt_ids, strict=True)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)

    await anyio.to_thread.run_sync(run_processes)

    assert sorted(outcome.read_text() for outcome in outcomes) == [
        "admitted",
        "budget_exhausted",
    ]
    status = await CredentialLedger(
        path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    ).status("anthropic", "2026-01")
    assert (status[0].open_ceiling_nusd, status[0].open_attempts) == (6, 1)


@pytest.mark.anyio
async def test_busy_lock_refuses_within_timeout(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(
        path,
        busy_timeout_s=0.2,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await ledger.status("anthropic", "2026-01")
    lock = sqlite3.connect(path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(AccountRoutingError) as caught:
            await reserve_one(ledger)
    finally:
        lock.execute("ROLLBACK")
        lock.close()

    assert caught.value.code is RouterErrorCode.LEDGER_BUSY
    assert time.monotonic() - started < 2
    assert await ledger.status("anthropic", "2026-01") == ()


@pytest.mark.anyio
async def test_commit_failure_rolls_back_attempt_and_binding(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    await ledger.status("anthropic", "2026-01")
    original_connect = ledger._connect

    class CommitFailure:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            if statement.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return self.connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(ledger, "_connect", lambda: CommitFailure(original_connect()))
    scope = make_scope()
    attempt_id = uuid4()
    with pytest.raises(AccountRoutingError) as caught:
        await ledger.reserve(scope, attempt_id, make_quote(), (make_spec(),))
    assert caught.value.code is RouterErrorCode.LEDGER_UNAVAILABLE

    monkeypatch.setattr(ledger, "_connect", original_connect)
    assert read_attempt(path, attempt_id) is None
    assert read_binding(path, scope) is None
    assert await ledger.status("anthropic", "2026-01") == ()


@pytest.mark.anyio
async def test_cancel_waits_for_reservation_worker_to_commit(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    await ledger.status("anthropic", "2026-01")
    original_connect = ledger._connect
    entered = threading.Event()
    release = threading.Event()

    class SlowCommit:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            if statement.strip().upper() == "COMMIT":
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("commit gate expired")
            return self.connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(ledger, "_connect", lambda: SlowCommit(original_connect()))
    scope = make_scope()
    attempt_id = uuid4()

    returned: list[bool] = []

    async def reserve_and_capture() -> None:
        try:
            await ledger.reserve(scope, attempt_id, make_quote(), (make_spec(),))
        except BaseException:
            pass
        returned.append(True)

    async with anyio.create_task_group() as group:
        group.start_soon(reserve_and_capture)
        await anyio.to_thread.run_sync(entered.wait, abandon_on_cancel=False)
        group.cancel_scope.cancel()
        with anyio.CancelScope(shield=True):
            await anyio.sleep(0.2)
        # The commit is still held, so a cancelled call that has already returned
        # left its worker behind.
        returned_before_commit = bool(returned)
        release.set()

    assert not returned_before_commit
    assert returned == [True]
    monkeypatch.setattr(ledger, "_connect", original_connect)
    row = read_attempt(path, attempt_id)
    assert row is not None
    assert row[0:2] == ("open", None)
    status = await ledger.status("anthropic", "2026-01")
    assert (status[0].open_attempts, status[0].open_ceiling_nusd) == (1, 1)
