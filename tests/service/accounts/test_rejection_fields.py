# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A routed request rejection carries two validated, closed-set fields.

Covers their validation on ``AccountRoutingError`` and ``UnknownOutcome``, their carriage from the
internal routed-attempt error to the public one, and their pass-through from
``AccountRouter.settle_unknown`` to the ledger (ADR-0126, Amendment 7).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from lionagi.service.accounts import _types
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._pricing import CURRENT_REVISION
from lionagi.service.accounts._router import AccountRouter, RouterAttempt
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    Quote,
    Reservation,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)
from lionagi.service.connections.endpoint import (
    Endpoint,
    _RoutedNoRetryError,
    _RoutedRetryableError,
)

ATTEMPT = UUID("00000000-0000-4000-8000-000000000001")
SENTINEL = "zz-sentinel-4f1c9a"
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
ALPHA = AccountSpec("alpha", "ALPHA_KEY", 10**15)
FIELDS = ["provider_error_type", "rejection_class"]


def build_error(**given):
    return AccountRoutingError(RouterErrorCode.REQUEST_REJECTED, **given)


def build_outcome(**given):
    return UnknownOutcome("request_rejected", **given)


BUILDERS = [build_error, build_outcome]
BUILDER_IDS = ["error", "outcome"]

NOT_MEMBERS = [
    (field, value) for field in FIELDS for value in ("Other", "other ", "", 7, ["other"])
] + [
    ("provider_error_type", "prompt_too_long"),
    ("rejection_class", "invalid_request_error"),
]


# --- the sets ---------------------------------------------------------------------------------


def test_the_sets_are_the_ones_written_in_the_decision_record():
    assert isinstance(_types._PROVIDER_ERROR_TYPES, frozenset)
    assert isinstance(_types._REJECTION_CLASSES, frozenset)
    assert _types._PROVIDER_ERROR_TYPES == frozenset(
        {
            "invalid_request_error",
            "authentication_error",
            "billing_error",
            "permission_error",
            "not_found_error",
            "conflict_error",
            "request_too_large",
            "rate_limit_error",
            "api_error",
            "timeout_error",
            "overloaded_error",
            "other",
        }
    )
    assert _types._REJECTION_CLASSES == frozenset(
        {
            "prompt_too_long",
            "tool_result_missing",
            "prefill_unsupported",
            "thinking_config_unsupported",
            "thinking_block_invalid",
            "tool_choice_unsupported",
            "tool_type_unsupported",
            "image_too_large",
            "other",
        }
    )


@pytest.mark.parametrize("build", BUILDERS, ids=BUILDER_IDS)
def test_every_member_of_both_sets_is_accepted(build):
    for value in sorted(_types._PROVIDER_ERROR_TYPES):
        assert build(provider_error_type=value).provider_error_type == value
    for value in sorted(_types._REJECTION_CLASSES):
        assert build(rejection_class=value).rejection_class == value


@pytest.mark.parametrize(("field", "value"), NOT_MEMBERS)
@pytest.mark.parametrize("build", BUILDERS, ids=BUILDER_IDS)
def test_a_value_outside_its_set_is_refused(build, field, value):
    with pytest.raises(ValueError):
        build(**{field: value})


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("build", BUILDERS, ids=BUILDER_IDS)
def test_a_refused_value_is_not_echoed_in_the_error(build, field):
    with pytest.raises(ValueError) as caught:
        build(**{field: SENTINEL})

    assert field in str(caught.value)
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value.args)


# --- AccountRoutingError ----------------------------------------------------------------------


def test_a_rejection_error_names_its_type_and_class():
    error = AccountRoutingError(
        RouterErrorCode.REQUEST_REJECTED,
        account="a",
        attempt_id=ATTEMPT,
        http_status=400,
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    assert str(error).endswith("status=400 type=invalid_request_error class=prompt_too_long")
    assert str(error) == (
        "request_rejected account=a attempt=00000000-0000-4000-8000-000000000001"
        " status=400 type=invalid_request_error class=prompt_too_long"
    )
    assert error.provider_error_type == "invalid_request_error"
    assert error.rejection_class == "prompt_too_long"


def test_without_the_two_fields_the_message_is_unchanged():
    error = AccountRoutingError(
        RouterErrorCode.REQUEST_REJECTED, account="a", attempt_id=ATTEMPT, http_status=400
    )

    assert str(error) == (
        "request_rejected account=a attempt=00000000-0000-4000-8000-000000000001 status=400"
    )
    assert str(AccountRoutingError(RouterErrorCode.BUDGET_EXHAUSTED)) == "budget_exhausted"
    assert error.provider_error_type is None
    assert error.rejection_class is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({"provider_error_type": "api_error"}, "request_rejected status=400 type=api_error"),
        (
            {"rejection_class": "image_too_large"},
            "request_rejected status=400 class=image_too_large",
        ),
    ],
)
def test_one_present_field_joins_the_message_alone(given, expected):
    assert str(build_error(http_status=400, **given)) == expected


# --- UnknownOutcome ---------------------------------------------------------------------------


def test_the_one_argument_outcome_still_works():
    outcome = UnknownOutcome("rate_limited")

    assert outcome.reason_code == "rate_limited"
    assert outcome.provider_error_type is None
    assert outcome.rejection_class is None
    assert outcome == UnknownOutcome("rate_limited")


def test_a_rejected_outcome_carries_both_fields():
    outcome = UnknownOutcome(
        "request_rejected",
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    assert outcome.reason_code == "request_rejected"
    assert outcome.provider_error_type == "invalid_request_error"
    assert outcome.rejection_class == "prompt_too_long"
    assert outcome != UnknownOutcome("request_rejected")


@pytest.mark.parametrize(
    "given",
    [
        {"provider_error_type": "other"},
        {"rejection_class": "other"},
        {"provider_error_type": "invalid_request_error", "rejection_class": "prompt_too_long"},
    ],
)
@pytest.mark.parametrize(
    "reason",
    ["rate_limited", "auth_required", "provider_error", "transport_error", "usage_incomplete"],
)
def test_the_fields_are_refused_on_any_other_outcome(reason, given):
    with pytest.raises(ValueError):
        UnknownOutcome(reason, **given)


# --- the internal routed-attempt error and its public form ------------------------------------


@pytest.mark.parametrize("error_type", [_RoutedNoRetryError, _RoutedRetryableError])
def test_the_routed_attempt_error_carries_both_fields(error_type):
    error = error_type(
        "request_rejected",
        account="a",
        attempt_id=ATTEMPT,
        http_status=400,
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    assert error.provider_error_type == "invalid_request_error"
    assert error.rejection_class == "prompt_too_long"
    assert error.args == ("request_rejected",)
    assert str(error) == "request_rejected"


def test_the_routed_attempt_error_defaults_both_fields_to_none():
    error = _RoutedNoRetryError("request_rejected", http_status=400)

    assert error.provider_error_type is None
    assert error.rejection_class is None
    assert str(error) == "request_rejected"


def test_the_public_failure_passes_both_fields_on():
    error = _RoutedNoRetryError(
        "request_rejected",
        account="a",
        attempt_id=ATTEMPT,
        http_status=400,
        provider_error_type="invalid_request_error",
        rejection_class="prompt_too_long",
    )

    failure = Endpoint._public_routed_failure(error)

    assert isinstance(failure, AccountRoutingError)
    assert failure.code is RouterErrorCode.REQUEST_REJECTED
    assert failure.provider_error_type == "invalid_request_error"
    assert failure.rejection_class == "prompt_too_long"
    assert str(failure).endswith("status=400 type=invalid_request_error class=prompt_too_long")


def test_the_public_failure_without_the_fields_keeps_its_message():
    error = _RoutedNoRetryError(
        "request_rejected", account="a", attempt_id=ATTEMPT, http_status=400
    )

    failure = Endpoint._public_routed_failure(error)

    assert str(failure) == (
        "request_rejected account=a attempt=00000000-0000-4000-8000-000000000001 status=400"
    )
    assert failure.provider_error_type is None
    assert failure.rejection_class is None


# --- AccountRouter.settle_unknown -------------------------------------------------------------


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
