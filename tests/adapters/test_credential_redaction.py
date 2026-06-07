# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for credential redaction in adapter errors.

Issue (High, security): _redact_url only masked the URL netloc password
(user:pass@host), but left query-string credentials (e.g. ?token=secret&
api_key=k) in plain text inside AdapterError messages, which may be logged
or displayed.

Fix: _redact_url now also redacts query parameters whose key matches
_SENSITIVE_QUERY_PARAMS.
"""

from __future__ import annotations

import pytest

from lionagi.adapters._base import AdapterError, _redact_url


class TestRedactUrlQueryCredentials:
    """Verify _redact_url strips sensitive query parameters."""

    def test_token_in_query_is_redacted(self):
        url = "https://example.com/cb?token=secret123"
        result = _redact_url(url)
        assert "secret123" not in result
        assert "token=***" in result

    def test_api_key_in_query_is_redacted(self):
        url = "https://api.example.com/v1?api_key=my-private-key"
        result = _redact_url(url)
        assert "my-private-key" not in result
        assert "api_key=***" in result

    def test_multiple_secrets_in_query_all_redacted(self):
        url = "https://example.com/cb?token=secret&api_key=k&safe=value"
        result = _redact_url(url)
        assert "secret" not in result
        assert "api_key=***" in result
        assert "token=***" in result
        # Non-secret params preserved
        assert "safe=value" in result

    def test_password_in_netloc_still_redacted(self):
        url = "postgresql://user:mypassword@localhost:5432/db"
        result = _redact_url(url)
        assert "mypassword" not in result
        assert "user:***@" in result

    def test_both_netloc_and_query_redacted(self):
        url = "https://user:pass@example.com/path?token=abc&safe=ok"
        result = _redact_url(url)
        assert "pass" not in result
        assert "abc" not in result
        assert "safe=ok" in result

    def test_non_sensitive_query_params_preserved(self):
        url = "https://example.com/api?page=1&limit=10&format=json"
        result = _redact_url(url)
        # No sensitive keys → URL unchanged
        assert result == url

    def test_non_credentialed_scheme_unchanged(self):
        """Schemes not in _CREDENTIAL_SCHEMES must pass through untouched."""
        url = "ftp://example.com?token=secret"
        result = _redact_url(url)
        assert result == url

    def test_empty_query_unchanged(self):
        url = "https://example.com/path"
        result = _redact_url(url)
        assert result == url


class TestAdapterErrorDoesNotLeakCredentials:
    """End-to-end: AdapterError.__str__ must not expose query secrets.

    Attack: Construct AdapterError with a URL detail containing query
    credentials; assert neither the token value nor the api_key value
    appear in the string representation of the error.
    """

    def test_url_with_query_token_not_in_error_str(self):
        """Regression for the exact scenario from the audit finding."""
        err = AdapterError(
            "connection failed",
            details={"url": "https://example.com/cb?token=secret&api_key=k"},
        )
        err_str = str(err)
        assert "secret" not in err_str
        assert "=k" not in err_str  # api_key value not exposed

    def test_dsn_with_password_not_in_error_str(self):
        err = AdapterError(
            "db error",
            details={"dsn": "postgresql://user:supersecret@localhost/db?sslmode=require"},
        )
        assert "supersecret" not in str(err)

    def test_url_with_api_key_not_in_error_str(self):
        url = "https://api.openai.com/v1/chat?api_key=sk-1234567890abcdef"
        err = AdapterError("api error", details={"url": url})
        assert "sk-1234567890abcdef" not in str(err)

    def test_host_and_path_still_visible(self):
        """Diagnostics that are NOT secrets should remain visible for debugging."""
        url = "https://api.example.com/v1/endpoint?api_key=secret123&page=2"
        err = AdapterError("error", details={"url": url})
        err_str = str(err)
        # The domain and path should still appear so the error is useful
        assert "api.example.com" in err_str
        # The secret must not
        assert "secret123" not in err_str
