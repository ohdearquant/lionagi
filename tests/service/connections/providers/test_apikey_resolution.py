"""Parity tests: api_key resolution via EndpointMeta.api_key_env matches the
old per-endpoint __init__ boilerplate, and the Ollama sentinel special-case
in EndpointConfig._validate_api_key is preserved."""

import os
from unittest.mock import MagicMock, patch

import pytest

from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.registry import EndpointMeta, EndpointRegistry, EndpointType

# ---------------------------------------------------------------------------
# Ollama: no key required; auth_type="none"; api_key stays None
# ---------------------------------------------------------------------------


class TestOllamaNoKeyRequired:
    def test_ollama_endpoint_has_no_api_key_env(self):
        from lionagi.providers.ollama._config import OllamaConfigs

        assert getattr(OllamaConfigs, "_API_KEY_ENV", None) is None

    def test_ollama_config_api_key_is_none(self):
        """api_key=None + auth_type=none is valid for Ollama."""
        config = EndpointConfig(
            name="ollama_chat",
            provider="ollama",
            base_url="http://localhost:11434/v1",
            endpoint="chat/completions",
            api_key=None,
            auth_type="none",
            content_type="application/json",
            method="POST",
        )
        assert config._api_key is None
        assert config.auth_type == "none"

    def test_ollama_headers_omit_authorization(self):
        """With auth_type='none', no Authorization or x-api-key header is added."""
        from lionagi.service.connections.header_factory import HeaderFactory

        headers = HeaderFactory.get_header(auth_type="none", api_key=None)
        assert "Authorization" not in headers
        assert "x-api-key" not in headers

    def test_ollama_sentinel_preserved_in_endpoint_config(self):
        """EndpointConfig._validate_api_key must preserve the ollama_key sentinel path
        exactly as at endpoint_config.py lines 71-72:
          if self.provider == 'ollama' and self.api_key == 'ollama_key':
              self._api_key = 'ollama_key'
        """
        config = EndpointConfig(
            name="ollama_chat",
            provider="ollama",
            base_url="http://localhost:11434/v1",
            endpoint="chat/completions",
            api_key="ollama_key",
            auth_type="none",
            content_type="application/json",
            method="POST",
        )
        # The sentinel path sets _api_key = "ollama_key" directly (no env lookup)
        assert config._api_key == "ollama_key"


# ---------------------------------------------------------------------------
# Provider api_key_env metadata declared
# ---------------------------------------------------------------------------


def test_api_key_env_metadata_on_provider_configs():
    """All non-Ollama API providers must declare _API_KEY_ENV; Ollama must not."""
    from lionagi.providers.anthropic._config import AnthropicConfigs
    from lionagi.providers.deepseek._config import DeepSeekConfigs
    from lionagi.providers.exa._config import ExaConfigs
    from lionagi.providers.firecrawl._config import FirecrawlConfigs
    from lionagi.providers.google._config import GeminiChatConfigs
    from lionagi.providers.groq._config import GroqConfigs
    from lionagi.providers.nvidia_nim._config import NvidiaNimConfigs
    from lionagi.providers.ollama._config import OllamaConfigs
    from lionagi.providers.openai._config import OpenAIConfigs
    from lionagi.providers.openrouter._config import OpenRouterConfigs
    from lionagi.providers.perplexity._config import PerplexityConfigs
    from lionagi.providers.tavily._config import TavilyConfigs

    has_key = {
        "openai": (OpenAIConfigs, "OPENAI_API_KEY"),
        "anthropic": (AnthropicConfigs, "ANTHROPIC_API_KEY"),
        "gemini": (GeminiChatConfigs, "GEMINI_API_KEY"),
        "groq": (GroqConfigs, "GROQ_API_KEY"),
        "deepseek": (DeepSeekConfigs, "DEEPSEEK_API_KEY"),
        "openrouter": (OpenRouterConfigs, "OPENROUTER_API_KEY"),
        "nvidia_nim": (NvidiaNimConfigs, "NVIDIA_NIM_API_KEY"),
        "perplexity": (PerplexityConfigs, "PERPLEXITY_API_KEY"),
        "exa": (ExaConfigs, "EXA_API_KEY"),
        "firecrawl": (FirecrawlConfigs, "FIRECRAWL_API_KEY"),
        "tavily": (TavilyConfigs, "TAVILY_API_KEY"),
    }
    for name, (cls, expected_env) in has_key.items():
        actual = getattr(cls, "_API_KEY_ENV", None)
        assert actual == expected_env, (
            f"{name} provider config: _API_KEY_ENV={actual!r}, expected {expected_env!r}"
        )

    # Ollama has no key
    assert getattr(OllamaConfigs, "_API_KEY_ENV", None) is None


# ---------------------------------------------------------------------------
# EndpointMeta.create_config resolves api_key via api_key_env
# ---------------------------------------------------------------------------


def test_endpoint_meta_resolves_api_key_from_settings():
    """EndpointMeta.create_config reads the correct settings attribute."""
    from pydantic import SecretStr

    mock_settings = MagicMock()
    mock_settings.OPENAI_API_KEY = SecretStr("sk-from-settings")

    meta = EndpointMeta(
        provider="openai",
        endpoint="chat/completions",
        endpoint_type=EndpointType.API,
        auth_type="bearer",
        api_key_env="OPENAI_API_KEY",
    )

    with patch("lionagi.config.settings", mock_settings):
        config = meta.create_config()

    assert config._api_key == "sk-from-settings"


def test_endpoint_meta_falls_back_to_dummy_when_setting_is_none():
    """When the settings attribute is None, api_key falls back to dummy-key-for-testing."""
    mock_settings = MagicMock()
    mock_settings.OPENAI_API_KEY = None

    meta = EndpointMeta(
        provider="openai",
        endpoint="chat/completions",
        endpoint_type=EndpointType.API,
        auth_type="bearer",
        api_key_env="OPENAI_API_KEY",
    )

    with patch("lionagi.config.settings", mock_settings):
        config = meta.create_config()

    assert config._api_key == "dummy-key-for-testing"


def test_endpoint_meta_respects_api_key_override():
    """If api_key is passed explicitly as a SecretStr, api_key_env resolution is skipped."""
    from pydantic import SecretStr

    mock_settings = MagicMock()
    mock_settings.OPENAI_API_KEY = SecretStr("sk-should-not-be-used")

    meta = EndpointMeta(
        provider="openai",
        endpoint="chat/completions",
        endpoint_type=EndpointType.API,
        auth_type="bearer",
        api_key_env="OPENAI_API_KEY",
    )

    with patch("lionagi.config.settings", mock_settings):
        # Pass as SecretStr so _validate_api_key uses its dedicated branch directly
        config = meta.create_config(api_key=SecretStr("sk-explicit"))

    # Explicit override wins over api_key_env resolution
    assert config._api_key == "sk-explicit"


# ---------------------------------------------------------------------------
# Header parity: resolved api_key produces correct auth header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth_type,expected_header,expected_prefix",
    [
        ("bearer", "Authorization", "Bearer "),
        ("x-api-key", "x-api-key", ""),
    ],
)
def test_header_parity_for_auth_types(auth_type, expected_header, expected_prefix):
    """api_key resolved via api_key_env produces correct auth headers."""
    from lionagi.service.connections.header_factory import HeaderFactory

    headers = HeaderFactory.get_header(auth_type=auth_type, api_key="test-key-value")
    assert expected_header in headers
    if expected_prefix:
        assert headers[expected_header].startswith(expected_prefix)
    assert "test-key-value" in headers[expected_header]


def test_anthropic_endpoint_uses_x_api_key_header():
    """Anthropic (x-api-key auth_type) endpoint emits x-api-key header, not Authorization."""
    from pydantic import SecretStr

    mock_settings = MagicMock()
    mock_settings.ANTHROPIC_API_KEY = SecretStr("anthro-test-key")

    from lionagi.providers.anthropic._config import AnthropicConfigs

    meta_member = AnthropicConfigs.MESSAGES
    meta_kwargs = meta_member.as_registry_kwargs()
    assert meta_kwargs["api_key_env"] == "ANTHROPIC_API_KEY"
    assert meta_kwargs["auth_type"] == "x-api-key"

    from lionagi.service.connections.registry import EndpointMeta

    meta = EndpointMeta(
        provider=meta_kwargs["provider"],
        endpoint=meta_kwargs["endpoint"],
        endpoint_type=meta_kwargs["endpoint_type"],
        auth_type=meta_kwargs["auth_type"],
        api_key_env=meta_kwargs["api_key_env"],
    )

    with patch("lionagi.config.settings", mock_settings):
        config = meta.create_config()

    assert config._api_key == "anthro-test-key"
