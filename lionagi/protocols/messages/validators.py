# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Validation helpers for message layer."""

from urllib.parse import urlparse

__all__ = ("validate_image_url",)


def validate_image_url(url: str) -> None:
    """Validate image URL to prevent security vulnerabilities.

    Security checks:
        - Reject null bytes (path truncation attacks)
        - Reject file:// URLs (local file access)
        - Reject javascript: URLs (XSS attacks)
        - Reject data:// URLs (DoS via large embedded images)
        - Only allow http:// and https:// schemes
        - Validate URL format

    Raises:
        ValueError: If URL is invalid or uses disallowed scheme.
    """
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
