# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest

from lionagi.service.imodel import iModel


class TestiModelProviderSpecificEdgeCases:
    """Tests for provider-specific edge cases."""

    def test_anthropic_without_max_tokens(self):
        """Test Anthropic iModel creation."""
        imodel = iModel(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )
        # Should create successfully, max_tokens required at invoke time
        assert imodel.endpoint.config.provider == "anthropic"

    def test_ollama_special_handling(self):
        """Test Ollama provider special handling."""
        pytest.importorskip("ollama")
        imodel = iModel(
            provider="ollama",
            model="llama2",
            api_key="ollama",  # Special ollama key
        )
        assert imodel.endpoint.config.provider == "ollama"

    def test_claude_code_session_id_initialization(self):
        """Test Claude Code session_id on CLI endpoint."""
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )
        # Set session_id on the CLI endpoint directly
        imodel.endpoint.session_id = "initial-session"
        assert imodel.endpoint.session_id == "initial-session"

    def test_openrouter_model_path_parsing(self):
        """Test OpenRouter model path parsing."""
        imodel = iModel(
            model="openrouter/anthropic/claude-3-opus",
            api_key="test-key",
        )
        # Should parse provider from model path
        assert imodel.endpoint.config.provider == "openrouter"

    def test_mixed_case_provider_names(self):
        """Test provider names with mixed case."""
        imodel = iModel(
            provider="OpenAI",  # Mixed case
            model="gpt-4.1-mini",
            api_key="test-key",
        )
        # Provider should be normalized
        assert imodel.endpoint.config.provider.lower() == "openai"
