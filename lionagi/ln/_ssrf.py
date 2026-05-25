# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""SSRF (Server-Side Request Forgery) guard for outbound HTTP requests.

All outbound HTTP requests that accept caller-controlled hostnames MUST
call :func:`is_ssrf_safe` before issuing the request.

DNS re-resolution note
----------------------
This module resolves the hostname once via ``socket.getaddrinfo`` and rejects
any result that maps to a private/reserved IP range.  There is a small window
between this check and the actual TCP connect where a DNS entry with a very
short TTL could change (DNS rebinding).  This is an accepted residual risk for
the current implementation.  If DNS rebinding becomes a concrete threat,
implement a custom ``httpx.AsyncHTTPTransport`` that pins the resolved address.

CWE reference: CWE-918.
"""

from __future__ import annotations

import ipaddress
import socket

# Blocked networks — covers:
#   10/8        private (RFC 1918)
#   172.16/12   private (RFC 1918)
#   192.168/16  private (RFC 1918)
#   169.254/16  link-local / AWS IMDS / GCP metadata
#   127/8       loopback
#   0.0.0.0/8   bind-all (IANA reserved; "this network")
#   100.64/10   Carrier-grade NAT (RFC 6598) — reachable in cloud envs
#   ::1/128     IPv6 loopback
#   fc00::/7    IPv6 unique local (fc00:: and fd00:: ranges)
#   ::ffff:0:0/96  IPv4-mapped IPv6 catch-all (belt-and-suspenders)
#   fe80::/10   IPv6 link-local (SLAAC; scoped service exposure via %iface)
#   ff00::/8    IPv6 multicast (defense-in-depth; no legitimate server targets)
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


def is_ssrf_safe(hostname: str) -> bool:
    """Return True if *hostname* resolves only to public, routable IPs.

    Resolves via ``socket.getaddrinfo`` and checks every returned address
    against :data:`_BLOCKED_NETS`.  Unresolvable hostnames are rejected
    (return False) so that DNS failures fail-closed.

    Both IPv4-mapped (``::ffff:a.b.c.d``) and IPv4-compatible (``::a.b.c.d``)
    IPv6 addresses are unmapped to their IPv4 form before checking, so they
    correctly match IPv4 blocked networks.

    Args:
        hostname: Hostname to validate.  Do NOT pass a full URL; extract the
            host with ``urllib.parse.urlparse(url).hostname`` first.

    Returns:
        True  — all resolved IPs are in public ranges; safe to connect.
        False — at least one IP is in a blocked range, or resolution failed.

    Examples::

        >>> is_ssrf_safe("api.openai.com")
        True
        >>> is_ssrf_safe("169.254.169.254")
        False
        >>> is_ssrf_safe("localhost")
        False
        >>> is_ssrf_safe("::ffff:169.254.169.254")
        False
        >>> is_ssrf_safe("::169.254.169.254")
        False
    """
    if not hostname:
        return False
    try:
        # family=0  → both IPv4 and IPv6
        # type=SOCK_STREAM → TCP only (we don't do UDP)
        results = socket.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM)
    except OSError:
        # DNS failure, invalid hostname, etc. → fail closed
        return False

    if not results:
        return False

    for _family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0]
        try:
            ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(raw_ip)
        except ValueError:
            # Unrecognised format — fail closed
            return False

        # Unmap IPv4-mapped IPv6 (::ffff:a.b.c.d → a.b.c.d) so it matches
        # IPv4 blocked networks.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        # Unmap IPv4-compatible IPv6 (::a.b.c.d → a.b.c.d) — the deprecated
        # form where the IPv4 address is embedded directly without the 0xffff
        # marker.  Python's ipv4_mapped returns None for this form, so we must
        # detect it manually.  RFC 4291 §2.5.5.1: packed representation is
        # 10 zero bytes + 2 zero bytes + 4 IPv4 bytes.
        # Fix for CWE-918 / issue #1125: ::169.254.169.254 reached AWS IMDS.
        if isinstance(ip, ipaddress.IPv6Address):
            b = ip.packed
            if b[:12] == b"\x00" * 12:
                ip = ipaddress.IPv4Address(b[12:])

        if any(ip in net for net in _BLOCKED_NETS):
            return False

    return True
