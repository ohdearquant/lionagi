# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Image URL validation for the message layer."""

from urllib.parse import urlparse

from lionagi.ln import is_ssrf_safe

__all__ = ("validate_image_url",)


def validate_image_url(url: str) -> None:
    """Validate image URL; raises ValueError for null bytes, non-http(s) schemes, missing netloc, or SSRF-prone hosts."""
    if not url or not isinstance(url, str):
        raise ValueError(f"Image URL must be non-empty string, got: {type(url).__name__}")

    if "\x00" in url:
        raise ValueError("Image URL contains null byte - potential path truncation attack")
    if "%00" in url.lower():
        raise ValueError(
            "Image URL contains percent-encoded null byte (%00) - potential path truncation attack"
        )

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Malformed image URL '{url}': {e}") from e

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Image URL must use http:// or https:// scheme, got: {parsed.scheme}://"
            f"\nRejected URL: {url}"
            f"\nReason: Disallowed schemes (file://, javascript://, data://) pose "
            f"security risks (local file access, XSS, DoS)"
        )

    if not parsed.netloc:
        raise ValueError(f"Image URL missing domain: {url}")

    # SSRF guard: reject hosts resolving to private/loopback/link-local ranges
    # (e.g. cloud metadata 169.254.169.254). Fail-closed: unresolvable hosts too.
    if not is_ssrf_safe(parsed.hostname or ""):
        raise ValueError(
            f"Image URL host {parsed.hostname!r} is not allowed: it resolves to a "
            f"private, loopback, or link-local address (SSRF risk), or could not be "
            f"resolved.\nRejected URL: {url}"
        )
