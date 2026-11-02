# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared value types for routed provider accounts (ADR-0126).

Money is integer nanodollars throughout; no value here holds key material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from lionagi.ln.types import Enum

__all__ = (
    "INT64_MAX",
    "NANODOLLARS_PER_USD",
    "AccountRoutingError",
    "AccountSpec",
    "CompleteUsage",
    "Evidence",
    "ProvenNotSent",
    "Quote",
    "Reservation",
    "RouterErrorCode",
    "Scope",
    "ScopeKind",
    "SettleResult",
    "UnknownOutcome",
)

NANODOLLARS_PER_USD = 10**9
INT64_MAX = 2**63 - 1

ScopeKind = Literal["branch", "imodel", "endpoint"]
SettleResult = Literal["settled", "already_settled", "still_open"]


class RouterErrorCode(str, Enum):
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_INVALID = "credential_invalid"
    CREDENTIAL_LOOKUP_FAILED = "credential_lookup_failed"
    CREDENTIAL_CONFLICT = "credential_conflict"
    UNSUPPORTED_PRICING = "unsupported_pricing"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ACCOUNT_BLOCKED = "account_blocked"
    ACCOUNT_CONFIG_CONFLICT = "account_config_conflict"
    LEDGER_BUSY = "ledger_busy"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    DUPLICATE_ATTEMPT = "duplicate_attempt"
    SETTLEMENT_CONFLICT = "settlement_conflict"
    CEILING_EXCEEDED = "ceiling_exceeded"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    REQUEST_REJECTED = "request_rejected"
    PROVIDER_ERROR = "provider_error"
    TRANSPORT_ERROR = "transport_error"


# The closed sets written in ADR-0126 Amendments 7 and 8. A provider table added there
# extends _PROVIDER_ERROR_TYPES.
_PROVIDER_ERROR_TYPES = frozenset(
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
_REJECTION_CLASSES = frozenset(
    {
        "prompt_too_long",
        "tool_result_missing",
        "prefill_unsupported",
        "thinking_config_unsupported",
        "thinking_block_invalid",
        "tool_choice_unsupported",
        "tool_type_unsupported",
        "image_too_large",
        "credit_balance_low",
        "spend_limit_reached",
        "other",
    }
)


def _check_member(field: str, value: Any, allowed: frozenset[str]) -> None:
    """Refuse a value that is neither None nor in ``allowed``; name the field, never the value."""
    if value is not None and not (isinstance(value, str) and value in allowed):
        raise ValueError(f"{field} is not one of its allowed values")


class AccountRoutingError(Exception):
    """A routed call's refusal or failure, carrying only a code and validated fields.

    The message is built from the code and those validated fields alone (ids, an HTTP
    status, and two strings checked against closed sets), so no provider text, request
    content or key material can reach it. Raise it ``from None`` where it replaces
    another exception, so a traceback does not print the replaced one. That exception
    stays reachable as ``__context__``, so it must carry no key material either.

    ``provider_error_type`` and ``rejection_class`` are accepted only with
    ``RouterErrorCode.REQUEST_REJECTED``: with any other code they raise ``ValueError``.
    """

    def __init__(
        self,
        code: RouterErrorCode,
        *,
        account: str | None = None,
        attempt_id: UUID | None = None,
        http_status: int | None = None,
        provider_error_type: str | None = None,
        rejection_class: str | None = None,
    ) -> None:
        _check_member("provider_error_type", provider_error_type, _PROVIDER_ERROR_TYPES)
        _check_member("rejection_class", rejection_class, _REJECTION_CLASSES)
        if (
            provider_error_type is not None or rejection_class is not None
        ) and code is not RouterErrorCode.REQUEST_REJECTED:
            raise ValueError(
                "provider_error_type and rejection_class are set only on a request_rejected error"
            )
        self.code = code
        self.account = account
        self.attempt_id = attempt_id
        self.http_status = http_status
        self.provider_error_type = provider_error_type
        self.rejection_class = rejection_class
        parts = [code.value]
        if account is not None:
            parts.append(f"account={account}")
        if attempt_id is not None:
            parts.append(f"attempt={attempt_id}")
        if http_status is not None:
            parts.append(f"status={http_status}")
        if provider_error_type is not None:
            parts.append(f"type={provider_error_type}")
        if rejection_class is not None:
            parts.append(f"class={rejection_class}")
        super().__init__(" ".join(parts))


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """One configured account as the ledger sees it: no key, only the name of its reference."""

    name: str
    key_env: str
    budget_nusd: int


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    id: UUID
    provider: str


@dataclass(frozen=True, slots=True)
class Quote:
    requested_model: str
    ceiling_nusd: int
    price_revision: str
    # sha256 hex of the final credential-free request bytes
    request_digest: str


@dataclass(frozen=True, slots=True)
class Reservation:
    attempt_id: UUID
    scope: Scope
    account: str
    # "YYYY-MM", the UTC month sampled inside the admission transaction
    month_utc: str
    quote: Quote


@dataclass(frozen=True, slots=True)
class CompleteUsage:
    """Complete billing evidence for one attempt, already priced at the attempt's revision."""

    served_model: str
    input_tokens: int
    cache_read_input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    cost_nusd: int
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProvenNotSent:
    """The transport's own proof that the request never left this process.

    ``caller_cancelled`` is true when the caller's own cancellation, not a fault on
    the account's side, is why the request never left; the ledger then keeps what it
    knows about the account's cached prefixes.
    """

    caller_cancelled: bool = False


@dataclass(frozen=True, slots=True)
class UnknownOutcome:
    """Settlement evidence that closes an attempt with a reason code and no usage.

    ``provider_error_type`` and ``rejection_class`` are closed-set strings that only a
    ``request_rejected`` outcome may carry. The ledger records ``reason_code`` only.
    """

    reason_code: str
    provider_error_type: str | None = None
    rejection_class: str | None = None

    def __post_init__(self) -> None:
        _check_member("provider_error_type", self.provider_error_type, _PROVIDER_ERROR_TYPES)
        _check_member("rejection_class", self.rejection_class, _REJECTION_CLASSES)
        if (
            self.provider_error_type is not None or self.rejection_class is not None
        ) and self.reason_code != "request_rejected":
            raise ValueError(
                "provider_error_type and rejection_class are set only on a request_rejected outcome"
            )


Evidence = CompleteUsage | ProvenNotSent | UnknownOutcome
