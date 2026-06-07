# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""SSRF (Server-Side Request Forgery) guard for outbound HTTP requests.

All outbound HTTP requests that accept caller-controlled hostnames MUST
call :func:`is_ssrf_safe` before issuing the request.

Local-address allowlist
-----------------------
Providers that are documented to run on the local machine (e.g. Ollama at
``http://localhost:11434``) may set ``allow_local=True`` when calling
:func:`is_ssrf_safe`.  When ``allow_local=True``:

* The raw hostname (before DNS resolution) MUST exactly match one of the
  canonical loopback literals: ``localhost``, ``127.0.0.1``, ``::1``.
  Any other hostname string is rejected immediately, regardless of what it
  resolves to.  This eliminates DNS-rebinding attacks.

* Alternate numeric encodings of the loopback address (decimal integer
  ``2130706433``, octal ``017700000001``, hex ``0x7f000001``, short form
  ``127.1``, zero-padded ``127.000.000.001``) are NOT in the canonical set
  and are therefore rejected.

* IPv4-mapped/compatible IPv6 loopback forms (``::ffff:127.0.0.1``,
  ``::127.0.0.1``) are NOT in the canonical set and are rejected.

* After DNS resolution, every resolved address is checked to confirm it is
  truly a loopback address.  Non-loopback results (including public IPs) are
  rejected — a local-mode endpoint pointing at a public IP is not a valid
  local endpoint.

* All other blocked ranges — link-local, metadata endpoints such as
  ``169.254.169.254`` (AWS/GCP IMDS), private RFC 1918, CGN, IPv6 private
  — remain blocked regardless of this flag.

DNS re-resolution note
----------------------
By gating on the raw hostname string before resolution, this module
eliminates the DNS-rebinding window that existed when the loopback check
was performed only on the resolved address.  The canonical allowlist is
the primary gate; the post-resolution loopback check provides defense in
depth.

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

# Networks that are loopback — used for post-resolution defense-in-depth when
# allow_local=True to ensure a canonical hostname actually resolved to loopback.
_LOOPBACK_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

# Canonical loopback hostname literals accepted by allow_local=True.
# The loopback exemption is granted on the RAW PARSED HOSTNAME STRING before
# DNS resolution.  Only these exact strings are accepted:
#
#   "localhost"   — the standard loopback name
#   "127.0.0.1"   — canonical IPv4 loopback decimal literal
#   "::1"         — canonical IPv6 loopback (urlparse strips brackets, so
#                   "[::1]" in a URL becomes "::1" at the hostname level)
#
# Alternate encodings of 127.0.0.1 (decimal integer 2130706433, octal
# 017700000001, hex 0x7f000001, short form 127.1, zero-padded
# 127.000.000.001) are NOT included.  IPv4-mapped / IPv4-compatible IPv6
# loopback forms (::ffff:127.0.0.1, ::127.0.0.1) are NOT included.  Any
# hostname that does not appear in this set is rejected immediately when
# allow_local=True, regardless of what it resolves to.
_CANONICAL_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _unmap_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unmap IPv4-mapped and IPv4-compatible IPv6 to their IPv4 form.

    IPv4-mapped (::ffff:a.b.c.d) and IPv4-compatible (::a.b.c.d) IPv6
    addresses are converted to their IPv4 equivalents so they correctly
    match IPv4 blocked networks.  Pure IPv6 addresses are returned unchanged.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip

    # IPv4-mapped: ::ffff:a.b.c.d — Python exposes this directly.
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped

    # IPv4-compatible: ::a.b.c.d — deprecated form where the IPv4 address is
    # embedded directly without the 0xffff marker.  Python's ipv4_mapped
    # returns None for this form.  RFC 4291 §2.5.5.1: packed representation
    # is 10 zero bytes + 2 zero bytes + 4 IPv4 bytes.
    b = ip.packed
    if b[:12] == b"\x00" * 12:
        return ipaddress.IPv4Address(b[12:])

    return ip


def is_ssrf_safe(hostname: str, *, allow_local: bool = False) -> bool:
    """Return True if *hostname* is safe for an outbound HTTP request.

    When ``allow_local=False`` (the default): resolves the hostname via
    ``socket.getaddrinfo`` and rejects any result that maps to a
    private/reserved IP range.  Unresolvable hostnames are rejected
    (fail-closed).

    When ``allow_local=True``: the hostname MUST exactly match one of the
    canonical loopback literals (``localhost``, ``127.0.0.1``, ``::1``).
    Any other hostname — including alternate numeric encodings or external
    hostnames that happen to resolve to loopback — is rejected immediately.
    After DNS resolution, every address is verified to be a true loopback
    address; non-loopback results (including public IPs) are rejected.

    Both IPv4-mapped (``::ffff:a.b.c.d``) and IPv4-compatible (``::a.b.c.d``)
    IPv6 addresses are unmapped to their IPv4 form before checking.

    Args:
        hostname: Hostname to validate.  Do NOT pass a full URL; extract the
            host with ``urllib.parse.urlparse(url).hostname`` first.
        allow_local: When True, permit the endpoint to use canonical loopback
            addresses only.  Use this only for providers documented to run
            locally (e.g. Ollama).  All other blocked ranges — including
            link-local and metadata endpoints such as ``169.254.169.254`` —
            remain blocked regardless of this flag.

    Returns:
        True  — hostname is in the canonical loopback set (if allow_local)
                and all resolved IPs are in permitted ranges.
        False — hostname fails the canonical check, at least one resolved IP
                is in a blocked range, or resolution failed.

    Examples::

        >>> is_ssrf_safe("api.openai.com")
        True
        >>> is_ssrf_safe("169.254.169.254")
        False
        >>> is_ssrf_safe("localhost")
        False
        >>> is_ssrf_safe("localhost", allow_local=True)
        True
        >>> is_ssrf_safe("127.0.0.1", allow_local=True)
        True
        >>> is_ssrf_safe("::1", allow_local=True)
        True
        >>> is_ssrf_safe("169.254.169.254", allow_local=True)
        False
        >>> is_ssrf_safe("::ffff:169.254.169.254")
        False
        >>> is_ssrf_safe("::169.254.169.254")
        False
    """
    if not hostname:
        return False

    if allow_local:
        # Gate on the raw hostname string BEFORE DNS resolution.
        # Only the exact canonical loopback literals are accepted.
        # This eliminates DNS rebinding (an external hostname resolving to
        # 127.0.0.1 does NOT pass) and alternate encodings (2130706433,
        # 0x7f000001, 017700000001, 127.1, ::ffff:127.0.0.1, etc.).
        if hostname not in _CANONICAL_LOCAL_HOSTS:
            return False

        # Canonical hostname accepted — resolve and verify every address is
        # loopback (defense in depth).
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

            # Retain the original address for the loopback check (::1 unmaps
            # to 0.0.0.1 via the IPv4-compat path, which would not match
            # ::1/128; we must test the original against ::1/128).
            original_ip = ip
            ip = _unmap_ip(ip)

            # Every resolved address must be loopback.  A canonical loopback
            # name resolving to a public IP is not a valid local endpoint.
            if not (
                any(ip in lb for lb in _LOOPBACK_NETS)
                or any(original_ip in lb for lb in _LOOPBACK_NETS)
            ):
                return False

        return True

    # allow_local=False path: standard SSRF check — any blocked range fails.
    try:
        results = socket.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM)
    except OSError:
        # DNS failure, invalid hostname, etc. → fail closed
        return False

    if not results:
        return False

    for _family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            # Unrecognised format — fail closed
            return False

        # Unmap IPv4-mapped and IPv4-compatible IPv6 addresses to their IPv4
        # equivalents so they correctly match IPv4 blocked networks.
        # We check the unmapped form for all networks.  The ::ffff:0:0/96
        # network in _BLOCKED_NETS acts as belt-and-suspenders for any
        # IPv6-mapped address that survives unmapping (e.g. if future variants
        # are not covered by _unmap_ip), but for a cleanly unmapped IPv4
        # address we only check the unmapped form to avoid false positives
        # (e.g. ::ffff:8.8.8.8 unmaps to 8.8.8.8 which is public and safe).
        ip = _unmap_ip(ip)

        if any(ip in net for net in _BLOCKED_NETS):
            return False

    return True
