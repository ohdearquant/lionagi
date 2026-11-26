# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The one way the CLI builds a request to its Studio daemon.

Every Studio HTTP client in the CLI builds its request with :func:`studio_request`, so
the rule for the daemon's bearer lives in one place: it is sent only to the configured
Studio (``LIONAGI_STUDIO_URL``, or the host and port settings), never to another
address a caller probes, and never across a redirect. A 401 is turned into a message by
:func:`with_unauthorized_hint`, which names the variable to set and never its value.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from lionagi.ln._ssrf import _is_loopback_host
from lionhub.orchestration.schedule.parsing import _base_url
from lionhub.state.log_channels import warn

__all__ = (
    "STUDIO_TOKEN_ENV",
    "STUDIO_UNAUTHORIZED_HINT",
    "studio_request",
    "with_unauthorized_hint",
)

#: The variable a Studio daemon reads its bearer token from, and the one its CLI clients send.
STUDIO_TOKEN_ENV = "LIONAGI_STUDIO_AUTH_TOKEN"  # noqa: S105 — a variable name, not a value

#: What a client says when Studio answers 401: the variable to set, never its value.
STUDIO_UNAUTHORIZED_HINT = (
    f"Studio refused the request as unauthorized; set {STUDIO_TOKEN_ENV} to the token the "
    "daemon was started with."
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_warned_cleartext_origins: set[tuple[str, str, int]] = set()


def _origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of *url*, the port filled in from the scheme; None if unreadable."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def _bearer_for(url: str) -> str | None:
    """The bearer to send to *url*: the token, when *url* is on the configured Studio's origin.

    Plain ``http`` to a host that is not loopback carries the token in cleartext; it is
    still sent, since that is a deployment the operator chose, and a warning names the
    origin once per process.
    """
    token = os.environ.get(STUDIO_TOKEN_ENV)
    if not token:
        return None
    origin = _origin(url)
    if origin is None or origin != _origin(_base_url()):
        return None
    scheme, host, _port = origin
    if scheme == "http" and not _is_loopback_host(host) and origin not in _warned_cleartext_origins:
        _warned_cleartext_origins.add(origin)
        warn(
            f"Sending {STUDIO_TOKEN_ENV} to {host} over plain http: the token crosses the "
            "network unencrypted. Use an https Studio URL, or a loopback or private-network "
            "tunnel, for a daemon on another machine."
        )
    return token


def studio_request(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None
) -> urllib.request.Request:
    """A request for *url* on the Studio daemon, carrying the bearer when it is owed one.

    A JSON *body* is encoded and declared; a method that carries a body declares JSON
    even without one, as the daemon's routes expect. The bearer is an unredirected
    header, so urllib does not carry it to the target of a redirect.
    """
    data = json.dumps(body).encode() if body is not None else None
    declares_json = data is not None or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    request = urllib.request.Request(  # noqa: S310 — an http(s) Studio URL
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if declares_json else {},
    )
    token = _bearer_for(url)
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def with_unauthorized_hint(status: int, message: str) -> str:
    """*message*, followed by :data:`STUDIO_UNAUTHORIZED_HINT` when *status* is 401."""
    return f"{message}. {STUDIO_UNAUTHORIZED_HINT}" if status == 401 else message
