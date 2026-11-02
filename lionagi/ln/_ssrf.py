# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Outbound HTTP host checks.

``is_ssrf_safe`` is the SSRF guard (CWE-918): it validates caller-controlled hostnames
before outbound HTTP. ``open_url`` opens a urllib request and sends one addressed to this
machine's loopback interface past any configured proxy.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import urllib.request

_BLOCKED_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(cidr)
    for cidr in [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "0.0.0.0/8",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "::ffff:0:0/96",
        "fe80::/10",
        "ff00::/8",
    ]
]

_LOOPBACK_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

# Only these exact hostname strings are accepted for allow_local=True — alternate
# encodings are intentionally excluded to prevent DNS-rebinding bypass.
_CANONICAL_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _unmap_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unmap IPv4-mapped/compatible IPv6 to IPv4 for blocked-net matching."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip

    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped

    # IPv4-compatible (::a.b.c.d, RFC 4291 section 2.5.5.1): 12 zero bytes + 4 IPv4 bytes.
    # Python's ipv4_mapped returns None for this form.
    b = ip.packed
    if b[:12] == b"\x00" * 12:
        return ipaddress.IPv4Address(b[12:])

    return ip


def is_ssrf_safe(hostname: str, *, allow_local: bool = False) -> bool:
    """True if hostname (not a full URL) is safe for outbound HTTP; resolves and rejects private/reserved ranges."""
    if not hostname:
        return False

    if allow_local:
        # Gate on raw hostname BEFORE DNS resolution to block rebinding
        if hostname not in _CANONICAL_LOCAL_HOSTS:
            return False

        try:
            results = socket.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM)
        except OSError:
            return False

        if not results:
            return False

        for _family, _type, _proto, _canonname, sockaddr in results:
            raw_ip = sockaddr[0]
            try:
                ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(raw_ip)
            except ValueError:
                return False

            # Keep original for ::1 which unmaps to 0.0.0.1 via IPv4-compat
            original_ip = ip
            ip = _unmap_ip(ip)

            if not (
                any(ip in lb for lb in _LOOPBACK_NETS)
                or any(original_ip in lb for lb in _LOOPBACK_NETS)
            ):
                return False

        return True

    try:
        results = socket.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM)
    except OSError:
        return False

    if not results:
        return False

    for _family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False

        ip = _unmap_ip(ip)

        if any(ip in net for net in _BLOCKED_NETS):
            return False

    return True


def _is_loopback_host(host: str | None) -> bool:
    """True for ``localhost``, an address in 127.0.0.0/8, ``::1``, or an IPv4-mapped loopback.

    *host* is a URL's hostname as ``urllib.parse.urlsplit`` returns it: lowercased, IPv6
    brackets removed. Names are not resolved, so no lookup decides where a request goes.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOOPBACK_NETS)


def open_url(request: str | urllib.request.Request, *, timeout: float) -> Any:
    """Open *request* with urllib, sending a request for a loopback host past any proxy.

    ``urllib.request.urlopen`` sends every request through the proxy that the environment
    (``http_proxy``, ``https_proxy``, ``all_proxy``) or, on macOS, the system settings name,
    loopback hosts included, and a proxy cannot reach this machine's own loopback interface.
    So a URL whose host is ``localhost``, an address in 127.0.0.0/8 or ``::1`` (bracketed in
    the URL) is opened by an opener with no proxy handler, and every other URL keeps
    ``urlopen`` and its proxies. A URL that cannot be split goes to ``urlopen`` too, which
    reports it as before. The response and the exceptions are urllib's own, so callers keep
    their ``with`` blocks and ``except`` clauses.
    """
    import urllib.parse
    import urllib.request

    url = request.full_url if isinstance(request, urllib.request.Request) else request
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        host = None
    if _is_loopback_host(host):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
