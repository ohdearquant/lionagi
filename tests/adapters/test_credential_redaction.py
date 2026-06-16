# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for credential redaction in adapter errors.

Covers: non-whitelisted URL schemes leaking query credentials; nested dicts
escaping redaction in _redact_details.
"""

from __future__ import annotations

import pytest

from lionagi.adapters._base import AdapterError, _redact_url


class TestRedactUrlQueryCredentials:
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

    def test_non_http_scheme_query_credentials_redacted(self):
        """Non-whitelisted scheme (s3://) with ?token= must be redacted.

        Previously the scheme-whitelist guard returned the URL unmodified.
        """
        url = "s3://my-bucket/path?token=supersecret&safe=ok"
        result = _redact_url(url)
        assert "supersecret" not in result, "token value leaked through non-whitelisted scheme"
        assert "token=***" in result
        # Non-sensitive param preserved
        assert "safe=ok" in result

    def test_ftp_scheme_query_credentials_redacted(self):
        url = "ftp://files.example.com/data?api_key=privatekeyvalue"
        result = _redact_url(url)
        assert "privatekeyvalue" not in result, "api_key leaked through ftp:// scheme"
        assert "api_key=***" in result

    def test_empty_query_unchanged(self):
        url = "https://example.com/path"
        result = _redact_url(url)
        assert result == url

    def test_no_scheme_string_unchanged(self):
        value = "just-a-plain-string"
        assert _redact_url(value) == value


class TestAdapterErrorDoesNotLeakCredentials:
    """AdapterError.__str__ must not expose credentials in detail values."""

    def test_url_with_query_token_not_in_error_str(self):
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
        """Non-secret URL parts (domain/path) remain visible for debugging."""
        url = "https://api.example.com/v1/endpoint?api_key=secret123&page=2"
        err = AdapterError("error", details={"url": url})
        err_str = str(err)
        # The domain and path should still appear so the error is useful
        assert "api.example.com" in err_str
        # The secret must not
        assert "secret123" not in err_str

    def test_non_http_scheme_url_credential_not_in_error_str(self):
        """s3:// URL with ?token= in a detail must not appear in error string.

        Previously non-whitelisted schemes caused _redact_url to return the URL
        unmodified, leaking token values into logged error messages.
        """
        err = AdapterError(
            "storage error",
            details={"url": "s3://my-bucket/key?token=s3secrettoken123&region=us-east-1"},
        )
        err_str = str(err)
        assert "s3secrettoken123" not in err_str, (
            "token value leaked through s3:// URL in AdapterError string"
        )

    def test_nested_dict_detail_credentials_redacted(self):
        """Secret inside a nested dict detail must be redacted.

        Previously _redact_details only walked the top-level dict.
        """
        err = AdapterError(
            "config error",
            details={
                "connection": {
                    "url": "postgresql://user:pass@host/db",
                    "secret": "db-secret-value",
                    "host": "db.example.com",
                }
            },
        )
        err_str = str(err)
        assert "pass" not in err_str, "nested netloc password leaked"
        assert "db-secret-value" not in err_str, "nested 'secret' key value leaked"
        # Non-sensitive nested key (host) may or may not appear; we only
        # require that credentials are absent.

    def test_nested_dict_under_sensitive_key_all_values_redacted(self):
        err = AdapterError(
            "auth error",
            details={"token": {"access": "tok-abc", "refresh": "tok-xyz"}},
        )
        err_str = str(err)
        assert "tok-abc" not in err_str
        assert "tok-xyz" not in err_str


class TestListLeakRegression:
    """List values under non-sensitive keys must also be redacted."""

    def test_credential_url_inside_list_of_dicts_under_non_sensitive_key(self):
        """errors=[{'input': 'postgresql://u:REALPW@h/db'}] must not leak REALPW.

        Previously the non-sensitive-key branch of _redact_value returned a list as-is.
        """
        from lionagi.adapters._base import AdapterValidationError

        err = AdapterValidationError(
            adapter="x",
            errors=[{"input": "postgresql://u:REALPW@h/db"}],
        )
        err_str = str(err)
        assert "REALPW" not in err_str, (
            f"netloc password leaked from list[dict] under non-sensitive key: {err_str!r}"
        )

    def test_bare_credential_url_strings_in_list_under_non_sensitive_key(self):
        """A list of bare credential URL strings must have passwords redacted.

        Previously the non-sensitive-key branch skipped list-of-strings.
        """
        err = AdapterError(
            "connection error",
            details={
                "candidates": [
                    "postgresql://alice:SECRETPW@primary/db",
                    "postgresql://alice:SECRETPW@replica/db",
                ]
            },
        )
        err_str = str(err)
        assert "SECRETPW" not in err_str, (
            f"netloc password leaked from list of URL strings under non-sensitive key: {err_str!r}"
        )
