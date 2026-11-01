# SPDX-License-Identifier: Apache-2.0

"""Built-in provider manifest: the hub's providers as immutable declarations.

``get_provider_manifest()`` describes every built-in provider and endpoint as a
:class:`~lionagi.service.connections.provider_catalog.ProviderManifest`. Importing this module
loads no endpoint implementation, and neither does calling the function: it reads the
provider ``_config`` modules for keys, aliases, settings and context-window tables, and every
endpoint is represented by a factory. A factory imports its implementation module only when it
is called, and returns the endpoint class.

The module-level declaration table below is the one explicit, ordered statement of which
endpoints exist. For each endpoint it names the ``_config`` enum member and the implementation
class; everything else is read from the enum, so no provider data is repeated here. Providers
appear in the order they first register, and endpoints within a provider in their registration
order, so a provider's default endpoint is its first one.
"""

from __future__ import annotations

import importlib
from functools import cache, partial
from typing import Any, NamedTuple

from lionagi.service.connections.provider_catalog import (
    DependencyRequirement,
    EndpointDeclaration,
    ProviderFragment,
    ProviderManifest,
)

__all__ = ("get_provider_manifest",)

_PROVIDERS = "lionhub.providers"
_SOURCE = "lionhub.providers"
_CONTRACT_VERSION = 1

_OLLAMA = (
    DependencyRequirement(
        distribution="ollama",
        import_roots=("ollama",),
        version_constraint=">=0.6.1",
        install_hint="pip install lionagi[ollama]",
    ),
)
_AG2 = (
    DependencyRequirement(
        distribution="ag2",
        import_roots=("autogen",),
        version_constraint=">=0.13.0",
        install_hint="pip install lionagi[ag2]",
    ),
)


class _Endpoint(NamedTuple):
    """One built-in endpoint: its enum member, its ``"module:Class"`` and what it requires."""

    member: str
    target: str
    requirements: tuple[DependencyRequirement, ...] = ()


class _Provider(NamedTuple):
    """One built-in provider: where its configuration lives and the endpoints it registers."""

    package: str
    configs: str
    windows: str | None
    endpoints: tuple[_Endpoint, ...]


# Order matters: providers by first registration, endpoints by registration within a provider.
# Each ``target`` is a module under ``lionhub.providers`` and its implementation class.
_DECLARATIONS: tuple[_Provider, ...] = (
    _Provider(
        "openai",
        "OpenAIConfigs",
        "CONTEXT_WINDOWS",
        (
            _Endpoint("CHAT", "openai.chat:OpenaiChatEndpoint"),
            _Endpoint("AUDIO_SPEECH", "openai.audio:OpenaiAudioSpeechEndpoint"),
            _Endpoint("AUDIO_TRANSCRIPTION", "openai.audio:OpenaiAudioTranscriptionEndpoint"),
            _Endpoint("BATCH", "openai.batch:OpenaiBatchEndpoint"),
            _Endpoint("IMAGE_GENERATION", "openai.images:OpenaiImageGenerationEndpoint"),
            _Endpoint("IMAGE_EDIT", "openai.images:OpenaiImageEditEndpoint"),
            _Endpoint("EMBED", "openai.embed:OpenaiEmbedEndpoint"),
            _Endpoint("RESPONSE", "openai.response:OpenaiResponseEndpoint"),
        ),
    ),
    _Provider(
        "openai",
        "CodexConfigs",
        "CODEX_CONTEXT_WINDOWS",
        (_Endpoint("CLI", "openai.codex:CodexCLIEndpoint"),),
    ),
    _Provider(
        "anthropic",
        "AnthropicConfigs",
        "ANTHROPIC_CONTEXT_WINDOWS",
        (_Endpoint("MESSAGES", "anthropic.messages:AnthropicMessagesEndpoint"),),
    ),
    _Provider(
        "anthropic",
        "ClaudeCodeConfigs",
        "CLAUDE_CODE_CONTEXT_WINDOWS",
        (_Endpoint("CLI", "anthropic.claude_code:ClaudeCodeCLIEndpoint"),),
    ),
    _Provider(
        "ollama",
        "OllamaConfigs",
        None,
        (
            _Endpoint("CHAT", "ollama.chat:OllamaChatEndpoint", _OLLAMA),
            _Endpoint("EMBED", "ollama.embed:OllamaEmbedEndpoint", _OLLAMA),
            _Endpoint("GENERATE", "ollama.generate:OllamaGenerateEndpoint", _OLLAMA),
        ),
    ),
    _Provider(
        "lattice",
        "LatticeConfigs",
        None,
        (
            _Endpoint("CHAT", "lattice.chat:LatticeChatEndpoint"),
            _Endpoint("EMBED", "lattice.embed:LatticeEmbedEndpoint"),
        ),
    ),
    _Provider(
        "tavily",
        "TavilyConfigs",
        None,
        (
            _Endpoint("SEARCH", "tavily.search:TavilySearchEndpoint"),
            _Endpoint("EXTRACT", "tavily.search:TavilyExtractEndpoint"),
        ),
    ),
    _Provider(
        "exa",
        "ExaConfigs",
        None,
        (
            _Endpoint("SEARCH", "exa.search:ExaSearchEndpoint"),
            _Endpoint("CONTENTS", "exa.contents:ExaContentsEndpoint"),
            _Endpoint("FIND_SIMILAR", "exa.find_similar:ExaFindSimilarEndpoint"),
        ),
    ),
    _Provider(
        "firecrawl",
        "FirecrawlConfigs",
        None,
        (
            _Endpoint("SCRAPE", "firecrawl.scrape:FirecrawlScrapeEndpoint"),
            _Endpoint("MAP", "firecrawl.map:FirecrawlMapEndpoint"),
            _Endpoint("CRAWL", "firecrawl.crawl:FirecrawlCrawlEndpoint"),
        ),
    ),
    _Provider(
        "perplexity",
        "PerplexityConfigs",
        "CONTEXT_WINDOWS",
        (_Endpoint("CHAT", "perplexity.chat:PerplexityChatEndpoint"),),
    ),
    _Provider(
        "nvidia_nim",
        "NvidiaNimConfigs",
        "CONTEXT_WINDOWS",
        (
            _Endpoint("CHAT", "nvidia_nim.chat:NvidiaNimChatEndpoint"),
            _Endpoint("EMBED", "nvidia_nim.embed:NvidiaNimEmbedEndpoint"),
        ),
    ),
    _Provider(
        "deepseek",
        "DeepSeekConfigs",
        "CONTEXT_WINDOWS",
        (_Endpoint("CHAT", "deepseek.chat:DeepseekChatEndpoint"),),
    ),
    _Provider(
        "google",
        "GeminiChatConfigs",
        "GEMINI_CONTEXT_WINDOWS",
        (_Endpoint("CHAT", "google.chat:GeminiChatEndpoint"),),
    ),
    _Provider(
        "google",
        "GeminiCodeConfigs",
        "GEMINI_CODE_CONTEXT_WINDOWS",
        (_Endpoint("CLI", "google.gemini_code:GeminiCLIEndpoint"),),
    ),
    _Provider(
        "groq",
        "GroqConfigs",
        "CONTEXT_WINDOWS",
        (
            _Endpoint("CHAT", "groq.chat:GroqChatEndpoint"),
            _Endpoint(
                "AUDIO_TRANSCRIPTION", "groq.audio_transcription:GroqAudioTranscriptionEndpoint"
            ),
        ),
    ),
    _Provider(
        "pi",
        "PiConfigs",
        "CONTEXT_WINDOWS",
        (_Endpoint("CLI", "pi.cli:PiCLIEndpoint"),),
    ),
    _Provider(
        "openrouter",
        "OpenRouterConfigs",
        "CONTEXT_WINDOWS",
        (_Endpoint("CHAT", "openrouter.chat:OpenRouterEndpoint"),),
    ),
    _Provider(
        "ag2",
        "AG2Configs",
        None,
        (
            _Endpoint("GROUP_CHAT", "ag2.groupchat:AG2GroupChatEndpoint", _AG2),
            _Endpoint("AGENT", "ag2.agent:AG2BetaEndpoint", _AG2),
            _Endpoint("NLIP", "ag2.nlip:AG2NlipEndpoint"),
        ),
    ),
)


def _load_endpoint_class(target: str) -> Any:
    """Import the module of a ``"module:Class"`` target and return the class."""
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def _load_config(package: str) -> Any:
    """Import one provider package's ``_config`` module, which loads no implementation."""
    return importlib.import_module(f"{_PROVIDERS}.{package}._config")


def _declare_endpoint(configs: Any, endpoint: _Endpoint) -> EndpointDeclaration:
    """Declare one endpoint from its enum member and its implementation target."""
    member = configs[endpoint.member]
    target = f"{_PROVIDERS}.{endpoint.target}"
    return EndpointDeclaration(
        key=member.endpoint_path,
        aliases=tuple(member.aliases),
        kind=member.endpoint_type.value,
        factory=partial(_load_endpoint_class, target),
        requirements=endpoint.requirements,
        settings={
            "base_url": member.base_url,
            "auth_type": member.auth_type,
            "content_type": member.content_type,
            "api_key_env": member.api_key_env,
            # The raw request-options entry, unresolved: a LazyType, a class or None.
            "request_options": member.value[3],
        },
        diagnostic_id=target,
    )


def _declare_provider(row: _Provider) -> ProviderFragment:
    """Declare one provider fragment from its declaration row and its ``_config`` module."""
    config = _load_config(row.package)
    configs = getattr(config, row.configs)
    endpoints = tuple(_declare_endpoint(configs, endpoint) for endpoint in row.endpoints)
    first = configs[row.endpoints[0].member]
    return ProviderFragment(
        contract_version=_CONTRACT_VERSION,
        key=first.provider,
        aliases=tuple(first.provider_aliases),
        endpoints=endpoints,
        default_endpoint=endpoints[0].key,
        context_windows=None if row.windows is None else getattr(config, row.windows),
    )


@cache
def get_provider_manifest() -> ProviderManifest:
    """Return the built-in provider manifest, built once; loads no endpoint implementation."""
    return ProviderManifest(
        contract_version=_CONTRACT_VERSION,
        source=_SOURCE,
        fragments=tuple(_declare_provider(row) for row in _DECLARATIONS),
    )
