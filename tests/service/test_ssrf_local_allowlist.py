# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: SSRF guard local-address allowlist for loopback providers.

Providers such as Ollama run on the local machine and use loopback addresses
(e.g. http://localhost:11434).  The generic SSRF guard previously blocked all
loopback traffic, making those providers unusable.

The fix adds allow_local_network=True to EndpointConfig, which is propagated
to is_ssrf_safe(allow_local=True).  When allow_local is set:

  * Only the exact canonical loopback hostname literals are permitted:
    localhost, 127.0.0.1, ::1, [::1].  The check is performed on the raw
    hostname string BEFORE DNS resolution.

  * Any other hostname — including external names that resolve to 127.0.0.1
    (DNS rebinding) and alternate numeric encodings of the loopback address —
    is rejected immediately.

  * After DNS resolution, every resolved address is verified to be loopback.
    A canonical hostname resolving to a public or non-loopback address is
    rejected.

  * Link-local and metadata addresses (169.254.0.0/16) remain blocked.

  * All other blocked ranges (RFC 1918, CGN, IPv6 private) remain blocked.

Attack model: a caller who sets allow_local_network=True on their endpoint
must NOT gain the ability to reach metadata services, private ranges, or
any host other than the canonical loopback literals.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

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
# 1. is_ssrf_safe with allow_local=True — canonical loopback literals allowed
# ---------------------------------------------------------------------------


def test_localhost_allowed_with_allow_local():
    """'localhost' passes when allow_local=True (canonical literal)."""
    assert is_ssrf_safe("localhost", allow_local=True) is True


def test_127_0_0_1_allowed_with_allow_local():
    """'127.0.0.1' passes when allow_local=True (canonical literal)."""
    assert is_ssrf_safe("127.0.0.1", allow_local=True) is True


def test_ipv6_loopback_allowed_when_allow_local():
    """'::1' (IPv6 loopback) passes when allow_local=True (canonical literal)."""
    assert is_ssrf_safe("::1", allow_local=True) is True


# ---------------------------------------------------------------------------
# 2. DNS rebinding rejection — Invariant A
#    A non-canonical hostname that resolves to 127.0.0.1 must be rejected
#    even with allow_local=True, because the raw hostname check fires first.
# ---------------------------------------------------------------------------


def test_dns_rebinding_external_hostname_resolving_to_loopback_rejected():
    """DNS rebinding: evil.example resolving to 127.0.0.1 is rejected with allow_local=True.

    The loopback exemption is gated on the raw hostname string before DNS
    resolution.  An external hostname that happens to resolve to 127.0.0.1
    does NOT qualify as a local endpoint.
    """
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        assert is_ssrf_safe("evil.example", allow_local=True) is False


def test_dns_rebinding_totally_local_name_resolving_to_loopback_rejected():
    """DNS rebinding: a name designed to look local but not in canonical set is rejected."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        assert is_ssrf_safe("totally-local.internal", allow_local=True) is False


def test_dns_rebinding_loopback_label_resolving_to_loopback_rejected():
    """DNS rebinding: 'loopback.example' resolving to 127.0.0.1 is rejected."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        assert is_ssrf_safe("loopback.example", allow_local=True) is False


# ---------------------------------------------------------------------------
# 3. Alternate encodings rejected — Invariant B
#    Non-canonical spellings of the loopback address are not in the canonical
#    set and are therefore rejected before DNS resolution.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding",
    [
        "2130706433",  # decimal integer for 127.0.0.1
        "017700000001",  # octal for 127.0.0.1
        "0x7f000001",  # hex for 127.0.0.1
        "127.1",  # short-form IPv4
        "127.000.000.001",  # zero-padded IPv4
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
        "::127.0.0.1",  # IPv4-compatible IPv6 loopback
    ],
)
def test_alternate_loopback_encoding_rejected(encoding):
    """Alternate loopback spellings are rejected with allow_local=True.

    Only the canonical literals (localhost, 127.0.0.1, ::1, [::1]) are
    accepted.  Non-canonical encodings are rejected before DNS resolution,
    regardless of what they resolve to.
    """
    # We do not even need to mock DNS — the raw hostname check fires first.
    assert is_ssrf_safe(encoding, allow_local=True) is False


# ---------------------------------------------------------------------------
# 4. Defense in depth: post-resolution loopback verification — Invariant C
#    Even a canonical hostname resolving to a non-loopback IP is rejected.
# ---------------------------------------------------------------------------


def test_canonical_hostname_resolving_to_public_ip_rejected():
    """If 'localhost' resolves to a public IP (DNS hijack), reject it.

    A local-mode endpoint pointing at a public IP is not a valid local
    endpoint — the caller declared local intent but the network says otherwise.
    """
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("8.8.8.8")):
        assert is_ssrf_safe("localhost", allow_local=True) is False


def test_canonical_hostname_resolving_to_private_ip_rejected():
    """If 'localhost' resolves to an RFC 1918 IP (DNS hijack), reject it."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("10.0.0.1")):
        assert is_ssrf_safe("localhost", allow_local=True) is False


def test_canonical_hostname_resolving_to_imds_rejected():
    """If 'localhost' resolves to the IMDS address (DNS hijack), reject it."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("169.254.169.254")):
        assert is_ssrf_safe("localhost", allow_local=True) is False


# ---------------------------------------------------------------------------
# 5. Public IPs rejected under allow_local=True — Invariant D
#    allow_local is a loopback-only flag; public targets are not permitted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
    ],
)
def test_public_ip_hostname_rejected_under_allow_local(ip):
    """Public IPs as the hostname are rejected when allow_local=True.

    A public IP literal is not in the canonical loopback allowlist; the raw
    hostname check rejects it before any DNS resolution.
    """
    assert is_ssrf_safe(ip, allow_local=True) is False


# ---------------------------------------------------------------------------
# 6. Invariant E: IMDS / link-local / private / 0.0.0.0 blocked regardless
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # AWS / GCP IMDS
        "169.254.0.1",  # link-local range
    ],
)
def test_imds_still_blocked_when_allow_local(ip):
    """Link-local / metadata addresses remain blocked even with allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("evil.internal", allow_local=True) is False


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "100.64.0.1",  # CGN (RFC 6598)
        "fc00::1",  # IPv6 unique local
        "fe80::1",  # IPv6 link-local
        "0.0.0.0",  # bind-all
    ],
)
def test_private_ranges_still_blocked_when_allow_local(ip):
    """RFC 1918, CGN, and IPv6 private ranges remain blocked when allow_local=True."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        assert is_ssrf_safe("internal.example", allow_local=True) is False


# ---------------------------------------------------------------------------
# 7. is_ssrf_safe default — loopback still blocked (backward compat)
# ---------------------------------------------------------------------------


def test_loopback_blocked_by_default():
    """Without allow_local=True, loopback is blocked (backward-compatible)."""
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        assert is_ssrf_safe("localhost") is False


def test_localhost_blocked_by_default():
    """'localhost' is blocked when allow_local is not set (backward-compatible)."""
    assert is_ssrf_safe("localhost") is False


# ---------------------------------------------------------------------------
# 8. EndpointConfig.allow_local_network field
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
# 9. Endpoint._assert_ssrf_safe_url — integration with allow_local_network
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


def test_assert_ssrf_safe_url_blocks_evil_domain_dns_rebinding():
    """Endpoint with evil.example:11434 DNS-patched to 127.0.0.1 is rejected.

    Even with allow_local_network=True, an external hostname resolving to
    loopback must be rejected.  The raw hostname 'evil.example' is not in the
    canonical loopback allowlist, so it is rejected before DNS resolution.
    """
    ep = _make_endpoint_with_url("http://evil.example:11434/v1", allow_local_network=True)
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        with pytest.raises(PermissionError, match="SSRF guard"):
            ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_blocks_imds_via_loopback_spoofed_name():
    """A hostname other than localhost with allow_local_network=True resolving to IMDS is blocked."""
    ep = _make_endpoint_with_url(
        "http://totally-local.example.com:11434/v1", allow_local_network=True
    )
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("169.254.169.254")):
        with pytest.raises(PermissionError, match="SSRF guard"):
            ep._assert_ssrf_safe_url()


def test_assert_ssrf_safe_url_blocks_public_ip_even_with_allow_local():
    """A URL with a public IP literal is blocked even with allow_local_network=True.

    A public IP literal is not a canonical loopback hostname, so it is rejected
    regardless of the allow_local flag.  A remote Ollama deployment must use a
    different mechanism — this flag is loopback-only.
    """
    ep = _make_endpoint_with_url("http://8.8.8.8:11434/v1", allow_local_network=True)
    with pytest.raises(PermissionError, match="SSRF guard"):
        ep._assert_ssrf_safe_url()


# ---------------------------------------------------------------------------
# 10. Ollama EndpointConfig carries allow_local_network
# ---------------------------------------------------------------------------


def test_ollama_endpoint_config_carries_allow_local_network():
    """Verify an Ollama-like endpoint config correctly sets allow_local_network."""
    config = EndpointConfig(
        name="ollama_chat",
        provider="ollama",
        base_url="http://localhost:11434/v1",
        endpoint="chat/completions",
        auth_type="none",
        allow_local_network=True,
    )
    assert config.allow_local_network is True
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
