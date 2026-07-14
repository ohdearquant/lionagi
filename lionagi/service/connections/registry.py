# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import logging
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

logger = logging.getLogger(__name__)

# (mtime_ns, ctime_ns, size, inode) for one file -- see _plugin_entry_stat.
# A cheap first gate only; see _plugin_entry_digest for the correctness guarantee.
_FileStat = tuple[int, int, int, int]

# (manifest_digest, target_digest) for one plugin entry -- see _plugin_entry_digest.
_ContentDigest = tuple[str, str]


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
    __slots__ = (
        "meta",
        "cls",
        "plugin_name",
        "plugin_target",
        "_validated_generation",
        "_validated_stat",
        "_validated_digest",
    )

    def __init__(self, meta: EndpointMeta, cls: type):
        self.meta = meta
        self.cls = cls
        self.plugin_name: str | None = None
        self.plugin_target: str | None = None
        # Fast-path cache for _revalidate_plugin_entry: the PluginRegistry
        # snapshot generation, (manifest, target) stat signatures, and
        # (manifest, target) content digests as of the last clean
        # activate_target() call. See _revalidate_plugin_entry.
        self._validated_generation: int | None = None
        self._validated_stat: tuple[_FileStat, _FileStat] | None = None
        self._validated_digest: _ContentDigest | None = None


class EndpointRegistry:
    _entries: ClassVar[list[_RegistryEntry]] = []
    _loaded: ClassVar[bool] = False
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _plugin_registration: ClassVar[threading.local] = threading.local()

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
            entry = _RegistryEntry(meta=meta, cls=endpoint_cls)
            provenance = getattr(cls._plugin_registration, "provenance", None)
            if provenance is not None:
                entry.plugin_name, entry.plugin_target = provenance
            cls._entries.append(entry)
            return endpoint_cls

        return decorator

    @classmethod
    def match(cls, provider: str, endpoint: str = "", **kwargs) -> Any:
        """Find and instantiate the best matching endpoint. On a registry
        miss, consults the plugin registry (ADR-0088 D3) before falling back
        to the generic OpenAI-compatible endpoint; see docs/internals/runtime.md.
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
        for entry in tuple(cls._entries):
            m = entry.meta
            if not (provider == m.provider or provider in m.provider_aliases):
                continue
            if not cls._revalidate_plugin_entry(entry):
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
                for e in tuple(cls._entries)
                if e.meta.provider == prov or prov in e.meta.provider_aliases
                if cls._revalidate_plugin_entry(e)
            )
            if n == 1:
                return first_for_provider.cls(None, **kwargs)

        return None

    @classmethod
    def _revalidate_plugin_entry(cls, entry: _RegistryEntry) -> bool:
        """Keep plugin entries available only while their declared target
        remains trusted. ``PluginRegistry.activate_target()`` rescans and
        rehashes every installed plugin on each call, not just this one --
        too expensive to pay on every ``match()`` hit against an endpoint
        that already activated cleanly. Only re-runs it when the
        ``PluginRegistry`` snapshot generation has strictly advanced (a
        ``reset()`` happened), when this plugin's manifest or declared
        target file's cheap stat signature (see ``_plugin_entry_stat``) no
        longer matches the last clean revalidation, or -- when that stat
        signature still matches -- when its content digest (see
        ``_plugin_entry_digest``) no longer matches either; otherwise reuses
        that prior result.

        The stat signature alone is not a portable content-change
        guarantee: ``os.utime()`` restores a spoofed mtime after an edit,
        and on platforms where ``st_ctime_ns`` is not a metadata-change
        token (Windows CPython documents it as file *creation* time, which
        a content write or ``os.utime()`` never advances), a same-length
        in-place edit can leave the whole stat tuple looking unchanged. The
        content digest is only computed on that stat-stable path -- the
        files plugins declare are small, so paying for the read there is
        cheap -- and closes that hole on every platform: it always changes
        when either file's bytes do.
        """
        if entry.plugin_name is None or entry.plugin_target is None:
            return True

        from lionagi.plugins import PluginActivationError, PluginRegistry

        generation = PluginRegistry.snapshot_generation()
        stat_signature = cls._plugin_entry_stat(entry.plugin_name, entry.plugin_target)
        stat_unchanged = (
            stat_signature is not None
            and entry._validated_generation == generation
            and entry._validated_stat == stat_signature
        )
        if stat_unchanged:
            digest = cls._plugin_entry_digest(entry.plugin_name, entry.plugin_target)
            if digest is not None and entry._validated_digest == digest:
                return True

        try:
            PluginRegistry.activate_target(entry.plugin_name, entry.plugin_target)
        except PluginActivationError:
            try:
                cls._entries.remove(entry)
            except ValueError:
                pass
            return False

        entry._validated_generation = generation
        entry._validated_stat = cls._plugin_entry_stat(entry.plugin_name, entry.plugin_target)
        entry._validated_digest = cls._plugin_entry_digest(entry.plugin_name, entry.plugin_target)
        return True

    @classmethod
    def _plugin_entry_stat(
        cls, plugin_name: str, target: str
    ) -> tuple[_FileStat, _FileStat] | None:
        """``(manifest_stat, target_stat)`` for a plugin-provided endpoint's
        backing files -- a cheap ``has anything on disk changed`` first gate
        for ``_revalidate_plugin_entry``'s fast path. It is a *probabilistic*
        signal, not a correctness guarantee; see ``_plugin_entry_digest``
        for the guarantee this gate feeds into.

        Each element is ``(mtime_ns, ctime_ns, size, inode)``. mtime ALONE is
        not a valid content-pinning signal: ``os.utime()`` lets a caller edit
        a file's bytes and then restore its original mtime. ctime narrows
        that hole on filesystems where it tracks inode metadata changes, but
        it is not portable: current CPython documents ``st_ctime``/
        ``st_ctime_ns`` as file *creation* time on Windows, so neither a
        content write nor ``os.utime()`` advances it there, and timestamp
        resolution is filesystem-dependent in general. size and inode are
        free extra signal from the same ``stat()`` call (no additional
        syscall) and catch same-second same-mtime same-ctime edits and
        delete+recreate respectively, but a same-length in-place edit
        defeats size too. Whenever this whole tuple compares unchanged,
        ``_revalidate_plugin_entry`` confirms with ``_plugin_entry_digest``
        before trusting it -- that confirmation, not this stat tuple, is
        what makes the fast path safe to serve from cache.

        ``None`` (unknown plugin, unresolvable path, either file missing)
        always forces the caller back onto the full ``activate_target()`` path.
        """
        from lionagi.plugins import PluginRegistry

        record = PluginRegistry.get(plugin_name)
        if record is None:
            return None
        module_path = target.split(":", 1)[0]
        try:
            manifest_stat = record.manifest_path.stat()
            target_stat = (record.bundle_dir / module_path).stat()
        except OSError:
            return None
        return (
            (
                manifest_stat.st_mtime_ns,
                manifest_stat.st_ctime_ns,
                manifest_stat.st_size,
                manifest_stat.st_ino,
            ),
            (
                target_stat.st_mtime_ns,
                target_stat.st_ctime_ns,
                target_stat.st_size,
                target_stat.st_ino,
            ),
        )

    @classmethod
    def _plugin_entry_digest(cls, plugin_name: str, target: str) -> _ContentDigest | None:
        """``(manifest_digest, target_digest)`` -- a content hash of the same
        two files ``_plugin_entry_stat`` stats, and the correctness
        guarantee ``_revalidate_plugin_entry``'s fast path actually relies
        on. Unlike any timestamp/size/inode signature, a hash of the file's
        bytes always changes when the bytes do, on every platform and
        filesystem -- there is no metadata field to spoof or leave static.
        Only computed on the stat-stable path (see ``_plugin_entry_stat``):
        plugin manifest and target files are small, so the extra read here
        is cheap, and an entry whose stat signature already looks different
        skips straight to ``activate_target()`` without paying for it.

        ``None`` under the same conditions ``_plugin_entry_stat`` returns
        ``None`` for (unknown plugin, unresolvable path, either file missing).
        """
        from lionagi.plugins import PluginRegistry

        record = PluginRegistry.get(plugin_name)
        if record is None:
            return None
        module_path = target.split(":", 1)[0]
        try:
            manifest_bytes = record.manifest_path.read_bytes()
            target_bytes = (record.bundle_dir / module_path).read_bytes()
        except OSError:
            return None
        return (
            hashlib.blake2b(manifest_bytes).hexdigest(),
            hashlib.blake2b(target_bytes).hexdigest(),
        )

    @classmethod
    def _consult_plugin_providers(cls) -> bool:
        """Import every ACTIVE plugin's declared provider module (ADR-0088
        D3), lazily, only from ``match()`` after a registered-entry miss —
        never at import time. Returns whether any import succeeded.
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
            previous = getattr(cls._plugin_registration, "provenance", None)
            cls._plugin_registration.provenance = (plugin_name, module)
            try:
                activated = PluginRegistry.activate_target(plugin_name, module)
                module_name = getattr(activated, "__name__", None)
                for entry in cls._entries:
                    if module_name is not None and entry.cls.__module__ == module_name:
                        entry.plugin_name = plugin_name
                        entry.plugin_target = module
                cls._reject_builtin_collisions(plugin_name, module, module_name)
                imported = True
            except PluginActivationError:
                continue
            finally:
                if previous is None:
                    del cls._plugin_registration.provenance
                else:
                    cls._plugin_registration.provenance = previous
        return imported

    @classmethod
    def _reject_builtin_collisions(
        cls, plugin_name: str, module: str, module_name: str | None
    ) -> None:
        """ADR-0088 D6: a plugin provider must never silently take over a
        provider name a built-in already serves. Drop (and log) any entry
        this activation just added whose provider name (or provider alias)
        matches an already-registered built-in entry -- the built-in stays
        authoritative and the plugin entry is rejected.
        """
        if module_name is None:
            return

        builtin_names: set[str] = set()
        for entry in cls._entries:
            if entry.plugin_name is None:
                builtin_names.add(entry.meta.provider)
                builtin_names.update(entry.meta.provider_aliases)
        if not builtin_names:
            return

        kept: list[_RegistryEntry] = []
        for entry in cls._entries:
            is_this_activation = (
                entry.plugin_name == plugin_name
                and entry.plugin_target == module
                and entry.cls.__module__ == module_name
            )
            collides = entry.meta.provider in builtin_names or any(
                alias in builtin_names for alias in entry.meta.provider_aliases
            )
            if is_this_activation and collides:
                logger.warning(
                    "plugin %r provider module %r declares provider %r, which "
                    "a built-in already serves; the built-in wins and this "
                    "plugin entry is rejected (ADR-0088 D6)",
                    plugin_name,
                    module,
                    entry.meta.provider,
                )
                continue
            kept.append(entry)
        cls._entries[:] = kept

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
        "lionagi.providers.openai.batch",
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
