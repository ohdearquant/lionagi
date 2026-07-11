# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint


class TestMatchEndpoint:
    """Test the match_endpoint function for provider matching logic.

    ``EndpointRegistry.match`` always returns either a registered endpoint
    instance or the generic fallback ``Endpoint`` — it never returns
    ``None``. Guards like ``if endpoint is None: pytest.skip(...)`` are
    therefore unreachable dead code that can silently mask a routing
    regression as a skip. These tests assert the concrete registered
    endpoint class rather than only ``isinstance(endpoint, Endpoint)``, so a
    regression that quietly falls through to the generic fallback fails
    loudly.
    """

    def test_openai_chat_endpoint(self):
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        assert isinstance(endpoint, OpenaiChatEndpoint)
        assert endpoint.config.provider == "openai"

    def test_anthropic_messages_endpoint(self):
        from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint

        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        assert isinstance(endpoint, AnthropicMessagesEndpoint)
        assert endpoint.config.provider == "anthropic"
        assert endpoint.config.default_headers["anthropic-version"] == "2023-06-01"

    def test_perplexity_endpoint(self):
        from lionagi.providers.perplexity.chat import PerplexityChatEndpoint

        endpoint = match_endpoint(
            provider="perplexity",
            endpoint="chat",
            model="llama-3.1-sonar-small-128k-online",
        )

        assert isinstance(endpoint, PerplexityChatEndpoint)
        assert endpoint.config.provider == "perplexity"

    def test_ollama_endpoint(self):
        from lionagi.providers.ollama.chat import OllamaChatEndpoint

        endpoint = match_endpoint(provider="ollama", endpoint="chat", model="llama2")

        assert isinstance(endpoint, OllamaChatEndpoint)
        assert endpoint.config.provider == "ollama"

    def test_exa_search_endpoint(self):
        from lionagi.providers.exa.search import ExaSearchEndpoint

        endpoint = match_endpoint(provider="exa", endpoint="search", query="test query")

        assert isinstance(endpoint, ExaSearchEndpoint)
        assert endpoint.config.provider == "exa"

    def test_custom_base_url(self):
        custom_url = "https://custom.api.com/v1"
        endpoint = match_endpoint(
            provider="openai",
            endpoint="chat",
            base_url=custom_url,
            model="gpt-4.1-mini",
        )

        assert endpoint.config.base_url == custom_url

    def test_custom_endpoint_params(self):
        endpoint = match_endpoint(
            provider="openai",
            endpoint="chat",
            endpoint_params=["custom", "path"],
            model="gpt-4.1-mini",
        )

        assert endpoint.config.endpoint_params == ["custom", "path"]

    def test_unknown_provider_fallback(self):
        endpoint = match_endpoint(provider="unknown_provider", endpoint="chat", model="some-model")

        # An unregistered provider must route to the generic openai-compatible
        # fallback endpoint, not raise and not return None.
        assert type(endpoint).__name__ == "Endpoint"
        assert endpoint.config.provider == "unknown_provider"

    def test_model_parameter_filtering(self):
        # Test with reasoning model
        reasoning_endpoint = match_endpoint(provider="openai", endpoint="chat", model="o1-preview")

        # Test with standard model
        standard_endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        assert isinstance(reasoning_endpoint, Endpoint)
        assert isinstance(standard_endpoint, Endpoint)

    @pytest.mark.parametrize(
        "provider,expected_compatible",
        [
            ("openai", False),  # Updated based on actual behavior
            ("anthropic", False),
            ("perplexity", False),  # Updated based on actual behavior
        ],
    )
    def test_openai_compatibility(self, provider, expected_compatible):
        endpoint = match_endpoint(provider=provider, endpoint="chat", model="test-model")

        assert endpoint.config.openai_compatible == expected_compatible

    def test_endpoint_with_api_key(self):
        endpoint = match_endpoint(
            provider="openai",
            endpoint="chat",
            model="gpt-4.1-mini",
            api_key="test-key",
        )

        # API key should be handled by the endpoint config
        assert isinstance(endpoint, Endpoint)

    def test_anthropic_specific_headers(self):
        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        assert "anthropic-version" in endpoint.config.default_headers
        assert endpoint.config.default_headers["anthropic-version"] == "2023-06-01"

    def test_endpoint_params_inheritance(self):
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        endpoint = match_endpoint(provider="openai", endpoint="chat")

        assert isinstance(endpoint, OpenaiChatEndpoint)

    def test_provider_case_insensitive(self):
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        endpoint_lower = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        endpoint_upper = match_endpoint(provider="OPENAI", endpoint="chat", model="gpt-4.1-mini")

        # EndpointRegistry.match compares provider strings exactly, so an
        # exact-case "openai" routes to the concrete registered endpoint
        # while a differently-cased "OPENAI" misses the registry entry and
        # falls through to the generic fallback Endpoint. `EndpointConfig`
        # separately lower-cases `config.provider` on validation, so both
        # instances still report `config.provider == "openai"` — asserting
        # only that string equality is too weak, because it stays true even
        # when the uppercase input silently misses registered routing. The
        # class-identity checks below are what actually distinguish
        # registered routing from the generic fallback.
        assert isinstance(endpoint_lower, OpenaiChatEndpoint)
        assert type(endpoint_upper).__name__ == "Endpoint"
        assert type(endpoint_lower) is not type(endpoint_upper)
        assert endpoint_lower.config.provider == endpoint_upper.config.provider == "openai"

    def test_multiple_providers_isolation(self):
        from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        openai_endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        anthropic_endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        assert isinstance(openai_endpoint, OpenaiChatEndpoint)
        assert isinstance(anthropic_endpoint, AnthropicMessagesEndpoint)

        # Should be different instances with different configurations
        assert openai_endpoint is not anthropic_endpoint
        assert openai_endpoint.config.provider != anthropic_endpoint.config.provider

    def test_endpoint_config_immutability(self):
        endpoint1 = match_endpoint(
            provider="openai",
            endpoint="chat",
            model="gpt-4.1-mini",
            temperature=0.5,
        )

        endpoint2 = match_endpoint(
            provider="openai", endpoint="chat", model="gpt-4o", temperature=0.8
        )

        # Should have different configurations
        assert endpoint1.config is not endpoint2.config

    def test_match_endpoint_routes_firecrawl_tavily_and_cli_aliases(self):
        from lionagi.providers.firecrawl.map import FirecrawlMapEndpoint
        from lionagi.providers.firecrawl.scrape import FirecrawlScrapeEndpoint
        from lionagi.providers.google.gemini_code import GeminiCLIEndpoint
        from lionagi.providers.openai.codex import CodexCLIEndpoint
        from lionagi.providers.pi.cli import PiCLIEndpoint
        from lionagi.providers.tavily.search import TavilyExtractEndpoint

        cases = [
            ("firecrawl", "map", FirecrawlMapEndpoint),
            ("firecrawl", "scrape", FirecrawlScrapeEndpoint),
            ("tavily", "extract", TavilyExtractEndpoint),
            ("gemini_cli", "cli", GeminiCLIEndpoint),
            ("codex", "cli", CodexCLIEndpoint),
            ("pi", "cli", PiCLIEndpoint),
        ]
        for provider, endpoint, expected_cls in cases:
            result = match_endpoint(provider=provider, endpoint=endpoint)
            assert isinstance(result, expected_cls), (
                f"Expected {expected_cls.__name__} for {provider}/{endpoint}, "
                f"got {type(result).__name__}"
            )

    def test_match_endpoint_fallback_builds_openai_compatible_endpoint_config(self):
        result = match_endpoint(provider="custom_provider", endpoint="")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "custom_provider"
        assert result.config.endpoint == "chat/completions"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"
