# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Warm-prefix preference in the credential ledger: admission order, settlement and clearing."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from lionagi.service.accounts import _ledger
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Quote,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
ALPHA = AccountSpec("alpha", "ALPHA_KEY", 100)
BETA = AccountSpec("beta", "BETA_KEY", 100)


def scope() -> Scope:
    return Scope("branch", uuid4(), "anthropic")


def quote(ceiling: int = 10) -> Quote:
    return Quote("model-a", ceiling, "price-a", "a" * 64)


def usage(cost: int = 1, *, read: int = 0, write: int = 0) -> CompleteUsage:
    return CompleteUsage("model-a", 1, read, 0, write, 5, cost, None)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "ledger.sqlite3"
        self.now = NOW
        self.ledger = CredentialLedger(self.path, clock=lambda: self.now)


async def make_env(tmp_path: Path, specs=(ALPHA, BETA)) -> Env:
    """A ledger holding the given accounts with nothing spent or open."""
    env = Env(tmp_path)
    first = await env.ledger.reserve(scope(), uuid4(), quote(1), specs)
    await env.ledger.settle(first.attempt_id, ProvenNotSent())
    return env


async def reserve(env: Env, specs=(ALPHA, BETA), *, cert="c1", scope_=None, ceiling=10):
    return await env.ledger.reserve(scope_ or scope(), uuid4(), quote(ceiling), specs, cert=cert)


def put_warm(path: Path, account: str, cert="c1", stable=6, expires=NOW + HOUR) -> None:
    value = expires.isoformat() if isinstance(expires, datetime) else expires
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO warm_prefix(provider, account, cert, stable_tokens, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("anthropic", account, cert, stable, value),
        )
        connection.commit()


def warm_rows(path: Path) -> dict[tuple[str, str], tuple[object, object]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            (row[0], row[1]): (row[2], row[3])
            for row in connection.execute(
                "SELECT account, cert, stable_tokens, expires_at FROM warm_prefix"
            )
        }


def drop_warm(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM warm_prefix")
        connection.commit()


def trace_sql(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real(*args, **kwargs)
        connection.set_trace_callback(seen.append)
        return connection

    monkeypatch.setattr(_ledger.sqlite3, "connect", connect)
    return seen


# --- admission order -------------------------------------------------------------------------


@pytest.mark.parametrize(("cert", "expected"), [("c1", "beta"), (None, "alpha"), ("c2", "alpha")])
async def test_warm_account_is_preferred_only_for_its_own_cert(tmp_path, cert, expected):
    env = await make_env(tmp_path)
    put_warm(env.path, "beta")

    assert (await reserve(env, cert=cert)).account == expected


async def test_two_warm_accounts_keep_the_spend_ratio_order(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha")
    put_warm(env.path, "beta")
    await reserve(env, (ALPHA,), cert=None)

    assert (await reserve(env, cert="c1")).account == "beta"


@pytest.mark.parametrize(
    ("expires", "counts"),
    [
        (NOW.isoformat(), False),
        ((NOW - timedelta(seconds=1)).isoformat(), False),
        ("2026-01-15T12:30:00+01:00", False),
        ("2026-01-15T13:30:00+01:00", True),
        ("2026-01-15T13:00:00", False),
        ("not a time", False),
        (b"2026-01-15T13:00:00+00:00", False),
        ("2026-01-15T13:00:00Z", sys.version_info >= (3, 11)),
    ],
)
async def test_expiry_value_decides_whether_a_row_counts(tmp_path, expires, counts):
    env = await make_env(tmp_path)
    put_warm(env.path, "beta", expires=expires)

    assert (await reserve(env)).account == ("beta" if counts else "alpha")


@pytest.mark.parametrize(("ceiling", "expected"), [(10, "alpha"), (11, "beta")])
async def test_warm_account_must_keep_ten_percent_headroom(tmp_path, ceiling, expected):
    env = await make_env(tmp_path)
    load = await reserve(env, (ALPHA,), cert=None, ceiling=80)
    await env.ledger.settle(load.attempt_id, usage(80))
    put_warm(env.path, "alpha")

    assert (await reserve(env, ceiling=ceiling)).account == expected


async def test_account_outside_the_headroom_rule_is_still_a_candidate(tmp_path):
    env = await make_env(tmp_path)
    load = await reserve(env, (ALPHA,), cert=None, ceiling=80)
    await env.ledger.settle(load.attempt_id, usage(80))
    put_warm(env.path, "alpha")

    assert (await reserve(env, (ALPHA,), ceiling=11)).account == "alpha"


async def test_blocked_warm_account_is_skipped(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "beta")
    await env.ledger.block("anthropic", "beta", "manual")
    put_warm(env.path, "beta")

    assert (await reserve(env)).account == "alpha"


async def test_reserve_without_cert_never_names_the_warm_table(tmp_path, monkeypatch):
    env = await make_env(tmp_path)
    seen = trace_sql(monkeypatch)

    await reserve(env, cert=None)

    assert seen
    assert not any("warm_prefix" in statement for statement in seen)


async def test_reserve_on_a_bound_scope_never_names_the_warm_table(tmp_path, monkeypatch):
    env = await make_env(tmp_path)
    bound = scope()
    first = await reserve(env, scope_=bound, cert=None)
    seen = trace_sql(monkeypatch)

    second = await reserve(env, scope_=bound, cert="c1")

    assert second.account == first.account
    assert seen
    assert not any("warm_prefix" in statement for statement in seen)


# --- settlement: write arm ---------------------------------------------------------------------


async def test_write_arm_records_tokens_and_expiry_from_the_attempt_start(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    env.now = NOW + timedelta(minutes=10)

    result = await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert result == "settled"
    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable == 6
    assert datetime.fromisoformat(expires) == NOW + HOUR


async def test_a_write_deletes_rows_that_expired_more_than_a_day_earlier(tmp_path):
    env = await make_env(tmp_path)
    other = await env.ledger.reserve(
        Scope("branch", uuid4(), "other"),
        uuid4(),
        quote(1),
        (AccountSpec("gamma", "GAMMA_KEY", 100),),
    )
    await env.ledger.settle(other.attempt_id, ProvenNotSent())
    rsv = await reserve(env, (ALPHA,))
    env.now = NOW + timedelta(minutes=10)
    put_warm(env.path, "alpha", cert="old", expires=NOW - timedelta(hours=26))
    put_warm(env.path, "alpha", cert="recent", expires=NOW - timedelta(hours=24))
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute(
            "INSERT INTO warm_prefix(provider, account, cert, stable_tokens, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("other", "gamma", "old", 6, (NOW - timedelta(hours=26)).isoformat()),
        )
        connection.commit()

    result = await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert result == "settled"
    rows = warm_rows(env.path)
    assert ("alpha", "old") not in rows
    assert ("alpha", "recent") in rows
    assert ("alpha", "c1") in rows
    with closing(sqlite3.connect(env.path)) as connection:
        assert connection.execute(
            "SELECT account, cert FROM warm_prefix WHERE provider = 'other'"
        ).fetchall() == [("gamma", "old")]


async def test_a_write_keeps_an_unexpired_row_whose_text_sorts_before_the_attempt(tmp_path):
    """An expiry half an hour ahead at -10:00 reads as text about 9.5 hours before the attempt."""
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    env.now = NOW + timedelta(minutes=10)
    offset = timezone(timedelta(hours=-10))
    put_warm(
        env.path, "alpha", cert="live", expires=(NOW + timedelta(minutes=30)).astimezone(offset)
    )

    result = await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert result == "settled"
    rows = warm_rows(env.path)
    assert ("alpha", "live") in rows
    assert ("alpha", "c1") in rows


@pytest.mark.parametrize("flag", ["hour_after_key", "server_tools"])
async def test_write_arm_leaves_stable_tokens_unknown_for_unreliable_counts(tmp_path, flag):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1", **{flag: True})

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable is None
    assert datetime.fromisoformat(expires) == NOW + HOUR


async def test_write_arm_unknown_count_keeps_stored_tokens_and_extends_expiry(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6, expires=NOW + timedelta(minutes=30))
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(
        rsv.attempt_id, usage(1, read=2, write=4), cert="c1", hour_after_key=True
    )

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable == 6
    assert datetime.fromisoformat(expires) == NOW + HOUR


async def test_write_arm_known_count_replaces_unknown_and_never_shortens_expiry(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=None, expires=NOW + timedelta(hours=5))
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable == 6
    assert datetime.fromisoformat(expires) == NOW + timedelta(hours=5)


async def test_write_arm_replaces_a_row_with_an_unreadable_expiry_whole(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=99, expires="garbage")
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(
        rsv.attempt_id, usage(1, read=2, write=4), cert="c1", hour_after_key=True
    )

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable is None
    assert datetime.fromisoformat(expires) == NOW + HOUR


async def test_nothing_is_written_without_a_cert(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,), cert=None)

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4))

    assert warm_rows(env.path) == {}


async def test_nothing_is_written_when_the_ceiling_is_exceeded(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,), ceiling=10)

    await env.ledger.settle(rsv.attempt_id, usage(11, read=2, write=4), cert="c1")

    assert warm_rows(env.path) == {}


async def test_nothing_is_written_for_a_blocked_account(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    await env.ledger.block("anthropic", "alpha", "manual")

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert warm_rows(env.path) == {}


async def test_nothing_is_written_without_hour_cache_writes(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=0), cert="c1")

    assert warm_rows(env.path) == {}


async def test_repeat_settlement_writes_nothing_more(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")
    drop_warm(env.path)

    result = await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert result == "already_settled"
    assert warm_rows(env.path) == {}


async def test_conflicting_settlement_writes_nothing(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")
    drop_warm(env.path)

    with pytest.raises(AccountRoutingError) as caught:
        await env.ledger.settle(rsv.attempt_id, usage(2, read=2, write=4), cert="c1")

    assert caught.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert warm_rows(env.path) == {}


@pytest.mark.parametrize("created_at", ["garbage", "2026-01-15T12:00:00"])
async def test_unreadable_attempt_start_writes_nothing(tmp_path, created_at):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute(
            "UPDATE attempt SET created_at = ? WHERE id = ?", (created_at, str(rsv.attempt_id))
        )
        connection.commit()

    await env.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert warm_rows(env.path) == {}


# --- settlement: read arm ----------------------------------------------------------------------


@pytest.mark.parametrize("stored", [NOW + timedelta(minutes=30), NOW - HOUR])
async def test_read_arm_refreshes_an_equal_read_even_after_lapse(tmp_path, stored):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6, expires=stored)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=6, write=0), cert="c1")

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert stable == 6
    assert datetime.fromisoformat(expires) == NOW + HOUR


@pytest.mark.parametrize("read", [5, 7])
async def test_read_arm_ignores_an_unequal_read(tmp_path, read):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6, expires=NOW + timedelta(minutes=30))
    before = warm_rows(env.path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=read, write=0), cert="c1")

    assert warm_rows(env.path) == before


async def test_read_arm_never_creates_a_row(tmp_path):
    env = await make_env(tmp_path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=6, write=0), cert="c1")

    assert warm_rows(env.path) == {}


@pytest.mark.parametrize(
    ("stable", "expires"),
    [(None, NOW + timedelta(minutes=30)), (6, "garbage")],
)
async def test_read_arm_leaves_unknown_or_unreadable_rows_alone(tmp_path, stable, expires):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=stable, expires=expires)
    before = warm_rows(env.path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=6, write=0), cert="c1")

    assert warm_rows(env.path) == before


async def test_read_arm_ignores_a_server_tool_request(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6, expires=NOW + timedelta(minutes=30))
    before = warm_rows(env.path)
    rsv = await reserve(env, (ALPHA,))

    await env.ledger.settle(rsv.attempt_id, usage(1, read=6, write=0), cert="c1", server_tools=True)

    assert warm_rows(env.path) == before


async def test_read_arm_applies_to_a_bound_scope(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6, expires=NOW + timedelta(minutes=30))
    bound = scope()
    await reserve(env, (ALPHA, BETA), scope_=bound, cert=None)
    rsv = await reserve(env, (ALPHA, BETA), scope_=bound, cert="c1")

    await env.ledger.settle(rsv.attempt_id, usage(1, read=6, write=0), cert="c1")

    stable, expires = warm_rows(env.path)[("alpha", "c1")]
    assert datetime.fromisoformat(expires) == NOW + HOUR


# --- clearing ----------------------------------------------------------------------------------


async def _open_attempt(env: Env) -> UUID:
    return (await reserve(env, (ALPHA,), cert=None)).attempt_id


def _seed(env: Env) -> None:
    put_warm(env.path, "alpha")
    put_warm(env.path, "beta")


@pytest.mark.parametrize(
    ("name", "make_evidence", "clears"),
    [
        ("unknown_rate_limited", lambda: UnknownOutcome("rate_limited"), True),
        ("unknown_cancelled", lambda: UnknownOutcome("cancelled"), False),
        ("unknown_stream_closed", lambda: UnknownOutcome("stream_closed"), False),
        ("not_sent", lambda: ProvenNotSent(), True),
        ("not_sent_caller_cancelled", lambda: ProvenNotSent(caller_cancelled=True), False),
        ("complete", lambda: usage(1), False),
        ("complete_over_ceiling", lambda: usage(11), True),
    ],
)
async def test_settlement_outcomes_clear_the_account_or_not(tmp_path, name, make_evidence, clears):
    env = await make_env(tmp_path)
    attempt_id = await _open_attempt(env)
    _seed(env)

    await env.ledger.settle(attempt_id, make_evidence())

    rows = warm_rows(env.path)
    assert (("alpha", "c1") in rows) is (not clears)
    assert ("beta", "c1") in rows


async def test_default_not_sent_evidence_is_not_caller_cancelled():
    assert ProvenNotSent().caller_cancelled is False
    assert ProvenNotSent() == ProvenNotSent(False)


async def test_unknown_outcome_clears_even_when_an_earlier_reason_stands(tmp_path):
    env = await make_env(tmp_path)
    attempt_id = await _open_attempt(env)
    _seed(env)
    await env.ledger.settle(attempt_id, UnknownOutcome("cancelled"))
    assert ("alpha", "c1") in warm_rows(env.path)

    assert await env.ledger.settle(attempt_id, UnknownOutcome("rate_limited")) == "still_open"

    assert ("alpha", "c1") not in warm_rows(env.path)
    assert ("beta", "c1") in warm_rows(env.path)


async def test_unknown_outcome_on_a_settled_attempt_clears_nothing(tmp_path):
    env = await make_env(tmp_path)
    attempt_id = await _open_attempt(env)
    await env.ledger.settle(attempt_id, usage(1))
    _seed(env)

    result = await env.ledger.settle(attempt_id, UnknownOutcome("rate_limited"))

    assert result == "already_settled"
    assert ("alpha", "c1") in warm_rows(env.path)


async def test_repeated_not_sent_with_another_flag_is_already_settled_and_clears_nothing(tmp_path):
    env = await make_env(tmp_path)
    attempt_id = await _open_attempt(env)
    await env.ledger.settle(attempt_id, ProvenNotSent(caller_cancelled=True))
    _seed(env)

    result = await env.ledger.settle(attempt_id, ProvenNotSent())

    assert result == "already_settled"
    assert ("alpha", "c1") in warm_rows(env.path)


async def test_settlement_conflict_clears_the_account(tmp_path):
    env = await make_env(tmp_path)
    attempt_id = await _open_attempt(env)
    await env.ledger.settle(attempt_id, usage(1))
    _seed(env)

    with pytest.raises(AccountRoutingError):
        await env.ledger.settle(attempt_id, usage(2))

    rows = warm_rows(env.path)
    assert ("alpha", "c1") not in rows
    assert ("beta", "c1") in rows


async def test_blocking_an_account_clears_its_rows(tmp_path):
    env = await make_env(tmp_path)
    _seed(env)

    await env.ledger.block("anthropic", "alpha", "manual")

    rows = warm_rows(env.path)
    assert ("alpha", "c1") not in rows
    assert ("beta", "c1") in rows


async def test_clear_block_clears_rows_only_for_a_blocked_account(tmp_path):
    env = await make_env(tmp_path)
    await env.ledger.block("anthropic", "alpha", "manual")
    _seed(env)

    await env.ledger.clear_block(uuid4(), "anthropic", "alpha", "operator")

    rows = warm_rows(env.path)
    assert ("alpha", "c1") not in rows
    assert ("beta", "c1") in rows

    put_warm(env.path, "alpha")
    await env.ledger.clear_block(uuid4(), "anthropic", "alpha", "operator")
    assert ("alpha", "c1") in warm_rows(env.path)


async def test_repeating_a_recorded_clear_block_deletes_nothing(tmp_path):
    env = await make_env(tmp_path)
    adjustment = uuid4()
    await env.ledger.block("anthropic", "alpha", "manual")
    await env.ledger.clear_block(adjustment, "anthropic", "alpha", "operator")
    await env.ledger.block("anthropic", "alpha", "manual")
    put_warm(env.path, "alpha")

    await env.ledger.clear_block(adjustment, "anthropic", "alpha", "operator")

    assert ("alpha", "c1") in warm_rows(env.path)


async def test_attempt_reserved_before_a_clear_still_writes_when_it_completes(tmp_path):
    env = await make_env(tmp_path)
    pending = await reserve(env, (ALPHA,))
    other = await _open_attempt(env)
    await env.ledger.settle(other, UnknownOutcome("rate_limited"))

    await env.ledger.settle(pending.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert warm_rows(env.path)[("alpha", "c1")][0] == 6


async def test_attempt_reserved_before_a_clear_does_not_refresh_a_cleared_row(tmp_path):
    env = await make_env(tmp_path)
    put_warm(env.path, "alpha", stable=6)
    pending = await reserve(env, (ALPHA,))
    other = await _open_attempt(env)
    await env.ledger.settle(other, UnknownOutcome("rate_limited"))

    await env.ledger.settle(pending.attempt_id, usage(1, read=6, write=0), cert="c1")

    assert warm_rows(env.path) == {}


# --- schema ------------------------------------------------------------------------------------


async def test_fresh_ledger_creates_the_warm_table_outside_the_base_set(tmp_path):
    env = await make_env(tmp_path)

    with closing(sqlite3.connect(env.path)) as connection:
        columns = {
            row[1]: (row[2], row[3], row[5])
            for row in connection.execute("PRAGMA table_info(warm_prefix)")
        }
        foreign = connection.execute("PRAGMA foreign_key_list(warm_prefix)").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert columns == {
        "provider": ("TEXT", 1, 1),
        "account": ("TEXT", 1, 2),
        "cert": ("TEXT", 1, 3),
        "stable_tokens": ("INTEGER", 0, 0),
        "expires_at": ("TEXT", 1, 0),
    }
    assert len(foreign) == 2
    assert version == 1
    assert "warm_prefix" not in _ledger._TABLES


async def test_fresh_ledger_creates_the_warm_table_before_the_version_is_set(tmp_path, monkeypatch):
    env = Env(tmp_path)
    seen = trace_sql(monkeypatch)

    await env.ledger.status("anthropic")

    statements = [" ".join(statement.split()) for statement in seen]
    created = [
        i
        for i, s in enumerate(statements)
        if s.startswith("CREATE TABLE IF NOT EXISTS warm_prefix")
    ]
    versioned = [i for i, s in enumerate(statements) if s == "PRAGMA user_version = 1"]
    assert created and versioned and created[0] < versioned[0]


async def test_version_one_ledger_without_the_warm_table_gains_it(tmp_path):
    env = await make_env(tmp_path)
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute("DROP TABLE warm_prefix")
        connection.commit()

    await env.ledger.status("anthropic")

    assert warm_rows(env.path) == {}
    with closing(sqlite3.connect(env.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


async def test_the_certificate_read_uses_its_index(tmp_path):
    env = await make_env(tmp_path)
    statement = "SELECT account, expires_at FROM warm_prefix WHERE provider = ? AND cert = ?"

    with closing(sqlite3.connect(env.path)) as connection:
        plan = [
            row[3]
            for row in connection.execute("EXPLAIN QUERY PLAN " + statement, ("anthropic", "c1"))
        ]

    assert any("warm_prefix_cert" in detail and "cert=?" in detail for detail in plan), plan


async def test_a_ledger_whose_table_predates_the_indexes_gains_them(tmp_path):
    env = await make_env(tmp_path)
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute("DROP INDEX IF EXISTS warm_prefix_cert")
        connection.execute("DROP INDEX IF EXISTS warm_prefix_expiry")
        connection.commit()

    await CredentialLedger(env.path, clock=lambda: NOW).status("anthropic")

    with closing(sqlite3.connect(env.path)) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert {"warm_prefix_cert", "warm_prefix_expiry"} <= names


async def test_version_one_ledger_missing_a_base_table_is_refused(tmp_path):
    env = await make_env(tmp_path)
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute("DROP TABLE adjustment")
        connection.commit()

    with pytest.raises(AccountRoutingError) as caught:
        await env.ledger.status("anthropic")

    assert caught.value.code is RouterErrorCode.LEDGER_UNAVAILABLE


async def test_two_ledgers_on_one_file_see_each_others_rows_and_clears(tmp_path):
    first = await make_env(tmp_path)
    second = CredentialLedger(first.path, clock=lambda: NOW)
    rsv = await reserve(first, (ALPHA,))
    await first.ledger.settle(rsv.attempt_id, usage(1, read=2, write=4), cert="c1")

    assert (
        await second.reserve(scope(), uuid4(), quote(), (ALPHA, BETA), cert="c1")
    ).account == "alpha"

    other = await _open_attempt(first)
    await second.settle(other, UnknownOutcome("rate_limited"))

    assert warm_rows(first.path) == {}
