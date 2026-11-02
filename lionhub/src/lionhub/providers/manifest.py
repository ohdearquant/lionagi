# SPDX-License-Identifier: Apache-2.0

"""Built-in provider manifest: the hub's providers as immutable declarations.

``get_provider_manifest()`` describes every built-in provider and endpoint as a
:class:`~lionagi.service.connections.provider_catalog.ProviderManifest`. Importing this module
loads no endpoint implementation, and neither does calling the function: it reads the
provider ``_config`` modules for settings and context-window tables, and every endpoint is
represented by a factory. A factory imports its implementation module only when it is called,
and returns the endpoint class.

The module-level declaration table below is the one explicit, ordered statement of which
endpoints exist. For each provider it states the key and aliases, and for each endpoint the
key, aliases and kind, together with the ``_config`` enum member and the implementation class.
Settings and context windows are read from the enum. Where the manifest builds a provider from
its ``_config``, it reads the provider's and endpoints' names from the enum members as well;
the table repeats them so that a provider whose ``_config`` cannot be imported still keeps its
identity, and a test keeps the two in step. Providers appear in the order they first register,
and endpoints within a provider in their registration order, so a provider's default endpoint
is its first one.

A provider whose ``_config`` raises ``ImportError`` does not fail the manifest. Its fragment is
built from its row alone, with no context windows and no settings, and each of its endpoint
factories raises ``ProviderActivationError`` chained from that ``ImportError`` and imports
nothing. Any other exception from a ``_config`` propagates.

The manifest also declares ``context_window_order``, the order in which a catalog composed
from it scans the providers' context-window tables. It is not registration order: where two
tables match the same model name, the provider named first in it decides the window.
"""

from __future__ import annotations

import importlib
from functools import cache, partial
from typing import Any, NamedTuple, NoReturn

from lionagi._provider_errors import ProviderActivationError
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

# The order an unhinted context-window lookup scans the providers' tables in: the first
# provider whose table matches the model wins, so where two tables hold the same model name
# this order, not registration order, decides which window is returned.
_CONTEXT_WINDOW_ORDER = (
    "openai",
    "anthropic",
    "claude_code",
    "codex",
    "deepseek",
    "nvidia_nim",
    "perplexity",
    "gemini_code",
    "pi",
    "groq",
    "gemini",
    "openrouter",
)

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
    """One built-in endpoint: its enum member, its names, its ``"module:Class"`` and requirements.

    ``key``, ``aliases`` and ``kind`` (``"api"`` or ``"agentic"``) are what the enum member
    produces; they let the manifest name the endpoint when the provider's ``_config`` cannot be
    imported.
    """

    member: str
    key: str
    aliases: tuple[str, ...]
    kind: str
    target: str
    requirements: tuple[DependencyRequirement, ...] = ()


class _Provider(NamedTuple):
    """One built-in provider: its names, where its configuration lives and its endpoints.

    ``key`` and ``aliases`` are what the provider's enum members produce; they let the manifest
    name the provider when its ``_config`` cannot be imported.
    """

    package: str
    configs: str
    key: str
    aliases: tuple[str, ...]
    windows: str | None
    endpoints: tuple[_Endpoint, ...]


# Order matters: providers by first registration, endpoints by registration within a provider.
# Each ``target`` is a module under ``lionhub.providers`` and its implementation class.
_DECLARATIONS: tuple[_Provider, ...] = (
    _Provider(
        "openai",
        "OpenAIConfigs",
        "openai",
        (),
        "CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "openai.chat:OpenaiChatEndpoint",
            ),
            _Endpoint(
                "AUDIO_SPEECH",
                "audio/speech",
                ("tts",),
                "api",
                "openai.audio:OpenaiAudioSpeechEndpoint",
            ),
            _Endpoint(
                "AUDIO_TRANSCRIPTION",
                "audio/transcriptions",
                ("stt", "whisper"),
                "api",
                "openai.audio:OpenaiAudioTranscriptionEndpoint",
            ),
            _Endpoint("BATCH", "batches", ("batch",), "api", "openai.batch:OpenaiBatchEndpoint"),
            _Endpoint(
                "IMAGE_GENERATION",
                "images/generations",
                ("dalle", "image"),
                "api",
                "openai.images:OpenaiImageGenerationEndpoint",
            ),
            _Endpoint(
                "IMAGE_EDIT",
                "images/edits",
                ("image_edit",),
                "api",
                "openai.images:OpenaiImageEditEndpoint",
            ),
            _Endpoint("EMBED", "embeddings", ("embed",), "api", "openai.embed:OpenaiEmbedEndpoint"),
            _Endpoint(
                "RESPONSE",
                "responses",
                ("response",),
                "api",
                "openai.response:OpenaiResponseEndpoint",
            ),
        ),
    ),
    _Provider(
        "openai",
        "CodexConfigs",
        "codex",
        ("openai-codex",),
        "CODEX_CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CLI",
                "query_cli",
                ("cli", "code"),
                "agentic",
                "openai.codex:CodexCLIEndpoint",
            ),
        ),
    ),
    _Provider(
        "anthropic",
        "AnthropicConfigs",
        "anthropic",
        (),
        "ANTHROPIC_CONTEXT_WINDOWS",
        (
            _Endpoint(
                "MESSAGES",
                "messages",
                ("chat",),
                "api",
                "anthropic.messages:AnthropicMessagesEndpoint",
            ),
        ),
    ),
    _Provider(
        "anthropic",
        "ClaudeCodeConfigs",
        "claude_code",
        ("claude-code", "claude"),
        "CLAUDE_CODE_CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CLI",
                "query_cli",
                ("cli", "code"),
                "agentic",
                "anthropic.claude_code:ClaudeCodeCLIEndpoint",
            ),
        ),
    ),
    _Provider(
        "ollama",
        "OllamaConfigs",
        "ollama",
        (),
        None,
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "ollama.chat:OllamaChatEndpoint",
                _OLLAMA,
            ),
            _Endpoint(
                "EMBED",
                "embeddings",
                ("embed",),
                "api",
                "ollama.embed:OllamaEmbedEndpoint",
                _OLLAMA,
            ),
            _Endpoint(
                "GENERATE",
                "generate",
                ("generate", "completion"),
                "api",
                "ollama.generate:OllamaGenerateEndpoint",
                _OLLAMA,
            ),
        ),
    ),
    _Provider(
        "lattice",
        "LatticeConfigs",
        "lattice",
        (),
        None,
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "lattice.chat:LatticeChatEndpoint",
            ),
            _Endpoint(
                "EMBED",
                "embeddings",
                ("embed",),
                "api",
                "lattice.embed:LatticeEmbedEndpoint",
            ),
        ),
    ),
    _Provider(
        "tavily",
        "TavilyConfigs",
        "tavily",
        (),
        None,
        (
            _Endpoint("SEARCH", "search", (), "api", "tavily.search:TavilySearchEndpoint"),
            _Endpoint("EXTRACT", "extract", (), "api", "tavily.search:TavilyExtractEndpoint"),
        ),
    ),
    _Provider(
        "exa",
        "ExaConfigs",
        "exa",
        (),
        None,
        (
            _Endpoint("SEARCH", "search", (), "api", "exa.search:ExaSearchEndpoint"),
            _Endpoint(
                "CONTENTS",
                "contents",
                ("get_contents",),
                "api",
                "exa.contents:ExaContentsEndpoint",
            ),
            _Endpoint(
                "FIND_SIMILAR",
                "findSimilar",
                ("similar", "find_similar"),
                "api",
                "exa.find_similar:ExaFindSimilarEndpoint",
            ),
        ),
    ),
    _Provider(
        "firecrawl",
        "FirecrawlConfigs",
        "firecrawl",
        (),
        None,
        (
            _Endpoint(
                "SCRAPE",
                "v1/scrape",
                ("scrape",),
                "api",
                "firecrawl.scrape:FirecrawlScrapeEndpoint",
            ),
            _Endpoint("MAP", "v1/map", ("map",), "api", "firecrawl.map:FirecrawlMapEndpoint"),
            _Endpoint(
                "CRAWL",
                "v1/crawl",
                ("crawl",),
                "api",
                "firecrawl.crawl:FirecrawlCrawlEndpoint",
            ),
        ),
    ),
    _Provider(
        "perplexity",
        "PerplexityConfigs",
        "perplexity",
        (),
        "CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "perplexity.chat:PerplexityChatEndpoint",
            ),
        ),
    ),
    _Provider(
        "nvidia_nim",
        "NvidiaNimConfigs",
        "nvidia_nim",
        ("nvidia", "nim"),
        "CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "nvidia_nim.chat:NvidiaNimChatEndpoint",
            ),
            _Endpoint(
                "EMBED",
                "embeddings",
                ("embed",),
                "api",
                "nvidia_nim.embed:NvidiaNimEmbedEndpoint",
            ),
        ),
    ),
    _Provider(
        "deepseek",
        "DeepSeekConfigs",
        "deepseek",
        (),
        "CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "deepseek.chat:DeepseekChatEndpoint",
            ),
        ),
    ),
    _Provider(
        "google",
        "GeminiChatConfigs",
        "gemini",
        ("gemini-api",),
        "GEMINI_CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat",),
                "api",
                "google.chat:GeminiChatEndpoint",
            ),
        ),
    ),
    _Provider(
        "google",
        "GeminiCodeConfigs",
        "gemini_code",
        ("gemini-code", "gemini_cli", "gemini-cli"),
        "GEMINI_CODE_CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CLI",
                "query_cli",
                ("cli",),
                "agentic",
                "google.gemini_code:GeminiCLIEndpoint",
            ),
        ),
    ),
    _Provider(
        "groq",
        "GroqConfigs",
        "groq",
        (),
        "CONTEXT_WINDOWS",
        (
            _Endpoint("CHAT", "chat/completions", ("chat",), "api", "groq.chat:GroqChatEndpoint"),
            _Endpoint(
                "AUDIO_TRANSCRIPTION",
                "audio/transcriptions",
                ("whisper", "stt"),
                "api",
                "groq.audio_transcription:GroqAudioTranscriptionEndpoint",
            ),
        ),
    ),
    _Provider(
        "pi",
        "PiConfigs",
        "pi",
        ("pi-code", "pi_code"),
        "CONTEXT_WINDOWS",
        (_Endpoint("CLI", "query_cli", ("cli", "code"), "agentic", "pi.cli:PiCLIEndpoint"),),
    ),
    _Provider(
        "openrouter",
        "OpenRouterConfigs",
        "openrouter",
        ("open-router",),
        "CONTEXT_WINDOWS",
        (
            _Endpoint(
                "CHAT",
                "chat/completions",
                ("chat", "chat/completions"),
                "api",
                "openrouter.chat:OpenRouterEndpoint",
            ),
        ),
    ),
    _Provider(
        "ag2",
        "AG2Configs",
        "ag2",
        ("autogen",),
        None,
        (
            _Endpoint(
                "GROUP_CHAT",
                "group_chat",
                ("groupchat", "chat"),
                "agentic",
                "ag2.groupchat:AG2GroupChatEndpoint",
                _AG2,
            ),
            _Endpoint(
                "AGENT",
                "agent",
                ("beta", "ask"),
                "agentic",
                "ag2.agent:AG2BetaEndpoint",
                _AG2,
            ),
            _Endpoint(
                "NLIP",
                "nlip",
                ("nlip_remote", "remote"),
                "agentic",
                "ag2.nlip:AG2NlipEndpoint",
            ),
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


def _raise_activation_failed(provider: str, endpoint: str, error: ImportError) -> NoReturn:
    """Factory of an endpoint whose provider's ``_config`` failed to import; imports nothing."""
    raise ProviderActivationError(
        provider=provider,
        endpoint=endpoint,
        source=_SOURCE,
        reason="activation_failed",
    ) from error


def _declare_unavailable_endpoint(
    provider: str, endpoint: _Endpoint, error: ImportError
) -> EndpointDeclaration:
    """Declare one endpoint from its row alone: it has no settings and its factory raises."""
    return EndpointDeclaration(
        key=endpoint.key,
        aliases=endpoint.aliases,
        kind=endpoint.kind,
        factory=partial(_raise_activation_failed, provider, endpoint.key, error),
        requirements=endpoint.requirements,
        settings={},
        diagnostic_id=f"{_PROVIDERS}.{endpoint.target}",
    )


def _declare_unavailable_provider(row: _Provider, error: ImportError) -> ProviderFragment:
    """Declare a provider fragment from its row alone, after its ``_config`` failed to import."""
    endpoints = tuple(
        _declare_unavailable_endpoint(row.key, endpoint, error) for endpoint in row.endpoints
    )
    return ProviderFragment(
        contract_version=_CONTRACT_VERSION,
        key=row.key,
        aliases=row.aliases,
        endpoints=endpoints,
        default_endpoint=endpoints[0].key,
        context_windows=None,
    )


def _declare_provider(row: _Provider) -> ProviderFragment:
    """Declare one provider fragment from its declaration row and its ``_config`` module.

    A ``_config`` that raises ``ImportError`` yields a fragment built from the row alone; any
    other exception propagates.
    """
    try:
        config = _load_config(row.package)
    except ImportError as error:
        return _declare_unavailable_provider(row, error)
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
        context_window_order=_CONTEXT_WINDOW_ORDER,
    )
