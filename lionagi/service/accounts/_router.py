# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Per-attempt accounting for routed Anthropic Messages requests."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

from pydantic import SecretStr

from lionagi.ln.concurrency import CancelScope
from lionagi.service._api_telemetry import record_selected_attempt
from lionagi.service._prompt_cache import note_attempt

from ._credentials import CredentialSources, resolve_account_key
from ._ledger import CredentialLedger, default_ledger_path
from ._pricing import (
    CURRENT_REVISION,
    PriceRevision,
    freeze_request,
    quote_ceiling,
    request_ttls,
    usage_outcome,
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
_CANONICAL_MESSAGES_URL = f"{_CANONICAL_BASE_URL}/messages"
_AUTH_HEADERS = frozenset({"authorization", "x-api-key"})
# A beta header turns on features no price revision covers.
_BETA_HEADERS = frozenset({"anthropic-beta"})
# The headers a routed request may carry, by lowercase name: those lionagi writes on a Messages
# call. Any other name is refused, since a header can change where or how the keyed request is
# delivered (`Host` names a site to a shared front end), and a list of names to refuse never closes.
_ROUTED_HEADERS = frozenset({"anthropic-version", "content-type", "idempotency-key"})
# Every other client option can change where or how the keyed request goes (a connector, a
# proxy taken from the environment, cookies, auth, trace hooks), and the session sets its own
# timeout, so headers are the only client option a routed call takes.
_ALLOWED_CLIENT_OPTIONS = frozenset({"headers"})
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
        headers: Any,
        *,
        endpoint: Any,
        request_kwargs: Mapping[str, Any],
        body_kwargs: Mapping[str, Any],
        client_kwargs: Mapping[str, Any] | None = None,
        url: str | None = None,
        method: str | None = None,
    ) -> RouterAttempt:
        validate_routed_request(
            endpoint,
            headers,
            request_kwargs=request_kwargs,
            body_kwargs=body_kwargs,
            payload=payload,
            client_kwargs=client_kwargs,
            url=url,
            method=method,
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
        with CancelScope(shield=True):
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
        attempt = RouterAttempt(reservation, body, ttls, key)
        label = attempt.account
        if (
            not isinstance(label, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", label) is None
            or "sk-ant" in label.lower()
            or mask_key_runs(label, key) != label
        ):
            label = None
        record_selected_attempt(
            attempt_id=str(attempt.attempt_id), provider="anthropic", account_label=label
        )
        # The prompt-cache entries this attempt writes or reads are evidenced on its account.
        note_attempt(label)
        return attempt

    async def settle_response(self, attempt: RouterAttempt, body: Any) -> None:
        evidence: CompleteUsage | UnknownOutcome = UnknownOutcome("usage_incomplete")
        if isinstance(body, Mapping):
            evidence = usage_outcome(
                self.revision,
                attempt.ttls,
                body.get("model"),
                body.get("usage"),
                provider_request_id(body.get("id"), attempt.key),
            )
        await self.ledger.settle(attempt.attempt_id, evidence)

    async def settle_unknown(self, attempt: RouterAttempt, reason: str) -> None:
        await self.ledger.settle(attempt.attempt_id, UnknownOutcome(reason))

    async def settle_not_sent(self, attempt: RouterAttempt) -> None:
        await self.settle_reservation_not_sent(attempt.reservation)

    async def settle_reservation_not_sent(self, reservation: Reservation) -> None:
        with CancelScope(shield=True):
            await self.ledger.settle(reservation.attempt_id, ProvenNotSent())

    async def block(self, attempt: RouterAttempt, reason: str) -> None:
        await self.ledger.block("anthropic", attempt.account, reason)

    async def settle_stream_end(
        self,
        attempt: RouterAttempt,
        usage: CompleteUsage | UnknownOutcome | None,
        *,
        reason: str = "usage_incomplete",
    ) -> None:
        evidence = usage or UnknownOutcome(reason)
        # The write runs in a worker thread that is not abandoned on cancellation, so the
        # shield keeps an anyio cancel scope from returning before it commits.
        with CancelScope(shield=True):
            await self.ledger.settle(attempt.attempt_id, evidence)


def _refuse(code: RouterErrorCode) -> AccountRoutingError:
    return AccountRoutingError(code)


# The HTTP client refuses a header value holding one of these when it writes the request.
_FORBIDDEN_HEADER_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# A header name is an RFC 9110 token. The HTTP client writes any other name as given, so a name
# with a space or a colon in it reaches the wire as a line a server may read differently.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def _header_key(name: str) -> str:
    # An underscore is read as a hyphen, since a server may read the two alike.
    return name.lower().replace("_", "-")


def header_pairs(headers: Any) -> list[tuple[str, str]]:
    """Headers as name-value pairs, read once, or a refusal.

    A mapping is read by its items and a list or tuple pair by pair. Anything else is refused as
    a credential conflict, and so is a name that is not an RFC 9110 token, a value that is not a
    string or that holds a control character other than a tab, and a name given twice (compared
    case-insensitively, with an underscore read as a hyphen): the HTTP client would write the
    first three as given or fail on them only when it writes the request, after an account was
    reserved, and a repeated name is sent as more than one header line.
    """
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        items = list(headers.items())
    elif isinstance(headers, (list, tuple)):
        items = list(headers)
    else:
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
        name, value = item
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not _HEADER_NAME.fullmatch(name)
            or _FORBIDDEN_HEADER_CHARS.search(value)
        ):
            raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
        pairs.append((name, value))
    if len({_header_key(name) for name, _ in pairs}) != len(pairs):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    return pairs


def routed_client_kwargs(client_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """The client options copied once, with their headers read as ``header_pairs`` reads them."""
    snapshot = dict(client_kwargs)
    if snapshot.get("headers") is not None:
        snapshot["headers"] = header_pairs(snapshot["headers"])
    return snapshot


def _has_header(headers: Any, names: frozenset[str]) -> bool:
    return any(_header_key(name) in names for name, _ in header_pairs(headers))


def validate_routed_request(
    endpoint: Any,
    headers: Any,
    *,
    request_kwargs: Mapping[str, Any],
    body_kwargs: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    client_kwargs: Mapping[str, Any] | None = None,
    url: str | None = None,
    method: str | None = None,
) -> None:
    """Refuse a routed request that could send the key anywhere but the Messages endpoint.

    ``url`` and ``method`` are the ones the request will use; a caller that reads them once
    before reserving an account passes them, so the values checked are the values sent.
    """
    config = endpoint.config
    if url is None:
        url = config.full_url
    if method is None:
        method = config.method
    if (
        getattr(config, "_api_key_explicit", False)
        or config.auth_type != "x-api-key"
        or url != _CANONICAL_MESSAGES_URL
        or not isinstance(method, str)
        or method.upper() != "POST"
        or _has_header(config.default_headers, _AUTH_HEADERS)
        or _has_header(headers, _AUTH_HEADERS)
        or (payload is not None and "api_key" in payload)
    ):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)

    if client_kwargs is None:
        client_kwargs = config.client_kwargs
    if any(name not in _ALLOWED_CLIENT_OPTIONS for name in client_kwargs) or _has_header(
        client_kwargs.get("headers"), _AUTH_HEADERS
    ):
        raise _refuse(RouterErrorCode.CREDENTIAL_CONFLICT)
    if any(
        _has_header(source, _BETA_HEADERS)
        for source in (config.default_headers, headers, client_kwargs.get("headers"))
    ):
        raise _refuse(RouterErrorCode.UNSUPPORTED_PRICING)
    if any(
        name.lower() not in _ROUTED_HEADERS
        for source in (config.default_headers, headers, client_kwargs.get("headers"))
        for name, _ in header_pairs(source)
    ):
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


def _key_runs(text: str, key: SecretStr) -> list[bool]:
    """Which characters of ``text`` lie in a run of ``StreamTextRedactor.MIN_RUN`` or more
    characters (the whole key, for a shorter key) that occurs anywhere in the key."""
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
    return masked


def mask_joined_key_runs(pieces: list[tuple[Any, str]], key: SecretStr) -> list[str]:
    """Each ``(kind, text)`` piece's text with every character masked that lies in a run
    ``mask_key_runs`` masks in a join it belongs to: the pieces of its kind in order, or
    every piece in order. A key split across pieces is masked in each piece that holds a
    part of it, and each piece keeps its own boundaries."""
    masked = [[False] * len(text) for _, text in pieces]
    kinds: dict[Any, list[int]] = {}
    for index, (kind, _) in enumerate(pieces):
        kinds.setdefault(kind, []).append(index)
    for members in (*kinds.values(), range(len(pieces))):
        runs = _key_runs("".join(pieces[index][1] for index in members), key)
        at = 0
        for index in members:
            size = len(pieces[index][1])
            masked[index] = [
                a or b for a, b in zip(masked[index], runs[at : at + size], strict=True)
            ]
            at += size
    return [_masked_text(text, masked[index]) for index, (_, text) in enumerate(pieces)]


def mask_key_runs(text: str, key: SecretStr) -> str:
    """``text`` with every run of ``StreamTextRedactor.MIN_RUN`` or more characters (the
    whole key, for a shorter key) that occurs anywhere in the key masked, for text that
    no other text continues."""
    return _masked_text(text, _key_runs(text, key))


def provider_request_id(value: Any, key: SecretStr) -> str | None:
    """A provider's message id as an attempt's settlement may record it: the id when it is
    a string holding no run of ``StreamTextRedactor.MIN_RUN`` or more characters of the
    key (the whole key, for a shorter key) matched exactly, else None. A shorter run, and
    any run that differs from the key in case, is not masked, so a recorded id can hold up
    to ``MIN_RUN - 1`` consecutive characters of the key, or a longer run in another case."""
    if not isinstance(value, str) or mask_key_runs(value, key) != value:
        return None
    return value


def _masked_text(text: str, masked: list[bool]) -> str:
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
    part of ``MIN_RUN`` or more characters of one are masked in each of those joins
    however the message's events split them. A consumer that joins the text of some
    chunk types and not others (text and thinking, without an error chunk between them)
    makes a join that is not matched, and such a run split across its parts can be sent
    there. Pieces of kind ``RAW`` (the data of an event that carries no
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
