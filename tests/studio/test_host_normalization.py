# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One normalizer for every reader of ``LIONAGI_STUDIO_HOST``.

Five readers used to parse the same variable their own way: the Host guard added
the value as written, the startup warning stripped whitespace and brackets without
lowercasing, and the base-URL builder returned the raw value when it could not
parse it. Each spelling one reader understood and another did not was a separate
bug, so the parsing lives in one place and every reader calls it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lionagi.studio._host import normalize_host


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Unspecified spellings
        ("0.0.0.0", "0.0.0.0"),
        ("::", "::"),
        ("[::]", "::"),
        ("0:0:0:0:0:0:0:0", "::"),
        ("[0:0:0:0:0:0:0:0]", "::"),
        ("::0", "::"),
        ("0000::0000", "::"),
        # An IPv4-mapped address is the IPv4 address in another spelling
        ("::ffff:0.0.0.0", "0.0.0.0"),
        ("[::ffff:0.0.0.0]", "0.0.0.0"),
        ("::ffff:0:0", "0.0.0.0"),
        ("[::ffff:0:0]", "0.0.0.0"),
        ("0:0:0:0:0:ffff:0:0", "0.0.0.0"),
        # Loopback spellings
        ("127.0.0.1", "127.0.0.1"),
        ("[127.0.0.1]", "127.0.0.1"),
        ("127.0.0.2", "127.0.0.2"),
        ("::1", "::1"),
        ("[::1]", "::1"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("[0:0:0:0:0:0:0:1]", "::1"),
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("[::ffff:127.0.0.1]", "127.0.0.1"),
        ("::ffff:7f00:1", "127.0.0.1"),
        ("[::ffff:7f00:1]", "127.0.0.1"),
        ("localhost", "localhost"),
        ("LOCALHOST", "localhost"),
        ("[localhost]", "localhost"),
        ("sub.localhost", "sub.localhost"),
        # IPv4-mapped unicast
        ("::ffff:192.168.1.5", "192.168.1.5"),
        ("[::ffff:192.168.1.5]", "192.168.1.5"),
        ("::ffff:c0a8:105", "192.168.1.5"),
        ("[::ffff:c0a8:105]", "192.168.1.5"),
        # Unicast IPv4 / IPv6: canonical, non-canonical, bracketed, padded
        ("192.168.1.5", "192.168.1.5"),
        (" 192.168.1.5 ", "192.168.1.5"),
        ("[192.168.1.5]", "192.168.1.5"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        (" 2001:db8::1 ", "2001:db8::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("2001:0db8::1", "2001:db8::1"),
        ("[2001:0db8::1]", "2001:db8::1"),
        ("2001:db8:0:0:0:0:0:1", "2001:db8::1"),
        # Link-local, and a zone that names a local interface
        ("fe80::1", "fe80::1"),
        ("[fe80::1]", "fe80::1"),
        ("fe80:0000::1", "fe80::1"),
        ("fe80::1%eth0", "fe80::1%eth0"),
        ("[fe80::1%eth0]", "fe80::1%eth0"),
        ("fe80::1%en0", "fe80::1%en0"),
        # Blanks, names, and values that carry a port
        ("", ""),
        ("   ", ""),
        ("[]", ""),
        ("[ ]", ""),
        ("studio.example.com", "studio.example.com"),
        ("STUDIO.EXAMPLE.COM", "studio.example.com"),
        (" studio.example.com ", "studio.example.com"),
        ("[studio.example.com]", "studio.example.com"),
        ("127.0.0.1:8765", "127.0.0.1:8765"),
        ("[::1]:8765", "[::1]:8765"),
        ("::1:8765", "::1:8765"),
    ],
)
def test_the_normalizer_returns_the_canonical_form(raw, expected):
    assert normalize_host(raw) == expected


def test_the_normalizer_is_idempotent():
    for raw in ("[2001:0db8::1]", " 192.168.1.5 ", "::ffff:192.168.1.5", "LOCALHOST", "[]"):
        once = normalize_host(raw)
        assert normalize_host(once) == once, raw


def test_the_normalizer_module_needs_no_web_stack():
    """The CLI imports the normalizer, so it must not drag FastAPI or Starlette
    into the CLI's import path."""
    script = (
        "import sys; import lionagi.studio._host as m; "
        "print('fastapi' in sys.modules, 'starlette' in sys.modules, m.normalize_host('LOCALHOST'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False False localhost", out.stdout
