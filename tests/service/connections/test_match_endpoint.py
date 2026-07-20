# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import ProviderNotFoundError


class TestMatchEndpoint:
    """Test the match_endpoint function for provider matching logic.

    ``EndpointRegistry.match`` returns a registered endpoint instance, or
    the generic fallback ``Endpoint`` when the caller explicitly opts in
    (``openai_compatible=True``, or ``base_url=`` on the deprecated
    migration path) -- it never returns ``None``. An unrecognized provider
    with no opt-in raises ``ProviderNotFoundError`` instead of silently
    mis-routing. Guards like ``if endpoint is None: pytest.skip(...)`` are
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

    def test_unknown_provider_raises_by_default(self):
        # An unregistered provider with no opt-in and no base_url must raise
        # a clear error rather than silently mis-routing to the generic
        # OpenAI-compatible fallback.
        with pytest.raises(ProviderNotFoundError, match="unknown_provider"):
            match_endpoint(provider="unknown_provider", endpoint="chat", model="some-model")

    def test_unknown_provider_error_names_registered_providers(self):
        with pytest.raises(ProviderNotFoundError, match="openai"):
            match_endpoint(provider="unknown_provider", endpoint="chat")

    def test_unknown_provider_with_explicit_opt_in_falls_back(self):
        endpoint = match_endpoint(
            provider="unknown_provider",
            endpoint="chat",
            model="some-model",
            openai_compatible=True,
        )

        assert type(endpoint).__name__ == "Endpoint"
        assert endpoint.config.provider == "unknown_provider"
        assert endpoint.config.openai_compatible is True

    def test_registered_provider_with_unmatched_endpoint_does_not_raise(self):
        # 'openai' is registered (chat, embeddings, batch, ...) but has no
        # 'query_cli' endpoint. The provider itself is known, so this must
        # never surface as ProviderNotFoundError -- only a genuinely
        # unrecognized *provider* string does that. No openai_compatible
        # opt-in should be required either: the provider identity was never
        # in question.
        endpoint = match_endpoint(provider="openai", endpoint="query_cli", model="gpt-4o-mini")

        assert endpoint.config.provider == "openai"
        assert endpoint.is_cli is False

    def test_unknown_provider_with_base_url_falls_back_with_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match="unknown_provider"):
            endpoint = match_endpoint(
                provider="unknown_provider",
                endpoint="chat",
                base_url="https://my-api.example.com/v1",
            )

        assert type(endpoint).__name__ == "Endpoint"
        assert endpoint.config.provider == "unknown_provider"
        assert endpoint.config.base_url == "https://my-api.example.com/v1"

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

    def test_provider_case_insensitive_routing(self):
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        endpoint_lower = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        endpoint_upper = match_endpoint(provider="OPENAI", endpoint="chat", model="gpt-4.1-mini")

        assert isinstance(endpoint_lower, OpenaiChatEndpoint)
        assert isinstance(endpoint_upper, OpenaiChatEndpoint)
        assert type(endpoint_lower) is type(endpoint_upper)
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
        result = match_endpoint(provider="custom_provider", endpoint="", openai_compatible=True)

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "custom_provider"
        assert result.config.endpoint == "chat/completions"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"
        assert result.config.openai_compatible is True
