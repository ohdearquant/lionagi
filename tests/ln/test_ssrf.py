# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.ln._ssrf.is_ssrf_safe."""

import socket
from unittest.mock import patch

import pytest

from lionagi.ln._ssrf import is_ssrf_safe

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(ip_str: str):
    """Return a getaddrinfo-shaped list for a single IP."""
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip_str, 0))]


# ---------------------------------------------------------------------------
# 1. Public IPs — must return True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",  # Google DNS
        "1.1.1.1",  # Cloudflare
        "52.84.0.1",  # AWS CloudFront (public)
        "2001:4860:4860::8888",  # Google IPv6 DNS
    ],
)
def test_public_ips_safe(ip):
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("example.com") is True


# ---------------------------------------------------------------------------
# 2. Private / reserved IPv4 — must return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",  # RFC 1918
        "10.255.255.255",  # RFC 1918 boundary
        "172.16.0.1",  # RFC 1918
        "172.31.255.255",  # RFC 1918 boundary
        "192.168.1.1",  # RFC 1918
        "169.254.169.254",  # AWS IMDS
        "169.254.0.1",  # link-local
        "127.0.0.1",  # loopback
        "127.255.255.255",  # loopback boundary
        "0.0.0.0",  # bind-all exact address (MEDIUM 2 regression)
        "0.0.0.1",  # bind-all range
        "100.64.0.1",  # CGN (RFC 6598)
    ],
)
def test_private_ipv4_blocked(ip):
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("evil.internal") is False


# ---------------------------------------------------------------------------
# 3. Private IPv6 — must return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 unique local
        "fd00::1",  # IPv6 unique local
        "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",  # boundary
        "fe80::1",  # IPv6 link-local (HIGH 1 regression)
        "ff02::1",  # IPv6 multicast all-nodes (HIGH 1 regression)
        "ff00::1",  # IPv6 multicast range start (HIGH 1 regression)
    ],
)
def test_private_ipv6_blocked(ip):
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("evil.internal") is False


def test_ipv6_link_local_scoped_blocked():
    """fe80::1%lo0 — scoped link-local. Python strips the scope before parsing."""
    # Python's ipaddress.ip_address strips the scope ID (e.g. %lo0) when
    # socket.getaddrinfo returns the raw IP string. The raw IP returned by
    # getaddrinfo on most platforms is the unscoped form ("fe80::1"), so we
    # test that the unscoped form is correctly blocked. Scoped forms returned
    # as "fe80::1%lo0" would cause ipaddress.ip_address to raise ValueError,
    # which the guard's except-ValueError branch already handles (fail-closed).
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("fe80::1")):
        assert is_ssrf_safe("link-local.example.com") is False


# ---------------------------------------------------------------------------
# 4. IPv4-mapped IPv6 — CRITICAL: must return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ipv4_mapped",
    [
        "::ffff:169.254.169.254",  # AWS IMDS via IPv4-mapped IPv6
        "::ffff:10.0.0.1",  # Private via IPv4-mapped IPv6
        "::ffff:192.168.1.1",  # Private via IPv4-mapped IPv6
        "::ffff:127.0.0.1",  # Loopback via IPv4-mapped IPv6
    ],
)
def test_ipv4_mapped_ipv6_blocked(ipv4_mapped):
    """IPv4-mapped IPv6 must not bypass IPv4 network checks."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ipv4_mapped)):
        assert is_ssrf_safe("rebind.example.com") is False


def test_ipv4_mapped_public_safe():
    """IPv4-mapped public IPv6 should be safe."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("::ffff:8.8.8.8")):
        assert is_ssrf_safe("dns.example.com") is True


# ---------------------------------------------------------------------------
# 5. DNS resolution failure — must fail closed
# ---------------------------------------------------------------------------


def test_dns_failure_returns_false():
    with patch("socket.getaddrinfo", side_effect=OSError("NXDOMAIN")):
        assert is_ssrf_safe("nonexistent.example.invalid") is False


def test_empty_hostname_returns_false():
    assert is_ssrf_safe("") is False


# ---------------------------------------------------------------------------
# 6. Multiple A records — any private IP blocks the request
# ---------------------------------------------------------------------------


def test_any_private_ip_blocks():
    """If getaddrinfo returns both public and private IPs, block."""
    results = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
    ]
    with patch("socket.getaddrinfo", return_value=results):
        assert is_ssrf_safe("mixed.example.com") is False


# ---------------------------------------------------------------------------
# 7. Localhost names
# ---------------------------------------------------------------------------


def test_localhost_blocked():
    """'localhost' resolves to 127.0.0.1 or ::1, both blocked."""
    # Do not mock — use real DNS for this test (localhost is always loopback)
    assert is_ssrf_safe("localhost") is False


# ---------------------------------------------------------------------------
# 8. Integration with reader.py call site
# ---------------------------------------------------------------------------


def test_reader_ssrf_guard_blocks_metadata_url(monkeypatch):
    """ReaderTool rejects SSRF even when hostname is in the allowlist."""
    import asyncio

    import lionagi.tools.file.reader as reader_mod
    from lionagi.tools.file.reader import ReaderRequest, ReaderTool

    tool = ReaderTool(allowed_url_hosts={"metadata.example.com"})

    # Monkeypatch is_ssrf_safe to simulate DNS-resolving to 169.254.169.254
    monkeypatch.setattr(reader_mod, "is_ssrf_safe", lambda h: False)

    result = asyncio.get_event_loop().run_until_complete(
        tool.handle_request(
            ReaderRequest(action="open", path="https://metadata.example.com/file.pdf")
        )
    )
    assert result.success is False
    assert "blocked" in result.error.lower()
