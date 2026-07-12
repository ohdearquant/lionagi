# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

from lionagi.ln.types import Enum

__all__ = (
    "EndpointType",
    "EndpointMeta",
    "EndpointRegistry",
    "register_endpoint",
)


class EndpointType(Enum):
    API = "api"
    AGENTIC = "agentic"


@dataclass(frozen=True, slots=True)
class EndpointMeta:
    """Injected onto endpoint classes as ``_ENDPOINT_META``; drives auto-generated ``EndpointConfig``."""

    provider: str
    endpoint: str
    endpoint_type: EndpointType
    aliases: tuple[str, ...] = ()
    provider_aliases: tuple[str, ...] = ()
    options: type[BaseModel] | None = None
    base_url: str | None = None
    auth_type: str | None = None
    content_type: str | None = None
    api_key_env: str | None = None

    def create_config(self, **overrides: Any):
        from .endpoint_config import EndpointConfig

        is_agentic = self.endpoint_type == EndpointType.AGENTIC
        api_key: Any = "internal" if is_agentic else None
        if not is_agentic and self.api_key_env and "api_key" not in overrides:
            from lionagi.config import settings

            raw = getattr(settings, self.api_key_env, None)
            # Pass SecretStr directly so _validate_api_key uses its dedicated branch;
            # None means the env var is unset, fall back to the testing sentinel.
            api_key = raw if raw is not None else "dummy-key-for-testing"
        defaults = dict(
            name=f"{self.provider}_{self.endpoint}",
            provider=self.provider,
            base_url=self.base_url or ("internal" if is_agentic else ""),
            endpoint=self.endpoint,
            api_key=api_key,
            request_options=self.options,
            timeout=3600 if is_agentic else 600,
            auth_type=self.auth_type or ("bearer" if not is_agentic else "bearer"),
            content_type=self.content_type or "application/json",
            method="POST",
        )
        defaults.update(overrides)
        return EndpointConfig(**defaults)


class _RegistryEntry:
    __slots__ = ("meta", "cls")

    def __init__(self, meta: EndpointMeta, cls: type):
        self.meta = meta
        self.cls = cls


class EndpointRegistry:
    _entries: ClassVar[list[_RegistryEntry]] = []
    _loaded: ClassVar[bool] = False
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def register(
        cls,
        provider: str,
        endpoint: str,
        aliases: list[str] | None = None,
        endpoint_type: EndpointType = EndpointType.API,
        provider_aliases: list[str] | None = None,
        options: type[BaseModel] | None = None,
        base_url: str | None = None,
        auth_type: str | None = None,
        content_type: str | None = None,
        api_key_env: str | None = None,
    ):
        def decorator(endpoint_cls: type) -> type:
            meta = EndpointMeta(
                provider=provider,
                endpoint=endpoint,
                endpoint_type=endpoint_type,
                aliases=tuple(aliases or ()),
                provider_aliases=tuple(provider_aliases or ()),
                options=options,
                base_url=base_url,
                auth_type=auth_type,
                content_type=content_type,
                api_key_env=api_key_env,
            )
            endpoint_cls._ENDPOINT_META = meta
            cls._entries.append(_RegistryEntry(meta=meta, cls=endpoint_cls))
            return endpoint_cls

        return decorator

    @classmethod
    def match(cls, provider: str, endpoint: str = "", **kwargs) -> Any:
        """Find and instantiate the best matching endpoint.

        On a registry miss, consults the plugin registry (ADR-0088 D3) before
        falling back to the generic OpenAI-compatible endpoint: any
        trusted + enabled + still-trusted plugin's declared provider modules
        are imported (firing their ``@register_endpoint`` decorators), and
        the match is re-run once. A plugin that supplies no matching provider
        — or none at all — leaves the fallback identical to today's.
        """
        cls._ensure_loaded()

        matched = cls._match_registered(provider, endpoint, kwargs)
        if matched is not None:
            return matched

        if cls._consult_plugin_providers():
            matched = cls._match_registered(provider, endpoint, kwargs)
            if matched is not None:
                return matched

        from .endpoint import Endpoint, EndpointConfig

        config = EndpointConfig(
            provider=provider,
            endpoint=endpoint or "chat/completions",
            name="openai_compatible_chat",
            auth_type="bearer",
            content_type="application/json",
            method="POST",
            requires_tokens=True,
        )
        return Endpoint(config, **kwargs)

    @classmethod
    def _match_registered(cls, provider: str, endpoint: str, kwargs: dict[str, Any]) -> Any | None:
        """Scan currently-registered entries (built-in + any plugin-activated). ``None`` = no match."""
        first_for_provider = None
        for entry in cls._entries:
            m = entry.meta
            if not (provider == m.provider or provider in m.provider_aliases):
                continue
            if first_for_provider is None:
                first_for_provider = entry
            if not endpoint or endpoint == m.endpoint or endpoint in m.aliases:
                return entry.cls(None, **kwargs)

        if first_for_provider is not None:
            # Single-endpoint providers (claude_code, codex, pi) always match; non-empty unmatched falls through.
            if not endpoint:
                return first_for_provider.cls(None, **kwargs)
            prov = first_for_provider.meta.provider
            n = sum(
                1
                for e in cls._entries
                if e.meta.provider == prov or prov in e.meta.provider_aliases
            )
            if n == 1:
                return first_for_provider.cls(None, **kwargs)

        return None

    @classmethod
    def _consult_plugin_providers(cls) -> bool:
        """Import every ACTIVE plugin's declared provider module (ADR-0088 D3), lazily.

        Runs only from ``match()`` after a registered-entry miss — never at
        import time or discovery, preserving import-time O(1). Goes through
        ``PluginRegistry.activate_target`` exclusively (never a direct
        ``importlib`` call on plugin code), so the trust/enabled/active
        chokepoints already enforced there apply here too. Each activation is
        cached by the plugin registry itself, so repeated misses are cheap.
        Returns whether any module import succeeded — the caller only retries
        the match when this is true.
        """
        try:
            from lionagi.plugins import PluginActivationError, PluginRegistry
        except ImportError:
            return False

        targets = PluginRegistry.active_provider_targets()
        if not targets:
            return False

        imported = False
        for plugin_name, module in targets:
            try:
                PluginRegistry.activate_target(plugin_name, module)
                imported = True
            except PluginActivationError:
                continue
        return imported

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        with cls._lock:
            if cls._loaded:
                return
            _import_all_providers()
            cls._loaded = True

    @classmethod
    def list_providers(cls) -> list[dict[str, Any]]:
        cls._ensure_loaded()
        return [
            {
                "provider": e.meta.provider,
                "endpoint": e.meta.endpoint,
                "aliases": list(e.meta.aliases),
                "type": e.meta.endpoint_type.value,
                "class": e.cls.__name__,
                "options": e.meta.options.__name__ if e.meta.options else None,
            }
            for e in cls._entries
        ]


def register_endpoint(
    provider: str,
    endpoint: str,
    aliases: list[str] | None = None,
    endpoint_type: EndpointType = EndpointType.API,
    provider_aliases: list[str] | None = None,
    options: type[BaseModel] | None = None,
    base_url: str | None = None,
    auth_type: str | None = None,
    content_type: str | None = None,
    api_key_env: str | None = None,
):
    """Decorator that registers an endpoint and injects ``_ENDPOINT_META``."""
    return EndpointRegistry.register(
        provider=provider,
        endpoint=endpoint,
        aliases=aliases,
        endpoint_type=endpoint_type,
        provider_aliases=provider_aliases,
        options=options,
        base_url=base_url,
        auth_type=auth_type,
        content_type=content_type,
        api_key_env=api_key_env,
    )


def _import_all_providers():
    """Import all provider modules to trigger registration decorators."""
    import importlib

    _modules = [
        # OpenAI family
        "lionagi.providers.openai.chat",
        "lionagi.providers.openai.codex",
        "lionagi.providers.openai.audio",
        "lionagi.providers.openai.images",
        "lionagi.providers.openai.embed",
        "lionagi.providers.openai.response",
        # Anthropic
        "lionagi.providers.anthropic.messages",
        "lionagi.providers.anthropic.claude_code",
        # Ollama
        "lionagi.providers.ollama.chat",
        "lionagi.providers.ollama.embed",
        "lionagi.providers.ollama.generate",
        # Search & scraping
        "lionagi.providers.tavily.search",
        "lionagi.providers.exa.search",
        "lionagi.providers.exa.contents",
        "lionagi.providers.exa.find_similar",
        "lionagi.providers.firecrawl.scrape",
        "lionagi.providers.firecrawl.map",
        "lionagi.providers.firecrawl.crawl",
        # Chat / LLM providers
        "lionagi.providers.perplexity.chat",
        "lionagi.providers.nvidia_nim.chat",
        "lionagi.providers.nvidia_nim.embed",
        "lionagi.providers.deepseek.chat",
        "lionagi.providers.google.chat",
        "lionagi.providers.google.gemini_code",
        "lionagi.providers.groq.chat",
        "lionagi.providers.groq.audio_transcription",
        "lionagi.providers.pi.cli",
        "lionagi.providers.openrouter.chat",
        # Agentic
        "lionagi.providers.ag2.groupchat",
        "lionagi.providers.ag2.agent",
        "lionagi.providers.ag2.nlip",
        # Test-only scripted provider (provider="scripted") — leaf module,
        # always loadable; gated behind LIONAGI_CHAT_PROVIDER=scripted.
        "lionagi.testing._endpoint",
    ]
    for mod in _modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            pass
