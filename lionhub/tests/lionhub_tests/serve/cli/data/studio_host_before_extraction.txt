# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The canonical form of a host value, for every reader of ``LIONAGI_STUDIO_HOST``.

The variable is written by an operator, a browser sends a host in its own
spelling, and the daemon has to decide whether the two name the same address.
Each reader used to strip whitespace, remove brackets and lowercase for itself,
so a spelling one reader understood and another did not was a separate bug: a
bind host in brackets widened nothing, and a browser's canonical IPv6 form was
refused for a bind written in its long form.

This module is deliberately thin: the CLI imports it, so it must not pull the
web stack into the CLI's import path. Only ``ipaddress`` is imported.

Brackets and whitespace are the *name's* syntax, not part of the value. A
non-canonical address is the same address -- ``2001:0db8::1`` and
``2001:db8::1`` name one interface -- and an IPv4-mapped address is the IPv4
address in another spelling, which is the form a browser reaches it as. All
three collapse to one string here so that no reader has to know about them.
"""

from __future__ import annotations

import ipaddress

__all__ = ("admitted_host_names", "is_unspecified_host", "normalize_host")


def normalize_host(raw: str) -> str:
    """*raw* in canonical form: whitespace and one bracket pair removed, lowercase.

    A value that is an IP address becomes the compressed lowercase form
    ``ipaddress`` prints, with an IPv4-mapped address unwrapped to its IPv4
    address (``::ffff:192.168.1.5`` -> ``192.168.1.5``). Anything else -- a name,
    a value carrying a port -- is returned stripped and lowercased. A value that
    is empty or holds nothing but brackets stays empty rather than becoming a
    name of its own.
    """
    host = (raw or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    host = host.lower()
    if not host:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return str(getattr(address, "ipv4_mapped", None) or address)


def is_unspecified_host(bind_host: str) -> bool:
    """Whether *bind_host* names every interface rather than one address.

    A wildcard (`0.0.0.0`, `::`, or an IPv6 spelling of one) binds all of them, so
    it is not an address a page is opened at and must not widen the admitted Host
    set. Judged by `ipaddress` on the canonical form, not by matching the literal
    `0.0.0.0`: `::` binds the same interfaces and the string test let it through.
    A blank value is unspecified too -- there is no name to admit. A value that is
    not an address at all is left to the caller, which admits it as the operator's
    chosen name.
    """
    host = normalize_host(bind_host)
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def admitted_host_names(bind_host: str) -> frozenset[str]:
    """The Host names a daemon bound to *bind_host* answers, in canonical form.

    The loopback names always, and the bind address itself when it is one
    address. A page served under any other name gets every request refused,
    so the CLI reads this same set before it prints a link to a page.
    """
    names = {"localhost", "127.0.0.1", "::1"}
    if not is_unspecified_host(bind_host):
        names.add(normalize_host(bind_host))
    return frozenset(names)
