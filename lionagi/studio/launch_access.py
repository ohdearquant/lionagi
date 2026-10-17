# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Access for a page on another origin, by launch, when no auth token is configured.

A daemon with no ``LIONAGI_STUDIO_AUTH_TOKEN`` refuses API requests from a browser
origin that is neither loopback nor its own. ``li studio`` opens the hosted page
anyway, so the page needs a bearer: this module holds one, minted per process and
kept in memory only. It is never put in the environment, so the processes the
daemon spawns do not inherit it, and it is never written anywhere.

The page does not receive the bearer in its URL. ``li studio`` opens it with a
one-time pairing code instead, and the page exchanges the code for the bearer
over the pairing route. A code is single use and expires after
``PAIRING_CODE_TTL_S`` seconds, so a URL that reaches a process list or a browser
history is spent by the time anyone reads it.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time

__all__ = (
    "PAIRING_CODE_TTL_S",
    "PAIRING_REDEEM_PATH",
    "launch_bearer",
    "mint_pairing_code",
    "redeem_pairing_code",
)

PAIRING_CODE_TTL_S = 120.0
# The one API path the bearer gate lets through without a bearer: the code it
# carries is the credential.
PAIRING_REDEEM_PATH = "/api/studio/pair"

_lock = threading.Lock()
_bearer: str | None = None
_codes: dict[str, float] = {}


def launch_bearer() -> str | None:
    """The bearer a page on another origin presents in this process, once one is minted."""
    with _lock:
        return _bearer


def mint_pairing_code() -> str:
    """A new single-use code for the launch bearer, minting the bearer on first use."""
    global _bearer
    now = time.monotonic()
    with _lock:
        if _bearer is None:
            _bearer = secrets.token_urlsafe(32)
        for code in [c for c, expires in _codes.items() if expires <= now]:
            del _codes[code]
        code = secrets.token_urlsafe(24)
        _codes[code] = now + PAIRING_CODE_TTL_S
        return code


def redeem_pairing_code(presented: str) -> str | None:
    """Spend a code: the launch bearer when it is live and unused, else None."""
    now = time.monotonic()
    with _lock:
        match = None
        for code in list(_codes):
            try:
                same = hmac.compare_digest(code, presented)
            except TypeError:
                same = False
            if same:
                match = code
        if match is None:
            return None
        expires = _codes.pop(match)
        if expires <= now:
            return None
        return _bearer
