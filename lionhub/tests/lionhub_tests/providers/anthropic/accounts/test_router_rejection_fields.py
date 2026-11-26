# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``AccountRouter.settle_unknown`` hands a rejection's two closed-set fields to the ledger.

The fields' validation and their carriage through the core endpoint are tested with the core
(ADR-0126, Amendment 7).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import SecretStr

from lionagi.service.accounts._types import AccountSpec, Quote, Reservation, Scope, UnknownOutcome
from lionhub.providers.anthropic.accounts._ledger import CredentialLedger
from lionhub.providers.anthropic.accounts._pricing import CURRENT_REVISION
from lionhub.providers.anthropic.accounts._router import AccountRouter, RouterAttempt

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
ALPHA = AccountSpec("alpha", "ALPHA_KEY", 10**15)


class SpyLedger:
    """Records the evidence each settlement hands the ledger."""

    def __init__(self) -> None:
        self.settled: list[tuple[object, object, dict]] = []

    async def settle(self, attempt_id, evidence, **kwargs):
        self.settled.append((attempt_id, evidence, kwargs))


def quote() -> Quote:
    return Quote("model-a", 1, "price-a", "a" * 64)


def spy_router() -> tuple[AccountRouter, SpyLedger, RouterAttempt]:
    spy = SpyLedger()
    router = AccountRouter(sources=None, ledger=spy, accounts=(ALPHA,), revision=CURRENT_REVISION)
    reservation = Reservation(
        uuid4(), Scope("branch", uuid4(), "anthropic"), "alpha", "2026-01", quote()
    )
    attempt = RouterAttempt(reservation, b"{}", frozenset(), SecretStr("test-key-0123456789"))
    return router, spy, attempt


@pytest.mark.asyncio
async def test_settle_unknown_hands_the_ledger_both_fields():
    router, spy, attempt = spy_router()

    await router.settle_unknown(
        attempt,
        "request_rejected",
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    expected = UnknownOutcome(
        "request_rejected",
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )
    assert spy.settled == [(attempt.attempt_id, expected, {})]
    assert spy.settled[0][1].provider_error_type == "invalid_request_error"
    assert spy.settled[0][1].rejection_class == "prompt_too_long"


@pytest.mark.asyncio
async def test_settle_unknown_without_the_fields_is_unchanged():
    router, spy, attempt = spy_router()

    await router.settle_unknown(attempt, "rate_limited")

    assert spy.settled == [(attempt.attempt_id, UnknownOutcome("rate_limited"), {})]
    assert spy.settled[0][1].provider_error_type is None
    assert spy.settled[0][1].rejection_class is None


@pytest.mark.asyncio
async def test_settle_unknown_takes_the_fields_by_keyword_only():
    router, spy, attempt = spy_router()

    with pytest.raises(TypeError):
        await router.settle_unknown(attempt, "request_rejected", "invalid_request_error")

    assert spy.settled == []


@pytest.mark.asyncio
async def test_settle_unknown_refuses_the_fields_on_another_outcome():
    router, spy, attempt = spy_router()

    with pytest.raises(ValueError):
        await router.settle_unknown(attempt, "rate_limited", rejection_class="other")

    assert spy.settled == []


@pytest.mark.asyncio
async def test_the_ledger_settles_an_outcome_that_carries_the_fields(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = CredentialLedger(path, clock=lambda: NOW)
    reservation = await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"), uuid4(), quote(), (ALPHA,)
    )
    router = AccountRouter(
        sources=None, ledger=ledger, accounts=(ALPHA,), revision=CURRENT_REVISION
    )
    attempt = RouterAttempt(reservation, b"{}", frozenset(), SecretStr("test-key-0123456789"))

    await router.settle_unknown(
        attempt,
        "request_rejected",
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT outcome FROM attempt WHERE id = ?", (str(reservation.attempt_id),)
        ).fetchone()
    assert row == ("request_rejected",)
