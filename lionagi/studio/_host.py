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

__all__ = ("normalize_host",)


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
