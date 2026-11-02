# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.ln._ssrf: is_ssrf_safe and open_url."""

import socket
import urllib.request
from unittest.mock import patch

import pytest

from lionagi.ln import _ssrf
from lionagi.ln._ssrf import is_ssrf_safe


def _mock_getaddrinfo(ip_str: str):
    """Return a getaddrinfo-shaped list for a single IP."""
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip_str, 0))]


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
    """fe80::1%lo0 — scoped link-local; getaddrinfo returns unscoped form on most platforms."""
    # Scoped forms ("fe80::1%lo0") cause ipaddress.ip_address to raise ValueError,
    # which the guard's except-ValueError branch handles (fail-closed).
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("fe80::1")):
        assert is_ssrf_safe("link-local.example.com") is False


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


# 4b. IPv4-compatible IPv6 (::a.b.c.d) — must return False (CWE-918)
# Deprecated form without the 0xffff marker; ipv4_mapped returns None so the
# ::ffff: unmap path does NOT catch it — separate fix required.


@pytest.mark.parametrize(
    "ipv4_compat",
    [
        "::169.254.169.254",  # AWS/GCP IMDS — the live IMDS bypass
        "::10.0.0.1",  # RFC 1918 private via IPv4-compat
        "::192.168.1.1",  # RFC 1918 private via IPv4-compat
        "::127.0.0.1",  # Loopback via IPv4-compat
        "::172.16.0.1",  # RFC 1918 private via IPv4-compat
        "::100.64.0.1",  # CGN (RFC 6598) via IPv4-compat
    ],
)
def test_ipv4_compatible_ipv6_blocked(ipv4_compat):
    """IPv4-compatible IPv6 (::a.b.c.d) must not bypass IPv4 network checks."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ipv4_compat)):
        assert is_ssrf_safe("evil.internal") is False


def test_ipv4_compatible_public_safe():
    """IPv4-compatible IPv6 wrapping a public IP should be safe."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("::8.8.8.8")):
        assert is_ssrf_safe("dns.example.com") is True


def test_dns_failure_returns_false():
    with patch("socket.getaddrinfo", side_effect=OSError("NXDOMAIN")):
        assert is_ssrf_safe("nonexistent.example.invalid") is False


def test_empty_hostname_returns_false():
    assert is_ssrf_safe("") is False


def test_any_private_ip_blocks():
    """If getaddrinfo returns both public and private IPs, block."""
    results = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
    ]
    with patch("socket.getaddrinfo", return_value=results):
        assert is_ssrf_safe("mixed.example.com") is False


def test_localhost_blocked():
    """'localhost' resolves to 127.0.0.1 or ::1, both blocked."""
    # Do not mock — use real DNS for this test (localhost is always loopback)
    assert is_ssrf_safe("localhost") is False


class _RecordingOpener:
    """Stands in for the opener that is built without proxy handlers."""

    def __init__(self, calls):
        self._calls = calls

    def open(self, request, timeout):
        self._calls.append((request, timeout))
        return "opened without proxy"


def _forbidden(*args, **kwargs):
    raise AssertionError("the wrong way of opening the request was used")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765/health",
        "http://LOCALHOST:8765/health",
        "http://127.0.0.1:8765/health",
        "http://127.8.9.10:8765/health",
        "http://[::1]:8765/health",
        "http://[::ffff:127.0.0.1]:8765/health",
    ],
)
def test_open_url_loopback_host_opens_without_proxy_handler(url, monkeypatch):
    """A loopback host is opened by an opener whose only handler has no proxies."""
    handlers_built = []
    opened = []

    def fake_build_opener(*handlers):
        handlers_built.append(handlers)
        return _RecordingOpener(opened)

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)

    assert _ssrf.open_url(url, timeout=3.5) == "opened without proxy"

    assert opened == [(url, 3.5)]
    (handlers,) = handlers_built
    assert len(handlers) == 1
    assert isinstance(handlers[0], urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://studio.example.test:8765/health",
        "https://example.com/",
        "http://10.0.0.5:8765/health",
        "http://[2001:db8::1]:8765/health",
        "http://localhost.example.test:8765/health",
        "http://127.0.0.1.example.test:8765/health",
        "http://[::1/health",  # malformed: no loopback verdict, urlopen decides
    ],
)
def test_open_url_other_hosts_keep_urlopen(url, monkeypatch):
    """Any host that is not loopback keeps the standard opener, proxies included."""
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return "opened through urlopen"

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", _forbidden)

    assert _ssrf.open_url(url, timeout=2.5) == "opened through urlopen"

    assert calls == [(url, 2.5)]


def test_open_url_classifies_request_objects_by_their_url(monkeypatch):
    loopback = urllib.request.Request("http://127.0.0.1:8765/api/schedules/", method="GET")
    elsewhere = urllib.request.Request("http://studio.example.test/api/schedules/", method="GET")
    opened = []
    proxied = []

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _RecordingOpener(opened))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: proxied.append((request, timeout))
    )

    _ssrf.open_url(loopback, timeout=1.0)
    _ssrf.open_url(elsewhere, timeout=2.0)

    assert opened == [(loopback, 1.0)]
    assert proxied == [(elsewhere, 2.0)]
