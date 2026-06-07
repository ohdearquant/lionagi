# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: SSRF guard local-address allowlist for loopback providers.

Providers such as Ollama run on the local machine and use loopback addresses
(e.g. http://localhost:11434).  The generic SSRF guard previously blocked all
loopback traffic, making those providers unusable.

The fix adds allow_local_network=True to EndpointConfig, which is propagated
to is_ssrf_safe(allow_local=True).  When allow_local is set:

  * Loopback addresses (127.0.0.0/8, ::1) are permitted.
  * Link-local and metadata addresses (169.254.0.0/16) remain blocked.
  * All other blocked ranges (RFC 1918, CGN, IPv6 private) remain blocked.

Attack model: a caller who sets allow_local_network=True on their endpoint
must NOT gain the ability to reach metadata services or other private ranges
that are not loopback.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from lionagi.ln._ssrf import is_ssrf_safe
from lionagi.service.connections.endpoint_config import EndpointConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(ip_str: str):
    """Return a getaddrinfo-shaped list for a single IP."""
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip_str, 0))]


def _make_endpoint_with_url(base_url: str, *, allow_local_network: bool = False):
    """Create a minimal Endpoint whose full_url is base_url + /test."""
    from lionagi.service.connections.endpoint import Endpoint

    config = EndpointConfig(
        name="test_endpoint",
        provider="test",
        base_url=base_url,
        endpoint="test",
        method="POST",
        allow_local_network=allow_local_network,
    )
    ep = Endpoint.__new__(Endpoint)
    ep.config = config
    ep.circuit_breaker = None
    ep.retry_config = None
    return ep


# ---------------------------------------------------------------------------
# 1. is_ssrf_safe with allow_local=True — loopback permitted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # IPv4 loopback
        "127.255.255.255",  # IPv4 loopback boundary
    ],
)
def test_loopback_ipv4_allowed_when_allow_local(ip):
    """Loopback IPv4 addresses pass the guard when allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("localhost", allow_local=True) is True


def test_ipv6_loopback_allowed_when_allow_local():
    """::1 (IPv6 loopback) passes the guard when allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("::1")):
        assert is_ssrf_safe("localhost", allow_local=True) is True


def test_localhost_allowed_with_allow_local():
    """'localhost' (real DNS, resolves to 127.0.0.1 or ::1) passes when allow_local=True."""
    assert is_ssrf_safe("localhost", allow_local=True) is True


# ---------------------------------------------------------------------------
# 2. is_ssrf_safe with allow_local=True — IMDS / metadata still blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # AWS / GCP IMDS
        "169.254.0.1",  # link-local range
    ],
)
def test_imds_still_blocked_when_allow_local(ip):
    """Link-local / metadata addresses remain blocked even with allow_local=True.

    This is the core security invariant: allow_local ONLY exempts loopback,
    not the full link-local range (169.254.0.0/16).
    """
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("evil.internal", allow_local=True) is False


# ---------------------------------------------------------------------------
# 3. is_ssrf_safe with allow_local=True — other private ranges still blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "100.64.0.1",  # CGN (RFC 6598)
        "fc00::1",  # IPv6 unique local
        "fe80::1",  # IPv6 link-local
    ],
)
def test_private_ranges_still_blocked_when_allow_local(ip):
    """RFC 1918, CGN, and IPv6 private ranges remain blocked when allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("internal.example", allow_local=True) is False


# ---------------------------------------------------------------------------
# 4. is_ssrf_safe default — loopback still blocked (backward compat)
# ---------------------------------------------------------------------------


def test_loopback_blocked_by_default():
    """Without allow_local=True, loopback is blocked (backward-compatible)."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        assert is_ssrf_safe("localhost") is False


def test_localhost_blocked_by_default():
    """'localhost' is blocked when allow_local is not set (backward-compatible)."""
    assert is_ssrf_safe("localhost") is False


# ---------------------------------------------------------------------------
# 5. Public IPs still permitted with allow_local=True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
    ],
)
def test_public_ips_still_safe_with_allow_local(ip):
    """Public IPs remain safe when allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("example.com", allow_local=True) is True


# ---------------------------------------------------------------------------
# 6. EndpointConfig.allow_local_network field
# ---------------------------------------------------------------------------


def test_endpoint_config_allow_local_network_defaults_false():
    """allow_local_network defaults to False — safe by default."""
    config = EndpointConfig(
        name="test",
        provider="test",
        base_url="https://api.example.com",
        endpoint="chat",
    )
    assert config.allow_local_network is False


def test_endpoint_config_allow_local_network_can_be_set():
    """allow_local_network can be explicitly set to True."""
    config = EndpointConfig(
        name="test",
        provider="test",
        base_url="http://localhost:11434/v1",
        endpoint="chat/completions",
        allow_local_network=True,
    )
    assert config.allow_local_network is True


# ---------------------------------------------------------------------------
# 7. Endpoint._assert_ssrf_safe_url — integration with allow_local_network
# ---------------------------------------------------------------------------


def test_assert_ssrf_safe_url_blocks_localhost_by_default():
    """Without allow_local_network, localhost URL raises PermissionError."""
    ep = _make_endpoint_with_url("http://localhost:11434/v1", allow_local_network=False)
    with pytest.raises(PermissionError, match="SSRF guard"):
        ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_allows_localhost_when_flag_set():
    """With allow_local_network=True, localhost URL passes the guard."""
    ep = _make_endpoint_with_url("http://localhost:11434/v1", allow_local_network=True)
    # Must not raise
    ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_allows_127_0_0_1_when_flag_set():
    """With allow_local_network=True, 127.0.0.1 URL passes the guard."""
    ep = _make_endpoint_with_url("http://127.0.0.1:11434/v1", allow_local_network=True)
    ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_blocks_imds_even_with_flag():
    """allow_local_network=True does NOT allow IMDS addresses (attack regression).

    An attacker who sets allow_local_network=True on their endpoint config must
    not gain access to cloud metadata services.  The flag exempts only loopback;
    169.254.169.254 remains blocked.
    """
    ep = _make_endpoint_with_url(
        "http://169.254.169.254/latest/meta-data", allow_local_network=True
    )
    with pytest.raises(PermissionError, match="SSRF guard"):
        ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_blocks_rfc1918_even_with_flag():
    """allow_local_network=True does NOT allow RFC 1918 addresses."""
    ep = _make_endpoint_with_url("http://10.0.0.1/api", allow_local_network=True)
    with pytest.raises(PermissionError, match="SSRF guard"):
        ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_blocks_public_ollama_spoof():
    """A URL spoofed to look local but resolving to a public IP is not affected."""
    ep = _make_endpoint_with_url("http://localhost:11434/v1", allow_local_network=True)
    # Simulate DNS rebinding: 'localhost' resolves to a public IP — still safe
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("8.8.8.8")):
        ep._assert_ssrf_safe_url()  # public IP always passes


def test_assert_ssrf_safe_url_blocks_imds_via_loopback_spoofed_name():
    """A hostname other than localhost with allow_local_network=True resolving to IMDS is blocked."""
    ep = _make_endpoint_with_url(
        "http://totally-local.example.com:11434/v1", allow_local_network=True
    )
    # DNS rebind: 'totally-local.example.com' resolves to IMDS IP
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("169.254.169.254")):
        with pytest.raises(PermissionError, match="SSRF guard"):
            ep._assert_ssrf_safe_url()


# ---------------------------------------------------------------------------
# 8. Ollama EndpointConfig sets allow_local_network=True via config
# ---------------------------------------------------------------------------


def test_ollama_endpoint_config_carries_allow_local_network():
    """Verify the Ollama-like endpoint config correctly sets allow_local_network."""
    config = EndpointConfig(
        name="ollama_chat",
        provider="ollama",
        base_url="http://localhost:11434/v1",
        endpoint="chat/completions",
        auth_type="none",
        allow_local_network=True,
    )
    assert config.allow_local_network is True
    # Sanity: full_url contains localhost
    assert "localhost" in config.full_url


def test_ollama_endpoint_config_via_endpoint_class():
    """EndpointConfig created directly for Ollama permits localhost via SSRF guard."""
    from lionagi.service.connections.endpoint import Endpoint

    config = EndpointConfig(
        name="ollama_chat",
        provider="ollama",
        base_url="http://localhost:11434/v1",
        endpoint="chat/completions",
        auth_type="none",
        allow_local_network=True,
    )
    ep = Endpoint.__new__(Endpoint)
    ep.config = config
    ep.circuit_breaker = None
    ep.retry_config = None

    # Must not raise for localhost with allow_local_network=True
    ep._assert_ssrf_safe_url()


def test_non_ollama_endpoint_with_localhost_still_blocked():
    """A non-Ollama endpoint targeting localhost is still blocked by default."""
    from lionagi.service.connections.endpoint import Endpoint

    config = EndpointConfig(
        name="attacker_endpoint",
        provider="openai",
        base_url="http://localhost:8080",
        endpoint="internal",
        allow_local_network=False,
    )
    ep = Endpoint.__new__(Endpoint)
    ep.config = config
    ep.circuit_breaker = None
    ep.retry_config = None

    with pytest.raises(PermissionError, match="SSRF guard"):
        ep._assert_ssrf_safe_url()
