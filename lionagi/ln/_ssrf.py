# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""SSRF guard (CWE-918): validate caller-controlled hostnames before outbound HTTP."""

from __future__ import annotations

import ipaddress
import socket

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
