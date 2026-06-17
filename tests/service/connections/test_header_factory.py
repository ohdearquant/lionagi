# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import SecretStr

from lionagi.service.connections.header_factory import HeaderFactory


class TestHeaderFactory:
    def test_bearer_auth_headers(self):
        headers = HeaderFactory.get_header(auth_type="bearer", api_key="test-key")

        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"

    def test_x_api_key_headers(self):
        headers = HeaderFactory.get_header(auth_type="x-api-key", api_key="test-key")

        assert headers["x-api-key"] == "test-key"
        assert headers["Content-Type"] == "application/json"

    def test_no_auth_headers(self):
        headers = HeaderFactory.get_header(auth_type="none")

        assert "Authorization" not in headers
        assert "x-api-key" not in headers
        assert headers["Content-Type"] == "application/json"

    def test_custom_content_type(self):
        headers = HeaderFactory.get_header(
            auth_type="bearer",
            api_key="test-key",
            content_type="application/xml",
        )

        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/xml"

    def test_secret_str_api_key(self):
        secret_key = SecretStr("secret-key")
        headers = HeaderFactory.get_header(auth_type="bearer", api_key=secret_key)

        assert headers["Authorization"] == "Bearer secret-key"
        assert headers["Content-Type"] == "application/json"

    def test_missing_api_key_with_auth(self):
        with pytest.raises(ValueError, match="API key is required for authentication"):
            HeaderFactory.get_header(auth_type="bearer", api_key=None)

    def test_unsupported_auth_type(self):
        with pytest.raises(ValueError, match="Unsupported auth type"):
            HeaderFactory.get_header(auth_type="unsupported", api_key="test-key")

    def test_get_content_type_header(self):
        headers = HeaderFactory.get_content_type_header()
        assert headers == {"Content-Type": "application/json"}

        headers = HeaderFactory.get_content_type_header("text/plain")
        assert headers == {"Content-Type": "text/plain"}

    def test_get_bearer_auth_header(self):
        headers = HeaderFactory.get_bearer_auth_header("test-key")
        assert headers == {"Authorization": "Bearer test-key"}

    def test_get_x_api_key_header(self):
        headers = HeaderFactory.get_x_api_key_header("test-key")
        assert headers == {"x-api-key": "test-key"}

    def test_default_headers_merge(self):
        default_headers = {"Custom-Header": "custom-value"}
        headers = HeaderFactory.get_header(
            auth_type="bearer",
            api_key="test-key",
            default_headers=default_headers,
        )

        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"
        # Note: The current implementation doesn't merge default_headers
        # This test documents the current behavior
