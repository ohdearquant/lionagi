# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private SQLite accounting for routed provider attempts (ADR-0126 D4)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from pathlib import Path
from typing import Generic, TypeVar, cast
from uuid import UUID

from lionagi.ln import now_utc
from lionagi.ln.concurrency import move_on_after, run_sync
from lionagi.service.accounts._types import (
    _PROVIDER_ERROR_TYPES,
    _REJECTION_CLASSES,
    INT64_MAX,
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    Evidence,
    PricingInputs,
    ProvenNotSent,
    Quote,
    RejectionZero,
    Reservation,
    RouterErrorCode,
    Scope,
    SettleResult,
    StreamUsageEstimate,
    UnknownOutcome,
    _check_member,
)

__all__ = ("CredentialLedger", "AccountStatus", "default_ledger_path")

_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")
_INT64_MIN = -(INT64_MAX + 1)
_TABLES = {"account", "month", "binding", "attempt", "adjustment"}
# Attempt columns added after a ledger could already exist; opening an older ledger adds them.
_REJECTION_COLUMNS = ("provider_error_type", "rejection_class")
_EVIDENCE_COLUMNS = ("settlement_evidence_json", "http_status")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)
# How long one ledger call waits for a worker thread before it is refused as busy. The
# bound covers admission only; the SQLite lock wait is set by busy_timeout_s.
_ADMISSION_BOUND_S = 5.0
# Expired rows are deleted by text order with a margin wider than an offset moves the text (a day),
# so an isoformat() row goes only after it expired; a row lost early only loses a routing hint.
_WARM_PURGE_MARGIN = timedelta(hours=25)


class _TimedCall(Generic[_T]):
    """Run an operation in a worker thread and record when it started and finished.

    The operation's result or error is kept here and the call itself returns normally,
    so a caller whose wait was cancelled can still read the outcome of a write that ran.
    """

    __slots__ = ("operation", "started_at", "finished_at", "result", "error")

    def __init__(self, operation: Callable[[], _T]) -> None:
        self.operation = operation
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.result: _T | None = None
        self.error: Exception | None = None

    def __call__(self) -> None:
        self.started_at = time.monotonic()
        try:
            self.result = self.operation()
        except Exception as exc:
            self.error = exc
        finally:
            self.finished_at = time.monotonic()


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """Accounting totals for one provider account in one UTC month."""

    provider: str
    name: str
    month_utc: str
    budget_nusd: int | None
    settled_nusd: int
    adjustments_nusd: int
    open_ceiling_nusd: int
    open_attempts: int
    block_reason: str | None


def default_ledger_path() -> Path:
    """Return the private ledger path beside the global settings file."""
    return Path.home() / ".lionagi" / "credential-ledger.sqlite3"


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _routing_error(
    code: RouterErrorCode,
    *,
    account: str | None = None,
    attempt_id: UUID | None = None,
) -> AccountRoutingError:
    return AccountRoutingError(code, account=account, attempt_id=attempt_id)


def _checked_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f"invalid {label}")
    return value


def _validate_reason(reason: object) -> str:
    if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
        raise ValueError("invalid reason code")
    return reason


def _validate_source(source_ref: object) -> str:
    if not isinstance(source_ref, str) or not source_ref or len(source_ref) > 512:
        raise ValueError("invalid source reference")
    return source_ref


def _validate_month(month_utc: object) -> str:
    if not isinstance(month_utc, str) or _MONTH_RE.fullmatch(month_utc) is None:
        raise ValueError("invalid UTC month")
    return month_utc


def _month_and_iso(clock: Callable[[], datetime]) -> tuple[str, str]:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    value = value.astimezone(timezone.utc)
    # A month is a credit period, which starts when the provider refreshes the credit at 00:00 UTC
    # on a calendar month's last day (ADR-0126 Amendment 3): the one keyed 2026-11 runs from
    # 2026-10-31T00:00Z to 2026-11-30T00:00Z.
    return (value + timedelta(days=1)).strftime("%Y-%m"), value.isoformat()


def _aware_instant(value: object) -> datetime | None:
    """Parse stored text as an instant; None unless it is ISO text carrying an offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _clear_warm_prefix(connection: sqlite3.Connection, provider: str, account: str) -> None:
    """Forget every warm-prefix record of one account."""
    connection.execute(
        "DELETE FROM warm_prefix WHERE provider = ? AND account = ?",
        (provider, account),
    )


def _warm_accounts(
    connection: sqlite3.Connection, provider: str, cert: str, now: datetime
) -> set[str]:
    """Return the accounts holding an unexpired warm-prefix record for ``cert``.

    A record whose expiry is not offset-carrying ISO text counts as absent.
    """
    rows = connection.execute(
        "SELECT account, expires_at FROM warm_prefix WHERE provider = ? AND cert = ?",
        (provider, cert),
    ).fetchall()
    names: set[str] = set()
    for name, expires_at in rows:
        expiry = _aware_instant(expires_at)
        if expiry is not None and expiry > now:
            names.add(name)
    return names


def _record_warm_prefix(
    connection: sqlite3.Connection,
    provider: str,
    account: str,
    cert: str,
    usage: CompleteUsage,
    created_at: object,
    hour_after_key: bool,
    server_tools: bool,
) -> None:
    """Record or refresh the warm prefix a completed attempt shows for its account.

    A one-hour cache write stores the stable prefix length (unknown when the request
    carried a later cache point or server tools); a request with no write whose cache
    read equals the stored length refreshes the expiry. Nothing is recorded for a
    blocked account or an attempt time that is not an aware instant.
    """
    state = connection.execute(
        "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
        (provider, account),
    ).fetchone()
    if state is None or state[0] is not None:
        return
    started = _aware_instant(created_at)
    if started is None:
        return
    expires = started + timedelta(hours=1)
    stored = connection.execute(
        "SELECT stable_tokens, expires_at FROM warm_prefix "
        "WHERE provider = ? AND account = ? AND cert = ?",
        (provider, account, cert),
    ).fetchone()
    stored_expiry = _aware_instant(stored[1]) if stored is not None else None
    if stored_expiry is None:
        stored = None
    if usage.cache_write_1h_tokens > 0:
        new_stable: int | None = None
        if not (hour_after_key or server_tools):
            new_stable = usage.cache_read_input_tokens + usage.cache_write_1h_tokens
        if stored is None:
            stable, expiry = new_stable, expires
        else:
            stable = new_stable if new_stable is not None else stored[0]
            expiry = max(stored_expiry, expires)
    else:
        if server_tools or stored is None or stored[0] is None:
            return
        if usage.cache_read_input_tokens != stored[0]:
            return
        stable, expiry = stored[0], max(stored_expiry, expires)
    connection.execute(
        "DELETE FROM warm_prefix WHERE provider = ? AND expires_at < ?",
        (provider, (started - _WARM_PURGE_MARGIN).astimezone(timezone.utc).isoformat()),
    )
    connection.execute(
        "INSERT OR REPLACE INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (provider, account, cert, stable, expiry.isoformat()),
    )


def _validate_quote(quote: Quote) -> None:
    if not isinstance(quote, Quote):
        raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)
    if (
        type(quote.ceiling_nusd) is not int
        or not 1 <= quote.ceiling_nusd <= INT64_MAX
        or not isinstance(quote.price_revision, str)
        or not quote.price_revision
        or not isinstance(quote.requested_model, str)
        or not quote.requested_model
        or not isinstance(quote.request_digest, str)
        or _DIGEST_RE.fullmatch(quote.request_digest) is None
    ):
        raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)


def _usage_json(evidence: CompleteUsage) -> tuple[str, str | None]:
    for name in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "output_tokens",
    ):
        value = getattr(evidence, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid {name}")
    _checked_int(evidence.cost_nusd, minimum=0, maximum=INT64_MAX, label="cost")
    if not isinstance(evidence.served_model, str) or not evidence.served_model:
        raise ValueError("invalid served model")
    usage = {
        "served_model": evidence.served_model,
        "input_tokens": evidence.input_tokens,
        "cache_read_input_tokens": evidence.cache_read_input_tokens,
        "cache_write_5m_tokens": evidence.cache_write_5m_tokens,
        "cache_write_1h_tokens": evidence.cache_write_1h_tokens,
        "output_tokens": evidence.output_tokens,
    }
    request_id = evidence.request_id
    if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
        request_id = None
    return json.dumps(usage, sort_keys=True, separators=(",", ":")), request_id


def _sum_rows(rows: list[tuple[int]]) -> int:
    return sum(row[0] for row in rows)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _stored_evidence(
    value: str | None,
    *,
    revision: str,
    model: str,
    outcome: str | None,
    cost: int | None,
    provider_error_type: str | None,
    rejection_class: str | None,
    request_id: str | None,
) -> dict[str, object] | None:
    """Read and validate the complete versioned settlement envelope."""
    if value is None:
        return None
    try:

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result

        def reject_float(_):
            raise ValueError("float")

        envelope = json.loads(
            value, object_pairs_hook=pairs, parse_float=reject_float, parse_constant=reject_float
        )
        top = {
            "schema_version",
            "rule_revision",
            "quote",
            "basis",
            "origin",
            "source_ref",
            "first_failure",
            "evidence",
        }
        if not isinstance(envelope, dict) or set(envelope) != top:
            raise ValueError("envelope keys")
        if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
            raise ValueError("version")
        if envelope["rule_revision"] != "adr0126-am9-2026-11-02-v1":
            raise ValueError("revision")
        quote = envelope["quote"]
        if quote is not None:
            if not isinstance(quote, dict) or set(quote) != {
                "requested_model",
                "price_revision",
                "pricing_inputs",
                "quote_origin",
            }:
                raise ValueError("quote")
            if quote["requested_model"] != model or quote["price_revision"] != revision:
                raise ValueError("quote identity")
            pi = quote["pricing_inputs"]
            if not isinstance(pi, dict) or set(pi) != {
                "output_bound",
                "cache_ttls",
                "single_ttl",
                "feature_profile",
                "tier_selector",
                "output_rate_max",
            }:
                raise ValueError("pricing inputs")
            _checked_int(pi["output_bound"], minimum=1, maximum=INT64_MAX, label="output bound")
            if type(pi["cache_ttls"]) is not list or pi["cache_ttls"] not in (
                [],
                ["1h"],
                ["5m"],
                ["5m", "1h"],
            ):
                raise ValueError("TTLs")
            if pi["single_ttl"] != (pi["cache_ttls"][0] if len(pi["cache_ttls"]) == 1 else None):
                raise ValueError("single TTL")
            if (
                pi["feature_profile"] != "messages_standard_v1"
                or pi["tier_selector"] != "max_applicable_at_reserve"
            ):
                raise ValueError("selector")
            rate = pi["output_rate_max"]
            if not isinstance(rate, dict) or set(rate) != {"numerator", "denominator"}:
                raise ValueError("rate")
            _checked_int(rate["numerator"], minimum=0, maximum=INT64_MAX, label="rate")
            _checked_int(rate["denominator"], minimum=1, maximum=INT64_MAX, label="rate")
            if math.gcd(rate["numerator"], rate["denominator"]) != 1:
                raise ValueError("unreduced rate")
            if quote["quote_origin"] not in ("reservation", "operator"):
                raise ValueError("quote origin")
        basis = envelope["basis"]
        if basis is None:
            if any(
                envelope[key] is not None
                for key in ("origin", "source_ref", "first_failure", "evidence")
            ):
                raise ValueError("open envelope")
        else:
            if basis not in ("rejection_zero", "stream_usage_estimate") or envelope[
                "origin"
            ] not in ("runtime", "operator"):
                raise ValueError("basis/origin")
            if outcome != basis:
                raise ValueError("outcome")
            if (envelope["origin"] == "runtime") != (envelope["source_ref"] is None):
                raise ValueError("source reference")
            if envelope["origin"] == "operator":
                source = envelope["source_ref"]
                if (
                    not isinstance(source, str)
                    or re.fullmatch(
                        r"evidence:[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source, flags=re.ASCII
                    )
                    is None
                ):
                    raise ValueError("operator source reference")
            first = envelope["first_failure"]
            if not isinstance(first, dict) or set(first) != {
                "reason",
                "provider_error_type",
                "rejection_class",
            }:
                raise ValueError("first failure")
            if first["reason"] is not None and (
                not isinstance(first["reason"], str)
                or _REASON_RE.fullmatch(first["reason"]) is None
            ):
                raise ValueError("first reason")
            _check_member(
                "provider_error_type", first["provider_error_type"], _PROVIDER_ERROR_TYPES
            )
            _check_member("rejection_class", first["rejection_class"], _REJECTION_CLASSES)
            ev = envelope["evidence"]
            if basis == "rejection_zero":
                if not isinstance(ev, dict) or set(ev) != {
                    "http_status",
                    "provider_error_type",
                    "rejection_class",
                    "original_reason",
                    "cost_nusd",
                }:
                    raise ValueError("rejection evidence")
                _checked_int(ev["http_status"], minimum=100, maximum=599, label="status")
                _checked_int(ev["cost_nusd"], minimum=0, maximum=INT64_MAX, label="cost")
                _check_member(
                    "provider_error_type", ev["provider_error_type"], _PROVIDER_ERROR_TYPES
                )
                _check_member("rejection_class", ev["rejection_class"], _REJECTION_CLASSES)
                expected = (
                    (400, "invalid_request_error", "request_rejected")
                    if ev["http_status"] == 400
                    else (529, "overloaded_error", "provider_error")
                    if ev["http_status"] == 529
                    else None
                )
                if (
                    expected is None
                    or (ev["http_status"], ev["provider_error_type"], ev["original_reason"])
                    != expected
                    or ev["cost_nusd"] != 0
                ):
                    raise ValueError("rejection values")
                if cost != 0:
                    raise ValueError("rejection row cost")
                prior_reason = first["reason"]
                if ev["http_status"] == 400:
                    if prior_reason is None and ev["rejection_class"] is None:
                        raise ValueError("missing normalized rejection class")
                    if prior_reason not in (None, "request_rejected"):
                        raise ValueError("rejection first reason")
                    if first["provider_error_type"] not in (None, "invalid_request_error"):
                        raise ValueError("rejection first type")
                    expected_type = first["provider_error_type"] or "invalid_request_error"
                    expected_class = (
                        first["rejection_class"]
                        if prior_reason == "request_rejected"
                        else ev["rejection_class"]
                    )
                    if (provider_error_type, rejection_class) != (expected_type, expected_class):
                        raise ValueError("rejection columns")
                elif (
                    ev["rejection_class"] is not None
                    or prior_reason not in (None, "provider_error")
                    or (first["provider_error_type"], first["rejection_class"]) != (None, None)
                    or (provider_error_type, rejection_class)
                    != (
                        first["provider_error_type"],
                        first["rejection_class"],
                    )
                ):
                    raise ValueError("overload columns")
            else:
                expected = {
                    "original_reason",
                    "served_model",
                    "counts",
                    "request_id",
                    "pricing_inputs",
                    "quote_origin",
                    "rates",
                    "observed_cost_nusd",
                    "margin_tokens",
                    "margin_nusd",
                    "cost_nusd",
                }
                if not isinstance(ev, dict) or set(ev) != expected or quote is None:
                    raise ValueError("stream evidence")
                counts = ev["counts"]
                count_names = {
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_write_5m_tokens",
                    "cache_write_1h_tokens",
                    "output_tokens",
                }
                if not isinstance(counts, dict) or set(counts) != count_names:
                    raise ValueError("counts")
                for name, count in counts.items():
                    _checked_int(count, minimum=0, maximum=INT64_MAX, label=name)
                for name in ("observed_cost_nusd", "margin_tokens", "margin_nusd", "cost_nusd"):
                    _checked_int(ev[name], minimum=0, maximum=INT64_MAX, label=name)
                if ev["cost_nusd"] != ev["observed_cost_nusd"] + ev["margin_nusd"]:
                    raise ValueError("stream amount")
                if (
                    ev["quote_origin"] not in ("reservation", "operator")
                    or ev["served_model"] != model
                ):
                    raise ValueError("stream provenance")
                if envelope["origin"] == "runtime" and ev["quote_origin"] != "reservation":
                    raise ValueError("runtime quote provenance")
                if (
                    ev["pricing_inputs"] != quote["pricing_inputs"]
                    or ev["quote_origin"] != quote["quote_origin"]
                ):
                    raise ValueError("stream quote correspondence")
                rates = ev["rates"]
                rate_names = {"input", "cache_read", "cache_write_5m", "cache_write_1h", "output"}
                if not isinstance(rates, dict) or set(rates) != rate_names:
                    raise ValueError("rates")
                for rate in rates.values():
                    if not isinstance(rate, dict) or set(rate) != {"numerator", "denominator"}:
                        raise ValueError("rate")
                    _checked_int(rate["numerator"], minimum=0, maximum=INT64_MAX, label="rate")
                    _checked_int(rate["denominator"], minimum=1, maximum=INT64_MAX, label="rate")
                    if math.gcd(rate["numerator"], rate["denominator"]) != 1:
                        raise ValueError("unreduced rate")
                envelope_request_id = ev["request_id"]
                if envelope_request_id is not None and (
                    not isinstance(envelope_request_id, str)
                    or _REQUEST_ID_RE.fullmatch(envelope_request_id) is None
                ):
                    envelope_request_id = None
                if ev["original_reason"] not in {
                    "cancelled",
                    "stream_closed",
                    "transport_error",
                    "stream_truncated",
                    "stream_error",
                }:
                    raise ValueError("stream reason")
                prior_reason = first["reason"]
                if prior_reason is not None and prior_reason != ev["original_reason"]:
                    raise ValueError("stream first reason")
                if (provider_error_type, rejection_class) != (
                    first["provider_error_type"],
                    first["rejection_class"],
                ):
                    raise ValueError("stream rejection columns")
                if request_id != envelope_request_id:
                    raise ValueError("stream request id")
                if cost != ev["cost_nusd"]:
                    raise ValueError("stream row cost")
        return envelope
    except Exception:
        raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE) from None


def _pricing_inputs_dict(inputs: PricingInputs) -> dict[str, object]:
    return {
        "output_bound": inputs.output_bound,
        "cache_ttls": list(inputs.cache_ttls),
        "single_ttl": inputs.single_ttl,
        "feature_profile": inputs.feature_profile,
        "tier_selector": inputs.tier_selector,
        "output_rate_max": {
            "numerator": inputs.output_rate_max.numerator,
            "denominator": inputs.output_rate_max.denominator,
        },
    }


def _quote_envelope(quote: Quote) -> str:
    if quote.pricing_inputs is None:
        return ""
    return _canonical_json(
        {
            "schema_version": 1,
            "rule_revision": "adr0126-am9-2026-11-02-v1",
            "quote": {
                "requested_model": quote.requested_model,
                "price_revision": quote.price_revision,
                "pricing_inputs": _pricing_inputs_dict(quote.pricing_inputs),
                "quote_origin": "reservation",
            },
            "basis": None,
            "origin": None,
            "source_ref": None,
            "first_failure": None,
            "evidence": None,
        }
    )


def _stream_evidence_dict(evidence: StreamUsageEstimate) -> dict[str, object]:
    return {
        "original_reason": evidence.original_reason,
        "served_model": evidence.served_model,
        "counts": {
            name: getattr(evidence.counts, name)
            for name in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_write_5m_tokens",
                "cache_write_1h_tokens",
                "output_tokens",
            )
        },
        "request_id": evidence.request_id,
        "pricing_inputs": _pricing_inputs_dict(evidence.pricing_inputs),
        "quote_origin": evidence.quote_origin,
        "rates": {
            name: {
                "numerator": getattr(evidence.rates, name).numerator,
                "denominator": getattr(evidence.rates, name).denominator,
            }
            for name in ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")
        },
        "observed_cost_nusd": evidence.observed_cost_nusd,
        "margin_tokens": evidence.margin_tokens,
        "margin_nusd": evidence.margin_nusd,
        "cost_nusd": evidence.cost_nusd,
    }


class CredentialLedger:
    """Persist account bindings and attempt costs in a local SQLite file."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_s: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        price_revisions: Mapping[str, object] | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s
        self._clock = clock or (lambda: now_utc())
        from types import MappingProxyType

        self.price_revisions = MappingProxyType(dict(price_revisions or {}))

    async def _run(self, operation: Callable[[], _T]) -> _T:
        """Run one ledger operation in a worker thread.

        The wait for a worker thread is bounded by _ADMISSION_BOUND_S. If the operation never
        started when the bound fires, the call is refused as ledger_busy. If it started, its
        result or error is returned as usual, even when the bound fired while it ran. Each
        call emits one DEBUG event with its admission wait, SQLite wait and outcome.
        """
        call = _TimedCall(operation)
        requested_at = time.monotonic()
        outcome = "committed"
        try:
            with move_on_after(_ADMISSION_BOUND_S):
                await run_sync(call)
            if call.started_at is None:
                outcome = "ledger_busy"
                raise _routing_error(RouterErrorCode.LEDGER_BUSY)
            try:
                if call.error is not None:
                    raise call.error
                return cast(_T, call.result)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    outcome = "ledger_busy"
                    raise _routing_error(RouterErrorCode.LEDGER_BUSY) from None
                outcome = type(exc).__name__
                raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE) from None
            except (sqlite3.DatabaseError, OSError) as exc:
                outcome = type(exc).__name__
                raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE) from None
        except BaseException as exc:
            if outcome == "committed":
                outcome = type(exc).__name__
            raise
        finally:
            now = time.monotonic()
            fields: dict[str, object] = {
                "admission_wait_s": (call.started_at if call.started_at is not None else now)
                - requested_at,
                "outcome": outcome,
            }
            if call.started_at is not None:
                fields["sqlite_wait_s"] = (
                    call.finished_at if call.finished_at is not None else now
                ) - call.started_at
            _LOGGER.debug("credential ledger call finished", extra=fields)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_s,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_s * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            self._ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _index_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }

    @staticmethod
    def _attempt_columns(connection: sqlite3.Connection) -> set[str]:
        return {row[1] for row in connection.execute('PRAGMA table_info("attempt")')}

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        # Unlocked, these reads can straddle another process's schema commit, so
        # they only let a complete ledger through, warm-prefix indexes and rejection
        # columns included; every other answer is read again, together, under the
        # write lock before anything is refused or created.
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] == 1
            and (_TABLES | {"warm_prefix"}).issubset(self._table_names(connection))
            and {"warm_prefix_cert", "warm_prefix_expiry"}.issubset(self._index_names(connection))
            and set(_REJECTION_COLUMNS).issubset(self._attempt_columns(connection))
            and set(_EVIDENCE_COLUMNS).issubset(self._attempt_columns(connection))
        ):
            return

        # A ledger written before warm-prefix tracking is still version 1 and has none
        # of this table, so the table is added whenever it is missing and a reader that
        # predates it never needs it. The same holds for the attempt columns that record
        # a rejected request: each missing one is added, after the columns are read again
        # under this lock because another process may have added them first.
        warm_prefix_ddl = """
            CREATE TABLE IF NOT EXISTS warm_prefix (
                provider TEXT NOT NULL,
                account TEXT NOT NULL,
                cert TEXT NOT NULL,
                stable_tokens INTEGER,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (provider, account, cert),
                FOREIGN KEY (provider, account) REFERENCES account(provider, name)
            )
            """
        warm_prefix_indexes = (
            "CREATE INDEX IF NOT EXISTS warm_prefix_cert ON warm_prefix (provider, cert)",
            "CREATE INDEX IF NOT EXISTS warm_prefix_expiry ON warm_prefix (provider, expires_at)",
        )

        with _transaction(connection):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = self._table_names(connection)
            if version == 1:
                if not _TABLES.issubset(tables):
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
                connection.execute(warm_prefix_ddl)
                for statement in warm_prefix_indexes:
                    connection.execute(statement)
                columns = self._attempt_columns(connection)
                for column in _REJECTION_COLUMNS:
                    if column not in columns:
                        connection.execute(f'ALTER TABLE attempt ADD COLUMN "{column}" TEXT')
                columns = self._attempt_columns(connection)
                if "settlement_evidence_json" not in columns:
                    connection.execute(
                        'ALTER TABLE attempt ADD COLUMN "settlement_evidence_json" TEXT'
                    )
                if "http_status" not in columns:
                    connection.execute('ALTER TABLE attempt ADD COLUMN "http_status" INTEGER')
                return
            if version != 0 or tables:
                raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
            connection.execute(
                """
                CREATE TABLE account (
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key_env TEXT NOT NULL,
                    block_reason TEXT,
                    PRIMARY KEY (provider, name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE month (
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    budget_nusd INTEGER NOT NULL
                        CHECK (typeof(budget_nusd) = 'integer' AND budget_nusd > 0),
                    PRIMARY KEY (provider, name, month_utc),
                    FOREIGN KEY (provider, name) REFERENCES account(provider, name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE binding (
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    PRIMARY KEY (scope_kind, scope_id, provider),
                    FOREIGN KEY (provider, account) REFERENCES account(provider, name)
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE attempt (
                    id TEXT PRIMARY KEY,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    ceiling_nusd INTEGER NOT NULL
                        CHECK (typeof(ceiling_nusd) = 'integer'
                               AND ceiling_nusd BETWEEN 1 AND {INT64_MAX}),
                    price_revision TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('open', 'settled')),
                    cost_nusd INTEGER,
                    usage_json TEXT,
                    request_id TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    provider_error_type TEXT,
                    rejection_class TEXT,
                    settlement_evidence_json TEXT,
                    http_status INTEGER,
                    CHECK (
                        (state = 'settled' AND typeof(cost_nusd) = 'integer'
                         AND cost_nusd >= 0)
                        OR (state = 'open' AND cost_nusd IS NULL)
                    ),
                    FOREIGN KEY (provider, account) REFERENCES account(provider, name),
                    FOREIGN KEY (provider, account, month_utc)
                        REFERENCES month(provider, name, month_utc)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE adjustment (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('correction', 'reconciliation', 'unblock')
                    ),
                    attempt_id TEXT,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    month_utc TEXT,
                    delta_nusd INTEGER NOT NULL CHECK (typeof(delta_nusd) = 'integer'),
                    source_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Every admission sums one account's month; without this each one scans
            # every attempt the file has ever held.
            connection.execute(
                "CREATE INDEX attempt_by_account_month "
                "ON attempt(provider, account, month_utc, state)"
            )
            connection.execute(
                "CREATE INDEX adjustment_by_account_month "
                "ON adjustment(provider, account, month_utc)"
            )
            connection.execute(warm_prefix_ddl)
            for statement in warm_prefix_indexes:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _spend_parts(
        connection: sqlite3.Connection,
        provider: str,
        account: str,
        month_utc: str,
    ) -> tuple[int, int, int, int]:
        settled = _sum_rows(
            connection.execute(
                """
                SELECT cost_nusd FROM attempt
                WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'settled'
                """,
                (provider, account, month_utc),
            ).fetchall()
        )
        adjustments = _sum_rows(
            connection.execute(
                """
                SELECT delta_nusd FROM adjustment
                WHERE provider = ? AND account = ? AND month_utc = ? AND kind = 'correction'
                """,
                (provider, account, month_utc),
            ).fetchall()
        )
        open_rows = connection.execute(
            """
            SELECT ceiling_nusd FROM attempt
            WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'open'
            """,
            (provider, account, month_utc),
        ).fetchall()
        opened = sum(row[0] for row in open_rows)
        scopes = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT scope_kind, scope_id FROM attempt
                WHERE provider = ? AND account = ? AND month_utc = ? AND state = 'open'
            )
            """,
            (provider, account, month_utc),
        ).fetchone()[0]
        spent = settled + adjustments
        if settled > INT64_MAX or not _INT64_MIN <= adjustments <= INT64_MAX or opened > INT64_MAX:
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=account)
        if spent < 0:
            raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=account)
        return settled, adjustments, opened, scopes

    @staticmethod
    def _fits(spent: int, opened: int, ceiling: int, budget: int) -> bool:
        total = spent + opened + ceiling
        return total <= INT64_MAX and total <= budget

    @staticmethod
    def _ratio_order(
        left: tuple[AccountSpec, int, int, int],
        right: tuple[AccountSpec, int, int, int],
    ) -> int:
        left_spec, left_spent, left_open, left_scopes = left
        right_spec, right_spent, right_open, right_scopes = right
        left_total = left_spent + left_open
        right_total = right_spent + right_open
        difference = left_total * right_spec.budget_nusd - right_total * left_spec.budget_nusd
        if difference:
            return -1 if difference < 0 else 1
        if left_scopes != right_scopes:
            return -1 if left_scopes < right_scopes else 1
        if left_spec.name != right_spec.name:
            return -1 if left_spec.name < right_spec.name else 1
        return 0

    def _reserve_sync(
        self,
        scope: Scope,
        attempt_id: UUID,
        quote: Quote,
        accounts: Sequence[AccountSpec],
        cert: str | None = None,
    ) -> Reservation:
        _validate_quote(quote)
        if quote.pricing_inputs is not None:
            inputs = quote.pricing_inputs
            if not isinstance(inputs, PricingInputs):
                raise ValueError("invalid pricing inputs")
            inputs = PricingInputs(
                inputs.output_bound,
                inputs.cache_ttls,
                inputs.single_ttl,
                inputs.feature_profile,
                inputs.tier_selector,
                inputs.output_rate_max,
            )
            revision = self.price_revisions.get(quote.price_revision)
            if revision is None:
                raise _routing_error(RouterErrorCode.UNSUPPORTED_PRICING)
            model = revision.models.get(quote.requested_model)
            if model is None:
                raise _routing_error(RouterErrorCode.UNSUPPORTED_PRICING)
            if inputs.output_bound > model.max_output_tokens:
                raise ValueError("output bound exceeds model maximum")
            geo = max(revision.geo_multipliers.values())
            max_out = max(tier.output for tier in model.tiers) * geo
            if (inputs.output_rate_max.numerator, inputs.output_rate_max.denominator) != (
                max_out.numerator,
                max_out.denominator,
            ):
                raise ValueError("maximum output rate differs from retained revision")
            p_in = (
                max(
                    rate
                    for tier in model.tiers
                    for rate in (
                        tier.input,
                        tier.read,
                        *([tier.write_5m] if "5m" in inputs.cache_ttls else []),
                        *([tier.write_1h] if "1h" in inputs.cache_ttls else []),
                    )
                )
                * geo
            )
            expected_ceiling = model.max_input_tokens * p_in + inputs.output_bound * max_out
            expected_ceiling = (
                expected_ceiling.numerator + expected_ceiling.denominator - 1
            ) // expected_ceiling.denominator
            if quote.ceiling_nusd != expected_ceiling:
                raise ValueError("quote ceiling differs from retained pricing inputs")
        with closing(self._connect()) as connection:
            with _transaction(connection):
                if not accounts:
                    raise ValueError("at least one account is required")
                specs: dict[str, AccountSpec] = {}
                for spec in accounts:
                    if type(spec.budget_nusd) is not int or not 1 <= spec.budget_nusd <= INT64_MAX:
                        raise ValueError("invalid account budget")
                    previous = specs.get(spec.name)
                    if previous is not None and previous != spec:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    specs[spec.name] = spec

                month_utc, created_at = _month_and_iso(self._clock)
                later = connection.execute(
                    "SELECT 1 FROM attempt WHERE month_utc > ? LIMIT 1", (month_utc,)
                ).fetchone()
                if later is not None:
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE)
                if connection.execute(
                    "SELECT 1 FROM attempt WHERE id = ?", (str(attempt_id),)
                ).fetchone():
                    raise _routing_error(
                        RouterErrorCode.DUPLICATE_ATTEMPT,
                        attempt_id=attempt_id,
                    )

                provider = scope.provider
                for spec in specs.values():
                    existing = connection.execute(
                        "SELECT key_env FROM account WHERE provider = ? AND name = ?",
                        (provider, spec.name),
                    ).fetchone()
                    if existing is not None and existing[0] != spec.key_env:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    month_row = connection.execute(
                        """
                        SELECT budget_nusd FROM month
                        WHERE provider = ? AND name = ? AND month_utc = ?
                        """,
                        (provider, spec.name, month_utc),
                    ).fetchone()
                    if month_row is not None and month_row[0] != spec.budget_nusd:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_CONFIG_CONFLICT,
                            account=spec.name,
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO account(provider, name, key_env) VALUES (?, ?, ?)",
                        (provider, spec.name, spec.key_env),
                    )

                binding = connection.execute(
                    """
                    SELECT account FROM binding
                    WHERE scope_kind = ? AND scope_id = ? AND provider = ?
                    """,
                    (scope.kind, str(scope.id), provider),
                ).fetchone()
                selected: AccountSpec | None = None
                if binding is not None:
                    bound_name = binding[0]
                    selected = specs.get(bound_name)
                    account_state = connection.execute(
                        "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                        (provider, bound_name),
                    ).fetchone()
                    if selected is None or account_state is None or account_state[0] is not None:
                        raise _routing_error(
                            RouterErrorCode.ACCOUNT_BLOCKED,
                            account=bound_name,
                            attempt_id=attempt_id,
                        )
                    settled, adjustments, opened, _ = self._spend_parts(
                        connection, provider, bound_name, month_utc
                    )
                    budget_row = connection.execute(
                        """
                        SELECT budget_nusd FROM month
                        WHERE provider = ? AND name = ? AND month_utc = ?
                        """,
                        (provider, bound_name, month_utc),
                    ).fetchone()
                    budget = selected.budget_nusd if budget_row is None else budget_row[0]
                    if not self._fits(settled + adjustments, opened, quote.ceiling_nusd, budget):
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            account=bound_name,
                            attempt_id=attempt_id,
                        )
                else:
                    candidates: list[tuple[AccountSpec, int, int, int]] = []
                    blocked_count = 0
                    for spec in specs.values():
                        account_state = connection.execute(
                            "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                            (provider, spec.name),
                        ).fetchone()
                        if account_state is None or account_state[0] is not None:
                            blocked_count += 1
                            continue
                        settled, adjustments, opened, scope_count = self._spend_parts(
                            connection, provider, spec.name, month_utc
                        )
                        spent = settled + adjustments
                        if self._fits(spent, opened, quote.ceiling_nusd, spec.budget_nusd):
                            candidates.append((spec, spent, opened, scope_count))
                    if not candidates:
                        if blocked_count == len(specs):
                            raise _routing_error(
                                RouterErrorCode.ACCOUNT_BLOCKED,
                                attempt_id=attempt_id,
                            )
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            attempt_id=attempt_id,
                        )
                    ordered = sorted(candidates, key=cmp_to_key(self._ratio_order))
                    pool = ordered
                    if cert is not None:
                        # Prefer an account that holds a live warm prefix for this credential
                        # and stays within nine tenths of its budget after this reservation.
                        warm_names = _warm_accounts(
                            connection, provider, cert, datetime.fromisoformat(created_at)
                        )
                        warm = [
                            entry
                            for entry in ordered
                            if entry[0].name in warm_names
                            and 10 * (entry[1] + entry[2] + quote.ceiling_nusd)
                            <= 9 * entry[0].budget_nusd
                        ]
                        if warm:
                            pool = warm
                    selected = pool[0][0]
                    connection.execute(
                        """
                        INSERT INTO binding(scope_kind, scope_id, provider, account)
                        VALUES (?, ?, ?, ?)
                        """,
                        (scope.kind, str(scope.id), provider, selected.name),
                    )

                connection.execute(
                    """
                    INSERT OR IGNORE INTO month(provider, name, month_utc, budget_nusd)
                    VALUES (?, ?, ?, ?)
                    """,
                    (provider, selected.name, month_utc, selected.budget_nusd),
                )
                connection.execute(
                    """
                    INSERT INTO attempt(
                        id, scope_kind, scope_id, provider, account, month_utc,
                        ceiling_nusd, price_revision, requested_model, request_digest,
                        state, cost_nusd, created_at, settlement_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)
                    """,
                    (
                        str(attempt_id),
                        scope.kind,
                        str(scope.id),
                        provider,
                        selected.name,
                        month_utc,
                        quote.ceiling_nusd,
                        quote.price_revision,
                        quote.requested_model,
                        quote.request_digest,
                        created_at,
                        _quote_envelope(quote) or None,
                    ),
                )
        return Reservation(attempt_id, scope, selected.name, month_utc, quote)

    async def reserve(
        self,
        scope: Scope,
        attempt_id: UUID,
        quote: Quote,
        accounts: Sequence[AccountSpec],
        *,
        cert: str | None = None,
    ) -> Reservation:
        """Reserve a quote and bind its scope before a provider request is sent.

        An unbound scope prefers an eligible account that holds a live warm prefix for
        ``cert``; a bound scope keeps its account and never reads warm-prefix state.
        """
        return await self._run(lambda: self._reserve_sync(scope, attempt_id, quote, accounts, cert))

    def _settle_sync(
        self,
        attempt_id: UUID,
        evidence: Evidence,
        cert: str | None = None,
        hour_after_key: bool = False,
        server_tools: bool = False,
        http_status: int | None = None,
    ) -> SettleResult:
        usage_json: str | None = None
        request_id: str | None = None
        provider_error_type: str | None = None
        rejection_class: str | None = None
        cost_nusd: int
        outcome: str
        if isinstance(evidence, CompleteUsage):
            usage_json, request_id = _usage_json(evidence)
            cost_nusd = evidence.cost_nusd
            outcome = "complete"
        elif isinstance(evidence, ProvenNotSent):
            cost_nusd = 0
            outcome = "not_sent"
        elif isinstance(evidence, UnknownOutcome):
            outcome = _validate_reason(evidence.reason_code)
            _check_member(
                "provider_error_type", evidence.provider_error_type, _PROVIDER_ERROR_TYPES
            )
            _check_member("rejection_class", evidence.rejection_class, _REJECTION_CLASSES)
            provider_error_type = evidence.provider_error_type
            rejection_class = evidence.rejection_class
            cost_nusd = 0
        elif isinstance(evidence, RejectionZero):
            cost_nusd = 0
            outcome = "rejection_zero"
        elif isinstance(evidence, StreamUsageEstimate):
            cost_nusd = evidence.cost_nusd
            outcome = "stream_usage_estimate"
        else:
            raise ValueError("invalid settlement evidence")

        with closing(self._connect()) as connection:
            conflict: tuple[str, str] | None = None
            result: SettleResult
            with _transaction(connection):
                row = connection.execute(
                    """
                    SELECT provider, account, month_utc, ceiling_nusd, state, cost_nusd,
                           usage_json, request_id, outcome, created_at,
                           provider_error_type, rejection_class, settlement_evidence_json,
                           price_revision, requested_model
                    FROM attempt WHERE id = ?
                    """,
                    (str(attempt_id),),
                ).fetchone()
                if row is None:
                    raise LookupError(str(attempt_id))
                (
                    provider,
                    account,
                    month_utc,
                    ceiling,
                    state,
                    old_cost,
                    old_usage,
                    old_request,
                    old_outcome,
                    created_at,
                    old_type,
                    old_class,
                    old_evidence_json,
                    stored_revision,
                    stored_model,
                ) = row
                stored_envelope = _stored_evidence(
                    old_evidence_json,
                    revision=stored_revision,
                    model=stored_model,
                    outcome=old_outcome,
                    cost=old_cost,
                    provider_error_type=old_type,
                    rejection_class=old_class,
                    request_id=old_request,
                )
                if http_status is not None and state == "open":
                    connection.execute(
                        "UPDATE attempt SET http_status = COALESCE(http_status, ?) WHERE id = ?",
                        (http_status, str(attempt_id)),
                    )
                if isinstance(evidence, UnknownOutcome):
                    if state == "settled":
                        return "already_settled"
                    # The first reason recorded on an open attempt stands: a later write (a
                    # cancellation arriving after a rate limit was recorded) keeps it, and the
                    # rejection fields are written with the reason or not at all.
                    connection.execute(
                        "UPDATE attempt SET outcome = ?, provider_error_type = ?, "
                        "rejection_class = ? "
                        "WHERE id = ? AND state = 'open' AND outcome IS NULL",
                        (outcome, provider_error_type, rejection_class, str(attempt_id)),
                    )
                    if outcome not in ("cancelled", "stream_closed"):
                        _clear_warm_prefix(connection, provider, account)
                    result = "still_open"
                elif isinstance(evidence, StreamUsageEstimate):
                    if state == "settled":
                        candidate = dict(stored_envelope) if stored_envelope else None
                        if candidate is not None:
                            candidate["basis"] = outcome
                            candidate["origin"] = "runtime"
                            candidate["source_ref"] = None
                            candidate["first_failure"] = stored_envelope.get("first_failure")
                            candidate["evidence"] = _stream_evidence_dict(evidence)
                        if (
                            old_outcome == outcome
                            and old_cost == cost_nusd
                            and candidate is not None
                            and _canonical_json(candidate) == _canonical_json(stored_envelope)
                        ):
                            return "already_settled"
                        connection.execute(
                            "UPDATE account SET block_reason='settlement_conflict' WHERE provider=? AND name=?",
                            (provider, account),
                        )
                        _clear_warm_prefix(connection, provider, account)
                        conflict = (account, str(attempt_id))
                        result = "settled"
                    else:
                        prior_reasons = {
                            None,
                            "cancelled",
                            "stream_closed",
                            "transport_error",
                            "stream_truncated",
                            "stream_error",
                        }
                        if old_outcome not in prior_reasons or (
                            old_outcome is not None and old_outcome != evidence.original_reason
                        ):
                            connection.execute(
                                "UPDATE account SET block_reason='settlement_conflict' WHERE provider=? AND name=?",
                                (provider, account),
                            )
                            _clear_warm_prefix(connection, provider, account)
                            conflict = (account, str(attempt_id))
                            result = "still_open"
                        else:
                            envelope = stored_envelope
                            quote_obj = (
                                envelope.get("quote") if isinstance(envelope, dict) else None
                            )
                            if not isinstance(quote_obj, dict):
                                raise ValueError("stream estimate lacks a matching quote")
                            if (
                                quote_obj.get("price_revision") != stored_revision
                                or quote_obj.get("requested_model") != stored_model
                            ):
                                raise ValueError("stored quote identity is inconsistent")
                            revision = self.price_revisions.get(quote_obj.get("price_revision"))
                            if revision is None:
                                raise _routing_error(RouterErrorCode.UNSUPPORTED_PRICING)
                            retained_inputs = quote_obj.get("pricing_inputs")
                            supplied_inputs = _pricing_inputs_dict(evidence.pricing_inputs)
                            if (
                                retained_inputs != supplied_inputs
                                or stored_model != evidence.served_model
                            ):
                                raise ValueError(
                                    "stream estimate pricing inputs differ from reservation"
                                )
                            from lionagi.service.accounts._pricing import stream_usage_estimate

                            recomputed = stream_usage_estimate(
                                revision,
                                evidence.pricing_inputs,
                                evidence.served_model,
                                evidence.counts,
                                evidence.request_id,
                                evidence.original_reason,
                            )
                            if recomputed is None:
                                raise ValueError("observed stream usage is not priceable")
                            if recomputed != evidence:
                                raise ValueError("stream estimate differs from recomputed amount")
                            if (
                                evidence.quote_origin != "reservation"
                                or evidence.cost_nusd > INT64_MAX
                            ):
                                raise ValueError("invalid runtime stream estimate provenance")
                            usage_data = {
                                name: getattr(evidence.counts, name)
                                for name in (
                                    "input_tokens",
                                    "cache_read_input_tokens",
                                    "cache_write_5m_tokens",
                                    "cache_write_1h_tokens",
                                    "output_tokens",
                                )
                            }
                            rate_data = {
                                name: {
                                    "numerator": getattr(evidence.rates, name).numerator,
                                    "denominator": getattr(evidence.rates, name).denominator,
                                }
                                for name in (
                                    "input",
                                    "cache_read",
                                    "cache_write_5m",
                                    "cache_write_1h",
                                    "output",
                                )
                            }
                            stream_data = {
                                "original_reason": evidence.original_reason,
                                "served_model": evidence.served_model,
                                "counts": usage_data,
                                "request_id": evidence.request_id,
                                "pricing_inputs": supplied_inputs,
                                "quote_origin": evidence.quote_origin,
                                "rates": rate_data,
                                "observed_cost_nusd": evidence.observed_cost_nusd,
                                "margin_tokens": evidence.margin_tokens,
                                "margin_nusd": evidence.margin_nusd,
                                "cost_nusd": evidence.cost_nusd,
                            }
                            evidence_json = _canonical_json(
                                {
                                    "schema_version": 1,
                                    "rule_revision": "adr0126-am9-2026-11-02-v1",
                                    "quote": quote_obj,
                                    "basis": outcome,
                                    "origin": "runtime",
                                    "source_ref": None,
                                    "first_failure": {
                                        "reason": old_outcome,
                                        "provider_error_type": old_type,
                                        "rejection_class": old_class,
                                    },
                                    "evidence": stream_data,
                                }
                            )
                            settled, adjustments, opened, _ = self._spend_parts(
                                connection, provider, account, month_utc
                            )
                            rest = opened - ceiling
                            if rest < 0 or settled + cost_nusd + adjustments + rest > INT64_MAX:
                                raise _routing_error(
                                    RouterErrorCode.BUDGET_EXHAUSTED,
                                    account=account,
                                    attempt_id=attempt_id,
                                )
                            _, settled_at = _month_and_iso(self._clock)
                            connection.execute(
                                "UPDATE attempt SET state='settled', cost_nusd=?, usage_json=NULL, request_id=?, outcome=?, settled_at=?, settlement_evidence_json=? WHERE id=? AND state='open'",
                                (
                                    cost_nusd,
                                    evidence.request_id,
                                    outcome,
                                    settled_at,
                                    evidence_json,
                                    str(attempt_id),
                                ),
                            )
                            if cost_nusd > ceiling:
                                connection.execute(
                                    "UPDATE account SET block_reason='ceiling_exceeded' WHERE provider=? AND name=?",
                                    (provider, account),
                                )
                            if cost_nusd > ceiling or evidence.original_reason in (
                                "transport_error",
                                "stream_truncated",
                                "stream_error",
                            ):
                                _clear_warm_prefix(connection, provider, account)
                            result = "settled"
                elif isinstance(evidence, RejectionZero):
                    expected_reason = (
                        "request_rejected" if evidence.http_status == 400 else "provider_error"
                    )
                    compatible = (
                        state != "open" or old_outcome is None or old_outcome == expected_reason
                    )
                    if (
                        state == "open"
                        and evidence.http_status == 400
                        and old_type
                        not in (
                            None,
                            "invalid_request_error",
                        )
                    ):
                        compatible = False
                    if (
                        state == "open"
                        and evidence.http_status == 529
                        and (old_type is not None or old_class is not None)
                    ):
                        compatible = False
                    if not compatible:
                        connection.execute(
                            "UPDATE account SET block_reason = 'settlement_conflict' WHERE provider = ? AND name = ?",
                            (provider, account),
                        )
                        _clear_warm_prefix(connection, provider, account)
                        conflict = (account, str(attempt_id))
                        result = "still_open"
                    elif state == "settled":
                        stored_settlement = (
                            stored_envelope.get("evidence")
                            if isinstance(stored_envelope, dict)
                            else None
                        )
                        if isinstance(stored_envelope, dict) and isinstance(
                            stored_settlement, dict
                        ):
                            candidate = dict(stored_envelope)
                            candidate["evidence"] = dict(stored_settlement)
                            candidate["evidence"].update(
                                http_status=evidence.http_status,
                                provider_error_type=evidence.provider_error_type,
                                rejection_class=stored_settlement.get("rejection_class"),
                                original_reason=expected_reason,
                                cost_nusd=0,
                            )
                            identical = (
                                old_outcome == outcome
                                and old_cost == 0
                                and _canonical_json(candidate) == _canonical_json(stored_envelope)
                            )
                        else:
                            identical = False
                        result = "already_settled" if identical else "settled"
                        if result == "settled":
                            connection.execute(
                                "UPDATE account SET block_reason = 'settlement_conflict' WHERE provider = ? AND name = ?",
                                (provider, account),
                            )
                            _clear_warm_prefix(connection, provider, account)
                            conflict = (account, str(attempt_id))
                    else:
                        stored_class = (
                            old_class
                            if old_outcome == "request_rejected"
                            else evidence.rejection_class
                        )
                        if evidence.http_status == 529:
                            stored_class = None
                        evidence_json = _canonical_json(
                            {
                                "schema_version": 1,
                                "rule_revision": "adr0126-am9-2026-11-02-v1",
                                "quote": stored_envelope.get("quote") if stored_envelope else None,
                                "basis": "rejection_zero",
                                "origin": "runtime",
                                "source_ref": None,
                                "first_failure": {
                                    "reason": old_outcome,
                                    "provider_error_type": old_type,
                                    "rejection_class": old_class,
                                },
                                "evidence": {
                                    "http_status": evidence.http_status,
                                    "provider_error_type": evidence.provider_error_type,
                                    "rejection_class": stored_class,
                                    "original_reason": expected_reason,
                                    "cost_nusd": 0,
                                },
                            }
                        )
                        _, settled_at = _month_and_iso(self._clock)
                        connection.execute(
                            "UPDATE attempt SET state='settled', cost_nusd=0, usage_json=NULL, request_id=NULL, outcome=?, settled_at=?, settlement_evidence_json=?, provider_error_type=?, rejection_class=? WHERE id=? AND state='open'",
                            (
                                outcome,
                                settled_at,
                                evidence_json,
                                (
                                    evidence.provider_error_type
                                    if evidence.http_status == 400
                                    else old_type
                                ),
                                stored_class,
                                str(attempt_id),
                            ),
                        )
                        _clear_warm_prefix(connection, provider, account)
                        result = "settled"
                elif state == "settled":
                    identical = (
                        old_outcome == outcome
                        and old_cost == cost_nusd
                        and old_usage == usage_json
                        and old_request == request_id
                    )
                    if identical:
                        return "already_settled"
                    connection.execute(
                        "UPDATE account SET block_reason = 'settlement_conflict' "
                        "WHERE provider = ? AND name = ?",
                        (provider, account),
                    )
                    _clear_warm_prefix(connection, provider, account)
                    conflict = (account, str(attempt_id))
                    result = "settled"
                else:
                    settled, adjustments, opened, _ = self._spend_parts(
                        connection, provider, account, month_utc
                    )
                    remaining_open = opened - ceiling
                    updated_settled = settled + cost_nusd
                    updated_spend = updated_settled + adjustments
                    if (
                        remaining_open < 0
                        or updated_settled > INT64_MAX
                        or updated_spend + remaining_open > INT64_MAX
                    ):
                        raise _routing_error(
                            RouterErrorCode.BUDGET_EXHAUSTED,
                            account=account,
                            attempt_id=attempt_id,
                        )
                    _, settled_at = _month_and_iso(self._clock)
                    connection.execute(
                        """
                        UPDATE attempt
                        SET state = 'settled', cost_nusd = ?, usage_json = ?, request_id = ?,
                            outcome = ?, settled_at = ?
                        WHERE id = ? AND state = 'open'
                        """,
                        (cost_nusd, usage_json, request_id, outcome, settled_at, str(attempt_id)),
                    )
                    if isinstance(evidence, CompleteUsage) and cost_nusd > ceiling:
                        connection.execute(
                            "UPDATE account SET block_reason = 'ceiling_exceeded' "
                            "WHERE provider = ? AND name = ?",
                            (provider, account),
                        )
                        _clear_warm_prefix(connection, provider, account)
                    elif isinstance(evidence, CompleteUsage):
                        if cert is not None:
                            _record_warm_prefix(
                                connection,
                                provider,
                                account,
                                cert,
                                evidence,
                                created_at,
                                hour_after_key,
                                server_tools,
                            )
                    elif isinstance(evidence, ProvenNotSent) and not evidence.caller_cancelled:
                        _clear_warm_prefix(connection, provider, account)
                    result = "settled"
            if conflict is not None:
                raise _routing_error(
                    RouterErrorCode.SETTLEMENT_CONFLICT,
                    account=conflict[0],
                    attempt_id=attempt_id,
                ) from None
        return result

    async def settle(
        self,
        attempt_id: UUID,
        evidence: Evidence,
        *,
        cert: str | None = None,
        hour_after_key: bool = False,
        server_tools: bool = False,
        http_status: int | None = None,
    ) -> SettleResult:
        """Apply evidence to an attempt using its stored account and month.

        With ``cert``, complete usage within the ceiling records or refreshes the
        account's warm prefix; ``hour_after_key`` and ``server_tools`` mark a request
        whose cached prefix length cannot be inferred from its token counts.
        """
        if http_status is not None and (
            type(http_status) is not int or not 100 <= http_status <= 599
        ):
            raise ValueError("invalid HTTP status")
        return await self._run(
            lambda: self._settle_sync(
                attempt_id, evidence, cert, hour_after_key, server_tools, http_status
            )
        )

    def _block_sync(self, provider: str, account: str, reason: str) -> None:
        reason = _validate_reason(reason)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                cursor = connection.execute(
                    "UPDATE account SET block_reason = ? WHERE provider = ? AND name = ?",
                    (reason, provider, account),
                )
                if cursor.rowcount == 0:
                    raise LookupError(account)
                _clear_warm_prefix(connection, provider, account)

    async def block(self, provider: str, account: str, reason: str) -> None:
        """Mark an existing account unavailable for new reservations."""
        await self._run(lambda: self._block_sync(provider, account, reason))

    @staticmethod
    def _adjustment_row(connection: sqlite3.Connection, adjustment_id: UUID) -> tuple | None:
        return connection.execute(
            """
            SELECT kind, attempt_id, provider, account, month_utc, delta_nusd, source_ref
            FROM adjustment WHERE id = ?
            """,
            (str(adjustment_id),),
        ).fetchone()

    @staticmethod
    def _insert_adjustment(
        connection: sqlite3.Connection,
        adjustment_id: UUID,
        kind: str,
        attempt_id: UUID | None,
        provider: str,
        account: str,
        month_utc: str | None,
        delta_nusd: int,
        source_ref: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO adjustment(
                id, kind, attempt_id, provider, account, month_utc,
                delta_nusd, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(adjustment_id),
                kind,
                None if attempt_id is None else str(attempt_id),
                provider,
                account,
                month_utc,
                delta_nusd,
                source_ref,
                created_at,
            ),
        )

    def _reconcile_sync(
        self,
        adjustment_id: UUID,
        attempt_id: UUID,
        cost_nusd: int,
        source_ref: str,
    ) -> None:
        cost_nusd = _checked_int(cost_nusd, minimum=0, maximum=INT64_MAX, label="cost")
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                attempt = connection.execute(
                    """
                    SELECT provider, account, month_utc, state, cost_nusd, outcome
                    FROM attempt WHERE id = ?
                    """,
                    (str(attempt_id),),
                ).fetchone()
                if attempt is None:
                    raise LookupError(str(attempt_id))
                provider, account, month_utc, state, old_cost, old_outcome = attempt
                expected = (
                    "reconciliation",
                    str(attempt_id),
                    provider,
                    account,
                    month_utc,
                    0,
                    source_ref,
                )
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if (
                        existing == expected
                        and old_outcome == "reconciled"
                        and old_cost == cost_nusd
                    ):
                        return
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                if state != "open":
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, account, month_utc
                )
                attempt_ceiling = connection.execute(
                    "SELECT ceiling_nusd FROM attempt WHERE id = ?", (str(attempt_id),)
                ).fetchone()[0]
                remaining_open = opened - attempt_ceiling
                updated_settled = settled + cost_nusd
                updated_spend = updated_settled + adjustments
                if (
                    remaining_open < 0
                    or updated_settled > INT64_MAX
                    or updated_spend + remaining_open > INT64_MAX
                ):
                    raise _routing_error(
                        RouterErrorCode.BUDGET_EXHAUSTED,
                        account=account,
                        attempt_id=attempt_id,
                    )
                _, settled_at = _month_and_iso(self._clock)
                connection.execute(
                    """
                    UPDATE attempt SET state = 'settled', cost_nusd = ?, usage_json = NULL,
                        request_id = NULL, outcome = 'reconciled', settled_at = ?
                    WHERE id = ? AND state = 'open'
                    """,
                    (cost_nusd, settled_at, str(attempt_id)),
                )
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "reconciliation",
                    attempt_id,
                    provider,
                    account,
                    month_utc,
                    0,
                    source_ref,
                    settled_at,
                )

    async def reconcile(
        self,
        adjustment_id: UUID,
        attempt_id: UUID,
        cost_nusd: int,
        source_ref: str,
    ) -> None:
        """Settle an open attempt and record its source reference atomically."""
        await self._run(
            lambda: self._reconcile_sync(adjustment_id, attempt_id, cost_nusd, source_ref)
        )

    def _account_exists(self, connection: sqlite3.Connection, provider: str, account: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM account WHERE provider = ? AND name = ?", (provider, account)
            ).fetchone()
            is None
        ):
            raise LookupError(account)

    def _adjust_sync(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        month_utc: str,
        delta_nusd: int,
        source_ref: str,
        attempt_id: UUID | None,
    ) -> None:
        month_utc = _validate_month(month_utc)
        delta_nusd = _checked_int(
            delta_nusd, minimum=_INT64_MIN, maximum=INT64_MAX, label="adjustment"
        )
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                expected = (
                    "correction",
                    None if attempt_id is None else str(attempt_id),
                    provider,
                    account,
                    month_utc,
                    delta_nusd,
                    source_ref,
                )
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if existing == expected:
                        return
                    raise _routing_error(
                        RouterErrorCode.SETTLEMENT_CONFLICT,
                        account=account,
                        attempt_id=attempt_id,
                    )
                self._account_exists(connection, provider, account)
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, account, month_utc
                )
                updated_adjustments = adjustments + delta_nusd
                updated_spend = settled + updated_adjustments
                if updated_spend < 0:
                    raise ValueError("adjustment would make settled spend negative")
                if (
                    not _INT64_MIN <= updated_adjustments <= INT64_MAX
                    or updated_spend + opened > INT64_MAX
                ):
                    raise ValueError("adjustment exceeds signed 64-bit range")
                _, created_at = _month_and_iso(self._clock)
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "correction",
                    attempt_id,
                    provider,
                    account,
                    month_utc,
                    delta_nusd,
                    source_ref,
                    created_at,
                )

    async def adjust(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        month_utc: str,
        delta_nusd: int,
        source_ref: str,
        attempt_id: UUID | None = None,
    ) -> None:
        """Record a sourced correction for one account and UTC month."""
        await self._run(
            lambda: self._adjust_sync(
                adjustment_id,
                provider,
                account,
                month_utc,
                delta_nusd,
                source_ref,
                attempt_id,
            )
        )

    def _clear_block_sync(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        source_ref: str,
    ) -> None:
        source_ref = _validate_source(source_ref)
        with closing(self._connect()) as connection:
            with _transaction(connection):
                self._account_exists(connection, provider, account)
                expected = ("unblock", None, provider, account, None, 0, source_ref)
                existing = self._adjustment_row(connection, adjustment_id)
                if existing is not None:
                    if existing == expected:
                        return
                    raise _routing_error(RouterErrorCode.SETTLEMENT_CONFLICT, account=account)
                _, created_at = _month_and_iso(self._clock)
                self._insert_adjustment(
                    connection,
                    adjustment_id,
                    "unblock",
                    None,
                    provider,
                    account,
                    None,
                    0,
                    source_ref,
                    created_at,
                )
                blocked = connection.execute(
                    "SELECT block_reason FROM account WHERE provider = ? AND name = ?",
                    (provider, account),
                ).fetchone()
                connection.execute(
                    "UPDATE account SET block_reason = NULL WHERE provider = ? AND name = ?",
                    (provider, account),
                )
                if blocked is not None and blocked[0] is not None:
                    _clear_warm_prefix(connection, provider, account)

    async def clear_block(
        self,
        adjustment_id: UUID,
        provider: str,
        account: str,
        source_ref: str,
    ) -> None:
        """Clear an account mark and persist the source reference."""
        await self._run(
            lambda: self._clear_block_sync(adjustment_id, provider, account, source_ref)
        )

    def _status_sync(self, provider: str, month_utc: str | None) -> tuple[AccountStatus, ...]:
        with closing(self._connect()) as connection:
            if month_utc is None:
                month_utc, _ = _month_and_iso(self._clock)
            else:
                month_utc = _validate_month(month_utc)
            accounts = connection.execute(
                "SELECT name, block_reason FROM account WHERE provider = ? ORDER BY name",
                (provider,),
            ).fetchall()
            statuses: list[AccountStatus] = []
            for name, block_reason in accounts:
                budget_row = connection.execute(
                    """
                    SELECT budget_nusd FROM month
                    WHERE provider = ? AND name = ? AND month_utc = ?
                    """,
                    (provider, name, month_utc),
                ).fetchone()
                settled, adjustments, opened, _ = self._spend_parts(
                    connection, provider, name, month_utc
                )
                if (
                    not _INT64_MIN <= settled <= INT64_MAX
                    or not _INT64_MIN <= adjustments <= INT64_MAX
                    or not 0 <= opened <= INT64_MAX
                    or settled + adjustments > INT64_MAX
                    or settled + adjustments + opened > INT64_MAX
                ):
                    raise _routing_error(RouterErrorCode.LEDGER_UNAVAILABLE, account=name)
                statuses.append(
                    AccountStatus(
                        provider,
                        name,
                        month_utc,
                        None if budget_row is None else budget_row[0],
                        settled,
                        adjustments,
                        opened,
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM attempt
                            WHERE provider = ? AND account = ? AND month_utc = ?
                              AND state = 'open'
                            """,
                            (provider, name, month_utc),
                        ).fetchone()[0],
                        block_reason,
                    )
                )
            return tuple(statuses)

    async def status(
        self,
        provider: str,
        month_utc: str | None = None,
    ) -> tuple[AccountStatus, ...]:
        """Return accounting totals for every account of a provider."""
        return await self._run(lambda: self._status_sync(provider, month_utc))
