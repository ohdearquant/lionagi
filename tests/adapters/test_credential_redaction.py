# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for credential redaction in adapter errors.

Security boundary: credentials embedded in error detail values must never
appear in AdapterError string representations, regardless of the URL scheme
or nesting depth of the details dict.

Two attack vectors are covered:

1. Non-whitelisted URL scheme (e.g. ``s3://``, ``ftp://``, ``jdbc://``):
   previously the scheme guard returned the URL unmodified, leaking any
   query-parameter credentials.

2. Nested dict in error details: previously ``_redact_details`` walked only
   the top-level dict, so a value that was itself a dict escaped redaction
   entirely.
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

    def test_non_http_scheme_query_credentials_redacted(self):
        """Attack: non-whitelisted scheme (s3://) with ?token= must be redacted.

        Previously the scheme-whitelist guard returned the URL unmodified,
        leaking query-parameter credentials for any scheme not in
        _CREDENTIAL_SCHEMES (s3, ftp, jdbc, custom, …).
        """
        url = "s3://my-bucket/path?token=supersecret&safe=ok"
        result = _redact_url(url)
        assert "supersecret" not in result, "token value leaked through non-whitelisted scheme"
        assert "token=***" in result
        # Non-sensitive param preserved
        assert "safe=ok" in result

    def test_ftp_scheme_query_credentials_redacted(self):
        """Attack: ftp:// scheme with ?api_key= must be redacted."""
        url = "ftp://files.example.com/data?api_key=privatekeyvalue"
        result = _redact_url(url)
        assert "privatekeyvalue" not in result, "api_key leaked through ftp:// scheme"
        assert "api_key=***" in result

    def test_empty_query_unchanged(self):
        url = "https://example.com/path"
        result = _redact_url(url)
        assert result == url

    def test_no_scheme_string_unchanged(self):
        """Bare strings with no scheme must not be mis-parsed as URLs."""
        value = "just-a-plain-string"
        assert _redact_url(value) == value


class TestAdapterErrorDoesNotLeakCredentials:
    """End-to-end: AdapterError.__str__ must not expose query secrets.

    Attack: Construct AdapterError with a URL detail containing query
    credentials; assert neither the token value nor the api_key value
    appear in the string representation of the error.
    """

    def test_url_with_query_token_not_in_error_str(self):
        """Regression: URL detail containing query credentials must be redacted."""
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

    def test_non_http_scheme_url_credential_not_in_error_str(self):
        """Attack: s3:// URL with ?token= in a detail must not appear in error string.

        This reproduces the bypass where a non-whitelisted scheme caused
        _redact_url to return the URL unmodified, leaking token values into
        logged error messages.
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
        """Attack: secret inside a nested dict detail value must be redacted.

        Previously _redact_details only walked the top-level dict.  A value
        that was itself a dict escaped redaction entirely, leaking any
        sensitive keys it contained.
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
        """A dict stored under a sensitive top-level key must be fully redacted."""
        err = AdapterError(
            "auth error",
            details={"token": {"access": "tok-abc", "refresh": "tok-xyz"}},
        )
        err_str = str(err)
        assert "tok-abc" not in err_str
        assert "tok-xyz" not in err_str


class TestListLeakRegression:
    """Regression: list values under non-sensitive keys must also be redacted.

    A credential URL inside a list element (e.g. ``errors=[{'input': 'postgresql://u:PW@h/db'}]``)
    or a bare list of URL strings under any non-sensitive key must not appear in
    AdapterError string representations.
    """

    def test_credential_url_inside_list_of_dicts_under_non_sensitive_key(self):
        """Attack: errors=[{'input': 'postgresql://u:REALPW@h/db'}] must not leak REALPW.

        Previously the non-sensitive-key branch of _redact_value returned a list
        as-is, so dict elements (and their URL strings) bypassed redaction entirely.
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
        """Attack: a list of bare credential URL strings must have passwords redacted.

        The same non-sensitive-key branch previously skipped list-of-strings,
        so each URL string was returned unmodified.
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


class TestRedactUrlEdgeCases:
    def test_percent_encoded_credentials_in_netloc_redacted(self):
        # user:p%40ss (@ encoded as %40) in netloc — urllib decodes on parse
        url = "https://user:p%40ss@example.com/path"
        result = _redact_url(url)
        assert "p%40ss" not in result
        assert "p@ss" not in result

    def test_percent_encoded_colon_in_password_redacted(self):
        # password contains %3A (encoded colon)
        url = "postgresql://user:p%3Ass@localhost/db"
        result = _redact_url(url)
        assert "p%3Ass" not in result
        assert "p:ss" not in result

    def test_data_uri_not_parsed_as_credential_url(self):
        # data: URIs should not be treated as having a netloc password
        value = "data:text/plain;base64,SGVsbG8gV29ybGQ="
        result = _redact_url(value)
        # data URIs have no password component; result must not corrupt the URI
        assert "SGVsbG8gV29ybGQ=" in result

    def test_very_long_url_no_backtracking_hang(self):
        # 9000-char URL with no sensitive params — must return quickly, not hang
        import time

        long_path = "/path/" + "x" * 8000
        url = f"https://example.com{long_path}?safe=value"
        start = time.monotonic()
        result = _redact_url(url)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"_redact_url took {elapsed:.2f}s on a long URL"
        assert "safe=value" in result

    def test_very_long_url_with_sensitive_param_redacted(self):
        long_path = "/path/" + "a" * 7000
        url = f"https://example.com{long_path}?token=mysecret&ok=1"
        result = _redact_url(url)
        assert "mysecret" not in result
        assert "token=***" in result
