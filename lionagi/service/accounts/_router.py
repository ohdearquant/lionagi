# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Per-attempt accounting for routed Anthropic Messages requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

import anyio
from pydantic import SecretStr

from ._credentials import CredentialSources, resolve_account_key
from ._ledger import CredentialLedger, default_ledger_path
from ._pricing import (
    CURRENT_REVISION,
    PriceRevision,
    freeze_request,
    price_usage,
    quote_ceiling,
    request_ttls,
)
from ._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Reservation,
    RouterErrorCode,
    Scope,
    UnknownOutcome,
)

__all__ = ("AccountRouter",)

_CANONICAL_BASE_URL = "https://api.anthropic.com/v1"
_AUTH_HEADERS = frozenset({"authorization", "x-api-key"})
_FORBIDDEN_CLIENT_OPTIONS = frozenset(
    {"auth", "trace_configs", "allow_redirects", "follow_redirects", "raise_for_status"}
)
_UNSET = object()
_router: AccountRouter | None = None
_router_lock = threading.Lock()
_test_ledger_path: Any = _UNSET
_test_sources: Any = _UNSET


@dataclass(slots=True)
class RouterAttempt:
    reservation: Reservation
    body: bytes
    ttls: frozenset[str]
    key: SecretStr = field(repr=False)

    @property
    def account(self) -> str:
        return self.reservation.account

    @property
    def attempt_id(self) -> UUID:
        return self.reservation.attempt_id


class AccountRouter:
    def __init__(
        self,
        *,
        sources: CredentialSources,
        ledger: CredentialLedger,
        accounts: tuple[AccountSpec, ...],
        revision: PriceRevision,
    ) -> None:
        self.sources = sources
        self.ledger = ledger
        self.accounts = accounts
        self.revision = revision

    async def begin(
        self,
        scope: Scope,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        *,
        endpoint: Any,
        request_kwargs: Mapping[str, Any],
        body_kwargs: Mapping[str, Any],
    ) -> RouterAttempt:
        validate_routed_request(
            endpoint,
            headers,
            request_kwargs=request_kwargs,
            body_kwargs=body_kwargs,
            payload=payload,
        )
        if scope.provider != "anthropic":
            raise _refuse(RouterErrorCode.ACCOUNT_CONFIG_CONFLICT)

        final_payload = dict(payload)
        # Only a request that names no tier is sent as standard_only. A tier the caller
        # names reaches the price check, which refuses every tier but standard_only.
        if final_payload.get("service_tier") is None:
            final_payload["service_tier"] = "standard_only"
        ttls = request_ttls(final_payload)
        body = freeze_request(final_payload)
        quote = quote_ceiling(self.revision, body)
        attempt_id = uuid4()
        # Shielded so a cancelled caller cannot leave a committed reservation unknown to it.
        with anyio.CancelScope(shield=True):
            reservation = await self.ledger.reserve(scope, attempt_id, quote, self.accounts)
        account = next(spec for spec in self.accounts if spec.name == reservation.account)
        try:
            key = await resolve_account_key(account.key_env, self.sources)
        except AccountRoutingError as exc:
            await self.settle_reservation_not_sent(reservation)
            raise exc from None
        except BaseException:
            await self.settle_reservation_not_sent(reservation)
            raise
        return RouterAttempt(reservation, body, ttls, key)

    async def settle_response(self, attempt: RouterAttempt, body: Any) -> None:
        usage = None
        if isinstance(body, Mapping):
            usage = price_usage(
                self.revision,
                attempt.ttls,
                body.get("model"),
                body.get("usage"),
                body.get("id") if isinstance(body.get("id"), str) else None,
            )
        evidence = usage or UnknownOutcome("usage_incomplete")
        await self.ledger.settle(attempt.attempt_id, evidence)

    async def settle_unknown(self, attempt: RouterAttempt, reason: str) -> None:
        await self.ledger.settle(attempt.attempt_id, UnknownOutcome(reason))

    async def settle_not_sent(self, attempt: RouterAttempt) -> None:
        await self.settle_reservation_not_sent(attempt.reservation)

    async def settle_reservation_not_sent(self, reservation: Reservation) -> None:
        with anyio.CancelScope(shield=True):
            await self.ledger.settle(reservation.attempt_id, ProvenNotSent())

    async def block(self, attempt: RouterAttempt, reason: str) -> None:
        await self.ledger.block("anthropic", attempt.account, reason)

    async def settle_stream_end(
        self,
        attempt: RouterAttempt,
        usage: CompleteUsage | None,
        *,
        reason: str = "usage_incomplete",
    ) -> bool:
        evidence = usage or UnknownOutcome(reason)
        timeout = max(0.1, self.ledger.busy_timeout_s)
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(timeout, shield=True) as timeout_scope:
                await self.ledger.settle(attempt.attempt_id, evidence)
        return not timeout_scope.cancel_called


def _refuse(code: RouterErrorCode) -> AccountRoutingError:
    return AccountRoutingError(code)


def _has_auth_header(headers: Mapping[str, Any] | None) -> bool:
    if not isinstance(headers, Mapping):
        return False
    return any(isinstance(name, str) and name.strip().lower() in _AUTH_HEADERS for name in headers)


def validate_routed_request(
    endpoint: Any,
    headers: Mapping[str, Any] | None,
    *,
    request_kwargs: Mapping[str, Any],
    body_kwargs: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    config = endpoint.config
    if (
        getattr(config, "_api_key_explicit", False)
        or config.auth_type != "x-api-key"
        or config.base_url != _CANONICAL_BASE_URL
        or _has_auth_header(config.default_headers)
        or _has_auth_header(headers)
        or (payload is not None and "api_key" in payload)
    ):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)

    client_kwargs = config.client_kwargs
    if any(
        isinstance(name, str) and name.lower() in _FORBIDDEN_CLIENT_OPTIONS
        for name in client_kwargs
    ) or _has_auth_header(client_kwargs.get("headers")):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    # A routed call takes no request keyword. Each one reaches the HTTP client, where a name it
    # does not know fails only after an account was reserved, and one it does know (ssl, proxy)
    # can move the keyed request off the verified connection. A tier keeps its own code: routed
    # calls are priced for the standard tier alone, and the caller should see a pricing refusal.
    if request_kwargs:
        if any(isinstance(name, str) and name.lower() == "service_tier" for name in request_kwargs):
            raise _refuse(RouterErrorCode.UNSUPPORTED_PRICING)
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    if body_kwargs is not None:
        if set(body_kwargs) != {"json"}:
            raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)


def get_router() -> AccountRouter:
    global _router
    if _router is not None:
        return _router
    with _router_lock:
        if _router is not None:
            return _router
        from lionagi.config import settings

        accounts = tuple(
            AccountSpec(name=item.name, key_env=item.key_env, budget_nusd=item.budget_nusd)
            for item in settings.ANTHROPIC_ACCOUNTS
        )
        sources = CredentialSources.capture(settings) if _test_sources is _UNSET else _test_sources
        path = default_ledger_path() if _test_ledger_path is _UNSET else _test_ledger_path
        _router = AccountRouter(
            sources=sources,
            ledger=CredentialLedger(path),
            accounts=accounts,
            revision=CURRENT_REVISION,
        )
        return _router


def _set_router_test_dependencies(*, ledger_path: Any = _UNSET, sources: Any = _UNSET) -> None:
    global _router, _test_ledger_path, _test_sources
    with _router_lock:
        _router = None
        _test_ledger_path = ledger_path
        _test_sources = sources


def _reset_router_test_dependencies() -> None:
    global _router, _test_ledger_path, _test_sources
    with _router_lock:
        _router = None
        _test_ledger_path = _UNSET
        _test_sources = _UNSET


def redact_value(value: Any, key: SecretStr) -> Any:
    secret = key.get_secret_value()
    mask = "[REDACTED]"
    if isinstance(value, str):
        return value.replace(secret, mask)
    if isinstance(value, bytes):
        return value.replace(secret.encode("ascii"), mask.encode("ascii"))
    if isinstance(value, list):
        return [redact_value(item, key) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, key) for item in value)
    if isinstance(value, dict):
        return {
            redact_value(name, key) if isinstance(name, str) else name: redact_value(item, key)
            for name, item in value.items()
        }
    return value


class StreamingTextRedactor:
    """Masks a key in one run of text a consumer joins, wherever the run is split.

    Each piece is held back by the key's length less one, so a key that continues
    in the next piece is replaced before either half is emitted. At a boundary
    the text must reach the consumer before (another kind of delta, the end of a
    block, an error event), ``release`` emits what is held except a tail of at
    least ``BOUNDARY_HOLD`` characters that begins the key; that tail waits for
    the next piece, where a continued key is masked whole. A shorter beginning
    of the key that was emitted is remembered, and the rest of the key is masked
    when it follows, so the joined run never holds the whole key and at most
    ``BOUNDARY_HOLD - 1`` of its characters.
    """

    BOUNDARY_HOLD = 8

    def __init__(self, key: SecretStr) -> None:
        self._secret = key.get_secret_value()
        self._tail = ""
        self._emitted = ""

    def _emit(self, text: str) -> str:
        keep = len(self._secret) - 1
        self._emitted = (self._emitted + text)[-keep:] if keep > 0 else ""
        return text

    def _mask_continuation(self, data: str) -> str:
        """Mask the rest of a key whose beginning was already emitted."""
        joined = self._emitted + data
        start = joined.find(self._secret)
        while 0 <= start < len(self._emitted):
            end = start + len(self._secret)
            if end > len(self._emitted):
                return "[REDACTED]" + data[end - len(self._emitted) :]
            start = joined.find(self._secret, start + 1)
        return data

    def feed(self, text: str, *, final: bool = False) -> str:
        data = self._mask_continuation(self._tail + text).replace(self._secret, "[REDACTED]")
        if final:
            self._tail = ""
            return self._emit(data)
        safe_length = max(0, len(data) - len(self._secret) + 1)
        output, self._tail = data[:safe_length], data[safe_length:]
        return self._emit(output)

    def release(self) -> str:
        """Emit the held text at a boundary, keeping back a tail that begins the key."""
        keep = 0
        for n in range(min(len(self._tail), len(self._secret) - 1), self.BOUNDARY_HOLD - 1, -1):
            if self._secret.startswith(self._tail[-n:]):
                keep = n
                break
        output = self._tail[: len(self._tail) - keep]
        self._tail = self._tail[len(self._tail) - keep :]
        return self._emit(output)

    def mask_whole(self, text: str) -> str:
        """Mask a piece that arrives whole and is never held: a key within it, and the
        rest of a key whose beginning ended the piece before."""
        return self._emit(self._mask_continuation(text).replace(self._secret, "[REDACTED]"))


class StreamingSecretRedactor:
    def __init__(self, key: SecretStr) -> None:
        self._secret = key.get_secret_value().encode("ascii")
        self._tail = b""

    def feed(self, value: bytes, *, final: bool = False) -> bytes:
        # Replace whole keys before cutting, so a key that straddles the cut is not
        # split between this output and the held tail, where neither half matches.
        data = (self._tail + value).replace(self._secret, b"[REDACTED]")
        if final:
            self._tail = b""
            return data
        safe_length = max(0, len(data) - len(self._secret) + 1)
        output, self._tail = data[:safe_length], data[safe_length:]
        return output
