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
# A beta header turns on features no price revision covers.
_BETA_HEADERS = frozenset({"anthropic-beta"})
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


def _has_header(headers: Mapping[str, Any] | None, names: frozenset[str]) -> bool:
    if not isinstance(headers, Mapping):
        return False
    return any(isinstance(name, str) and name.strip().lower() in names for name in headers)


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
        or _has_header(config.default_headers, _AUTH_HEADERS)
        or _has_header(headers, _AUTH_HEADERS)
        or (payload is not None and "api_key" in payload)
    ):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)

    client_kwargs = config.client_kwargs
    if any(
        isinstance(name, str) and name.lower() in _FORBIDDEN_CLIENT_OPTIONS
        for name in client_kwargs
    ) or _has_header(client_kwargs.get("headers"), _AUTH_HEADERS):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    if any(
        _has_header(source, _BETA_HEADERS)
        for source in (config.default_headers, headers, client_kwargs.get("headers"))
    ):
        raise _refuse(RouterErrorCode.UNSUPPORTED_PRICING)
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


class _Char:
    __slots__ = ("ch", "masked", "open")

    def __init__(self, ch: str, joins: int) -> None:
        self.ch = ch
        self.masked = False
        self.open = joins  # joins that have not decided this character yet


class _Join:
    __slots__ = ("active", "pending", "tail")

    def __init__(self) -> None:
        self.active: set[int] = set()  # lengths of the key's beginning the join now ends with
        self.tail = ""  # the longest end of the join that occurs anywhere in the key
        self.pending: list[_Char] = []  # undecided characters, the join's last ones


def mask_key_runs(text: str, key: SecretStr) -> str:
    """``text`` with every run of ``StreamTextRedactor.MIN_RUN`` or more characters (the
    whole key, for a shorter key) that occurs anywhere in the key masked, for text that
    no other text continues."""
    secret = key.get_secret_value()
    need = min(StreamTextRedactor.MIN_RUN, len(secret))
    masked = [False] * len(text)
    tail = ""
    for index, ch in enumerate(text):
        tail += ch
        while tail and tail not in secret:
            tail = tail[1:]
        if len(tail) >= need:
            masked[index - len(tail) + 1 : index + 1] = [True] * len(tail)
    out: list[str] = []
    for index, ch in enumerate(text):
        if not masked[index]:
            out.append(ch)
        elif index == 0 or not masked[index - 1]:
            out.append(StreamTextRedactor.MASK)
    return "".join(out)


class StreamTextRedactor:
    """Masks a key in the text a consumer joins out of one streamed message.

    A consumer joins the pieces of one kind across the message (text across blocks
    and citations, thinking across signatures, the quoted text of citations), joins
    the text of the stream chunks it receives by chunk type and all of it in order,
    and may join every piece in order. Each of those joins is matched against the key,
    and a character is masked when it lies in a run of ``MIN_RUN`` or more characters
    that occurs anywhere in the key, in any join it belongs to. A whole key and any
    part of ``MIN_RUN`` or more characters of one are masked however the message's
    events split them. Pieces of kind ``RAW`` (the data of an event that carries no
    known text field) are joined only with each other; the text a chunk shows for such
    an event is a piece of its own (see ``push``). Joins are keyed by tuples, so no
    type name an event carries can name another join.

    A character in a beginning of the key longer than ``PUBLIC_PREFIX``, the
    ``sk-ant-`` every Anthropic key starts with, is masked as soon as the beginning
    is that long, so what is sent of a beginning is public. A key that does not
    start with ``PUBLIC_PREFIX`` therefore has every character of any beginning of
    it masked, its first character wherever it appears.

    An event whose text ends in a shorter run of the key is held until the next
    event shows whether the run continues. A held character the next character
    shows is in no run is decided at once; when the next event carries no text into
    the same join, or the stream ends, every held character is decided. A held
    character of a run shorter than ``MIN_RUN`` that is not masked as part of a
    beginning is sent: fewer than ``MIN_RUN`` characters cannot be told from other
    text. So from inside the key, up to ``MIN_RUN - 1`` characters can be sent
    before a pause the run continues after; from there the run is masked.

    Masking is decided per run, which sets two limits. A key whose runs of fewer
    than ``MIN_RUN`` characters have other characters between them, such as the key
    written in seven-character pieces separated by spaces, is sent in those pieces,
    within one event as across events: no run is long enough to tell it from other
    text. And text that is not the key is masked where it holds a run of ``MIN_RUN``
    characters that occurs in the key, or a beginning of the key past the public
    prefix, as ``sk-ant-api03-`` is for a key that begins that way.
    """

    MIN_RUN = 8
    MASK = "[REDACTED]"
    ALL = ("all",)  # every piece in order
    CHUNKS = ("chunks",)  # the text of every chunk a consumer receives, in order
    RAW = ("data",)  # the kind of an event's data when it carries no known text field
    PUBLIC_PREFIX = "sk-ant-"

    def __init__(self, key: SecretStr) -> None:
        self._secret = key.get_secret_value()
        # The longest beginning of the key that held text may end in and still be sent.
        self._release = (
            len(self.PUBLIC_PREFIX) if self._secret.startswith(self.PUBLIC_PREFIX) else 0
        )
        if self._release >= self.MIN_RUN:
            raise ValueError("PUBLIC_PREFIX must be shorter than MIN_RUN")
        self._joins: dict[tuple, _Join] = {self.ALL: _Join()}
        self._holding: set[tuple] = set()  # joins with undecided characters
        self._queue: list[tuple[Any, list[tuple[tuple, list[_Char]]]]] = []
        self._last_masked: dict[tuple, bool] = {}

    def push(
        self,
        item: Any,
        pieces: list[tuple[str | None | tuple[str], str]],
        chunk: tuple[str, int | str] | None = None,
    ) -> list[tuple[Any, list[str]]]:
        """Queue one event's text pieces as ``(kind, text)`` and return the events now
        ready, in order, each with its pieces' text as sent. A kind is a delta type, or
        ``RAW``.

        ``chunk`` is ``(chunk_type, source)`` for an event a consumer receives as a chunk
        with text, which it joins with the text of the other chunks of its type and with
        the text of every chunk: ``source`` is the index of the piece whose text the
        chunk shows, or the text itself when no piece holds it. The chunk's text as sent
        follows the pieces'. Text no piece holds is also joined with every piece in
        order."""
        fed: set[tuple] = set()
        entry: list[tuple[tuple, list[_Char]]] = []
        linked = chunk[1] if chunk is not None and isinstance(chunk[1], int) else None
        chunk_joins = (("chunk", chunk[0]), self.CHUNKS) if chunk is not None else ()
        for index, (kind, text) in enumerate(pieces):
            own = self.RAW if kind == self.RAW else ("delta", kind)
            joins = (own,) if kind == self.RAW else (self.ALL, own)
            if index == linked:
                joins += chunk_joins
            entry.append((own, self._feed_text(text, joins, fed)))
        if chunk is not None:
            source = chunk[1]
            if isinstance(source, int):
                entry.append((chunk_joins[0], entry[source][1]))
            else:
                entry.append(
                    (chunk_joins[0], self._feed_text(source, (self.ALL, *chunk_joins), fed))
                )
        for name in fed:
            if self._joins[name].pending:
                self._holding.add(name)
            else:
                self._holding.discard(name)
        for name in self._holding - fed:
            self._decide(self._joins[name])
        self._holding &= fed
        self._queue.append((item, entry))
        return self._ready()

    def finish(self) -> list[tuple[Any, list[str]]]:
        """Send everything held: the stream has ended, so no key continues."""
        for name in self._holding:
            self._decide(self._joins[name])
        self._holding.clear()
        return self._ready()

    def _feed_text(self, text: str, joins: tuple[tuple, ...], fed: set[tuple]) -> list[_Char]:
        chars = [_Char(ch, len(joins)) for ch in text]
        for name in joins:
            join = self._joins.setdefault(name, _Join())
            for char in chars:
                self._feed(join, char)
        if chars:
            fed.update(joins)
        return chars

    def _feed(self, join: _Join, char: _Char) -> None:
        secret = self._secret
        join.active = {
            length + 1
            for length in (*join.active, 0)
            if length < len(secret) and secret[length] == char.ch
        }
        beginning = max(join.active, default=0)
        join.active.discard(len(secret))
        tail = join.tail + char.ch
        while tail and tail not in secret:
            tail = tail[1:]
        join.tail = tail
        join.pending.append(char)
        # The join's last len(tail) characters are a run of the key; an older held
        # character is in no run, so this join has decided it.
        while len(join.pending) > len(tail):
            join.pending.pop(0).open -= 1
        if len(tail) >= min(self.MIN_RUN, len(secret)):
            # Every undecided character of the run is masked; its earlier characters
            # were masked when the run reached MIN_RUN or sent before it did.
            for held in join.pending:
                held.masked = True
                held.open -= 1
            join.pending.clear()
        elif beginning > self._release:
            # A beginning of the key longer than the public prefix is never sent.
            for held in join.pending[-beginning:]:
                held.masked = True

    def _decide(self, join: _Join) -> None:
        # No more text reaches this join before the next event. Its undecided
        # characters end a run of the key shorter than MIN_RUN; one in a beginning
        # longer than the public prefix is already masked, and the rest are sent. The
        # join still remembers the run and the beginning they make.
        for held in join.pending:
            held.open -= 1
        join.pending.clear()

    def _ready(self) -> list[tuple[Any, list[str]]]:
        ready = []
        while self._queue and all(
            char.masked or char.open == 0 for _, chars in self._queue[0][1] for char in chars
        ):
            item, entry = self._queue.pop(0)
            texts = []
            for kind, chars in entry:
                out = []
                previous = self._last_masked.get(kind, False)
                for char in chars:
                    if char.masked:
                        if not previous:
                            out.append(self.MASK)
                        previous = True
                    else:
                        out.append(char.ch)
                        previous = False
                self._last_masked[kind] = previous
                texts.append("".join(out))
            ready.append((item, texts))
        return ready


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
