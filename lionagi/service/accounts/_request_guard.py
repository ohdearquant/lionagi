# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checks a routed Anthropic Messages request passes before an account key is attached.

The endpoint runs these on its own request path; the router runs them again before it reserves
an account. They read only the request and the endpoint's configuration.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ._types import AccountRoutingError, RouterErrorCode

__all__ = ("header_pairs", "routed_client_kwargs", "validate_routed_request")

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
