# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest

from lionagi.service.imodel import iModel


class TestiModelValidationErrors:
    """Tests for validation error handling in iModel."""

    def test_invalid_temperature_type(self):
        """Test iModel with invalid temperature type raises validation error."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        # Temperature validation happens at payload creation
        with pytest.raises(ValueError, match="Invalid payload"):
            imodel.create_api_calling(
                messages=[{"role": "user", "content": "test"}],
                temperature="invalid",
            )

    def test_invalid_max_tokens_negative(self):
        """Test iModel with negative max_tokens."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "test"}], max_tokens=-100
        )
        # Should accept negative value, API will validate
        assert api_call.payload["max_tokens"] == -100

    def test_invalid_messages_structure(self):
        """Test iModel with invalid messages structure."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        # Test with malformed messages
        api_call = imodel.create_api_calling(messages=[{"invalid": "structure"}])
        # Should create payload, validation happens at API level
        assert len(api_call.payload["messages"]) == 1

    def test_empty_content_in_messages(self):
        """Test iModel with empty content in messages."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        api_call = imodel.create_api_calling(messages=[{"role": "user", "content": ""}])
        assert api_call.payload["messages"][0]["content"] == ""

    def test_none_role_in_messages(self):
        """Test iModel with None role raises validation error."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        # None role should fail validation
        with pytest.raises(ValueError, match="Invalid payload"):
            imodel.create_api_calling(messages=[{"role": None, "content": "test"}])

    def test_invalid_model_parameter_type(self):
        """Test iModel with invalid model type raises validation error."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        # Invalid model type should fail validation
        with pytest.raises(ValueError, match="Invalid payload"):
            imodel.create_api_calling(
                messages=[{"role": "user", "content": "test"}], model=123
            )

    def test_very_long_message_content(self):
        """Test iModel with extremely long message content."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        long_content = "x" * 1000000  # 1 million characters
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": long_content}]
        )
        assert len(api_call.payload["messages"][0]["content"]) == 1000000

    def test_special_characters_in_messages(self):
        """Test iModel with special characters and unicode in messages."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        special_content = "Hello 世界 🌍 \n\t\r !@#$%^&*()"
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": special_content}]
        )
        assert api_call.payload["messages"][0]["content"] == special_content
