# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for InstructionContent image URL validation: rejects dangerous schemes, null bytes, and SSRF targets."""

from __future__ import annotations

import pytest

from lionagi.protocols.messages.instruction import InstructionContent


def _render(url: str) -> None:
    """Drive the full public path: construct InstructionContent, then render."""
    content = InstructionContent(instruction="test", images=[url], image_detail="auto")
    # .rendered triggers _format_image_content → _format_image_item for each URL.
    _ = content.rendered


class TestImageUrlAttackRejection:
    """Dangerous image URLs must be rejected before provider payload assembly."""

    def test_file_scheme_rejected(self):
        """file:// URLs enable local file read — must be rejected."""
        with pytest.raises(ValueError, match="http|https|scheme"):
            _render("file:///etc/passwd")

    def test_javascript_scheme_rejected(self):
        """javascript: URLs enable XSS — must be rejected."""
        with pytest.raises(ValueError, match="http|https|scheme"):
            _render("javascript:alert(1)")

    def test_null_byte_in_url_rejected(self):
        """Null bytes enable path-truncation attacks — must be rejected."""
        with pytest.raises(ValueError, match="null byte"):
            _render("https://example.com/image\x00.png")

    def test_percent_encoded_null_byte_rejected(self):
        """Percent-encoded null byte — path truncation variant — must be rejected."""
        with pytest.raises(ValueError, match="null byte|%00"):
            _render("https://example.com/image%00.png")

    def test_missing_domain_rejected(self):
        """http:// without a domain (SSRF / redirect abuse) — must be rejected."""
        with pytest.raises(ValueError, match="domain|netloc"):
            _render("http:///image.png")

    def test_data_html_rejected(self):
        """data:text/html is NOT an image — must be rejected."""
        with pytest.raises(ValueError, match="data:image"):
            _render("data:text/html,<script>alert(1)</script>")

    def test_data_svg_rejected(self):
        """data:image/svg+xml without base64 is not the allowed form."""
        with pytest.raises(ValueError, match="data:image"):
            _render("data:image/svg+xml,<svg><script>alert(1)</script></svg>")

    def test_data_svg_base64_rejected(self):
        """base64 SVG must also be rejected — SVG can carry active content, so
        only a bitmap MIME allowlist (png/jpeg/gif/webp) is accepted."""
        import base64

        payload = base64.b64encode(b"<svg><script>alert(1)</script></svg>").decode()
        with pytest.raises(ValueError, match="data:image"):
            _render(f"data:image/svg+xml;base64,{payload}")

    def test_cloud_metadata_endpoint_rejected(self):
        """The cloud metadata endpoint (169.254.169.254) must be blocked (SSRF)."""
        with pytest.raises(ValueError, match="SSRF|not allowed|private"):
            _render("http://169.254.169.254/latest/meta-data/")

    def test_loopback_rejected(self):
        """Loopback hosts must be blocked (SSRF)."""
        with pytest.raises(ValueError, match="SSRF|not allowed|private"):
            _render("http://127.0.0.1:8080/image.png")

    def test_private_range_rejected(self):
        """Private-range IPs must be blocked (SSRF)."""
        with pytest.raises(ValueError, match="SSRF|not allowed|private"):
            _render("http://192.168.1.1/image.png")


class TestImageUrlSafeInputs:
    """Well-formed image URLs must pass through unchanged."""

    def test_https_url_accepted(self, monkeypatch):
        """Standard HTTPS image URL with a public host must be accepted; is_ssrf_safe is stubbed to avoid DNS."""
        monkeypatch.setattr("lionagi.protocols.messages.validators.is_ssrf_safe", lambda host: True)
        content = InstructionContent(
            instruction="test",
            images=["https://example.com/photo.jpg"],
            image_detail="auto",
        )
        rendered = content.rendered
        assert isinstance(rendered, list)
        item = rendered[1]
        assert item["image_url"]["url"] == "https://example.com/photo.jpg"

    def test_http_url_accepted(self, monkeypatch):
        """Standard HTTP image URL with a public host must be accepted."""
        monkeypatch.setattr("lionagi.protocols.messages.validators.is_ssrf_safe", lambda host: True)
        content = InstructionContent(
            instruction="test",
            images=["http://example.com/photo.png"],
            image_detail="low",
        )
        rendered = content.rendered
        assert rendered[1]["image_url"]["url"] == "http://example.com/photo.png"

    def test_valid_data_image_uri_accepted(self):
        """Proper data:image/*;base64,… URI must be accepted."""
        b64 = "iVBORw0KGgo="  # minimal fake PNG base64
        uri = f"data:image/png;base64,{b64}"
        content = InstructionContent(
            instruction="test",
            images=[uri],
            image_detail="high",
        )
        rendered = content.rendered
        assert rendered[1]["image_url"]["url"] == uri

    def test_raw_base64_wrapped_as_data_uri(self):
        """Raw base64 (no scheme) is wrapped as data:image/jpeg;base64,…"""
        raw_b64 = "iVBORw0KGgo="
        content = InstructionContent(
            instruction="test",
            images=[raw_b64],
            image_detail="auto",
        )
        rendered = content.rendered
        url = rendered[1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert raw_b64 in url
