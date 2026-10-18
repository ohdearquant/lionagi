# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Access for a page on another origin, by launch, when no auth token is configured.

A daemon with no ``LIONAGI_STUDIO_AUTH_TOKEN`` refuses API requests from a browser
origin that is neither loopback nor its own. ``li studio`` opens the hosted page
anyway, so the page needs two credentials: a bearer for API admission and a
decision capability for the two Operator decision routes. This module holds both,
minted per process and kept in memory only. They are never put in the environment,
so the processes the daemon spawns do not inherit them, and they are never written
anywhere.

The page does not receive the bearer in its URL. ``li studio`` opens it with a
one-time pairing code instead, and the page exchanges the code, over the pairing
route, for the bearer and a decision capability. A code is single use, bound to
the origins the launcher named, and expires after ``PAIRING_CODE_TTL_S`` seconds,
so a URL that reaches a process list or a browser history is spent by the time
anyone reads it.

A decision capability is a random 256-bit secret kept only as its hash, bound to
a random principal id, this process's start epoch, the frontend origin it was
redeemed from, and the scope ``operator:decide``. It expires
``DECISION_CAPABILITY_TTL_S`` seconds after redemption and can be revoked. Every
code and capability belongs to one process: a daemon restart mints a new epoch
and invalidates all of them.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = (
    "DECISION_CAPABILITY_TTL_S",
    "DECISION_SCOPE",
    "DECISION_TOKEN_HEADER",
    "DecisionPrincipal",
    "PAIRING_CODE_TTL_S",
    "PAIRING_REDEEM_PATH",
    "begin_serving",
    "daemon_epoch",
    "launch_bearer",
    "mint_decision_capability",
    "mint_pairing_code",
    "redeem_pairing_code",
    "redeem_pairing_grant",
    "reset_launch_access",
    "resolve_decision_capability",
    "revoke_decision_capability",
)

PAIRING_CODE_TTL_S = 120.0
DECISION_CAPABILITY_TTL_S = 8 * 60 * 60.0
# The one API path the bearer gate lets through without a bearer: the code it
# carries is the credential.
PAIRING_REDEEM_PATH = "/api/studio/pair"

DECISION_SCOPE = "operator:decide"
# The header a paired page sends its decision capability in, on the two
# Operator decision requests and on an explicit disconnect.
DECISION_TOKEN_HEADER = "x-studio-decision-token"  # noqa: S105 - a header name, not a credential


def _new_epoch() -> str:
    return secrets.token_hex(16)


_lock = threading.Lock()
_bearer: str | None = None
# code -> (expires_at, origins): a tuple, so the redeem path can never alias one
# code's binding to another's.
_codes: dict[str, tuple[float, tuple[str, ...]]] = {}
_epoch = _new_epoch()
# The codes a launcher minted in this process for the daemon it starts next.
# They belong to the daemon starting now, so serving begins by carrying them
# into the new epoch instead of clearing them.
_launch_codes: set[str] = set()


@dataclass(frozen=True, slots=True)
class DecisionPrincipal:
    """Who a decision capability was issued to: a redemption, not a person."""

    id: str
    epoch: str
    method: str
    origin: str
    scope: str = DECISION_SCOPE


@dataclass(frozen=True, slots=True)
class _Capability:
    principal: DecisionPrincipal
    digest: bytes
    expires_at: float


# digest -> capability. The secret itself is never kept.
_capabilities: dict[bytes, _Capability] = {}


@dataclass(frozen=True, slots=True)
class PairingGrant:
    """What a spent code yields: the admission bearer and the decision capability."""

    bearer: str
    capability: str


def daemon_epoch() -> str:
    """This process's start epoch; every code and capability is bound to it."""
    with _lock:
        return _epoch


def reset_launch_access() -> None:
    """Start a new epoch and drop every code and capability."""
    global _bearer, _epoch
    with _lock:
        _bearer = None
        _codes.clear()
        _capabilities.clear()
        _launch_codes.clear()
        _epoch = _new_epoch()


def begin_serving() -> None:
    """Start the daemon's serving epoch, invalidating what a previous one issued.

    A code a launcher minted just before starting this daemon, in this same
    process (``li studio`` mints the code it opens the page with and then runs
    the server), is carried into the new epoch, since it belongs to the daemon
    starting now. Every other code and every capability is dropped. A restart
    in a fresh process starts with fresh module state; this matters for a
    second daemon in one interpreter, whose earlier codes and capabilities must
    not outlive it.
    """
    global _bearer, _epoch
    with _lock:
        carried = {code: _codes[code] for code in _launch_codes if code in _codes}
        _launch_codes.clear()
        _bearer = None
        _codes.clear()
        _capabilities.clear()
        _epoch = _new_epoch()
        if carried:
            _bearer = secrets.token_urlsafe(32)
            _codes.update(carried)


def launch_bearer() -> str | None:
    """The bearer a page on another origin presents in this process, once one is minted."""
    with _lock:
        return _bearer


def mint_pairing_code(*, origins: Iterable[str] | None = None, for_launch: bool = False) -> str:
    """A new single-use code for the launch bearer, minting the bearer on first use.

    The code redeems only from an origin in *origins*; ``None`` binds it to the
    origins this daemon admits (``CORS_ORIGINS`` at the time of the call).
    *for_launch* marks a code a launcher minted in this process for the daemon
    it starts next, so starting that daemon does not invalidate it.
    """
    global _bearer
    now = time.monotonic()
    bound = _normalize_origins(origins)
    with _lock:
        if _bearer is None:
            _bearer = secrets.token_urlsafe(32)
        for code in [c for c, (expires, _origins) in _codes.items() if expires <= now]:
            del _codes[code]
        code = secrets.token_urlsafe(24)
        _codes[code] = (now + PAIRING_CODE_TTL_S, bound)
        if for_launch:
            _launch_codes.add(code)
        return code


def _normalize_origins(origins: Iterable[str] | None) -> tuple[str, ...]:
    if origins is None:
        from .config import CORS_ORIGINS

        origins = CORS_ORIGINS
    return tuple(str(origin) for origin in origins)


def _spend_code(presented: str, origin: str | None) -> bool:
    """Consume *presented* if it is live and, when *origin* is given, bound to it.

    Consumption and the origin check happen under one lock, so two concurrent
    redemptions of one code cannot both succeed and a refused origin leaves the
    code in place.
    """
    if not isinstance(presented, str):
        return False
    now = time.monotonic()
    with _lock:
        match = None
        for code in _codes:
            try:
                same = hmac.compare_digest(code, presented)
            except TypeError:
                same = False
            if same:
                match = code
        if match is None:
            return False
        expires, origins = _codes[match]
        if expires <= now:
            del _codes[match]
            return False
        if origin is not None and origin not in origins:
            return False
        del _codes[match]
        return True


def redeem_pairing_code(presented: str, *, origin: str | None = None) -> str | None:
    """Spend a code: the launch bearer when it is live, unused and bound to *origin*."""
    with _lock:
        bearer = _bearer
    if not _spend_code(presented, origin):
        return None
    return bearer


def redeem_pairing_grant(presented: str, *, origin: str) -> PairingGrant | None:
    """Spend a code for the admission bearer and a new decision capability."""
    with _lock:
        bearer = _bearer
    if bearer is None or not _spend_code(presented, origin):
        return None
    return PairingGrant(bearer=bearer, capability=mint_decision_capability(origin))


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def mint_decision_capability(origin: str) -> str:
    """A new decision capability for a page redeemed from *origin*; returns the secret."""
    now = time.monotonic()
    secret = secrets.token_urlsafe(32)
    principal = DecisionPrincipal(
        id=secrets.token_hex(16),
        epoch=daemon_epoch(),
        method="pairing",
        origin=origin,
    )
    with _lock:
        for digest in [
            d for d, capability in _capabilities.items() if capability.expires_at <= now
        ]:
            del _capabilities[digest]
        _capabilities[_digest(secret)] = _Capability(
            principal=principal,
            digest=_digest(secret),
            expires_at=now + DECISION_CAPABILITY_TTL_S,
        )
    return secret


def resolve_decision_capability(
    presented: str,
    *,
    origin: str,
    scope: str = DECISION_SCOPE,
) -> DecisionPrincipal | None:
    """The principal *presented* was issued to, or None if it cannot decide now.

    *origin* is the origin the request came from; a capability resolves only
    from the origin it was bound to, so there is no call that skips the check.
    None covers an unknown, expired or revoked secret, a capability from another
    daemon start, another origin or another scope. A resolved principal's
    capability is still live.
    """
    if not isinstance(presented, str) or not presented:
        return None
    now = time.monotonic()
    with _lock:
        capability = _capabilities.get(_digest(presented))
        if capability is None:
            return None
        if capability.expires_at <= now:
            del _capabilities[capability.digest]
            return None
        principal = capability.principal
        if principal.epoch != _epoch or principal.scope != scope:
            return None
        if origin != principal.origin:
            return None
        return principal


def revoke_decision_capability(presented: str) -> bool:
    """Revoke *presented* on explicit disconnect; True when it was live."""
    if not isinstance(presented, str) or not presented:
        return False
    with _lock:
        return _capabilities.pop(_digest(presented), None) is not None
