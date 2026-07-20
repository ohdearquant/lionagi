# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import logging
import threading
import warnings
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

from lionagi.ln.types import Enum

__all__ = (
    "EndpointType",
    "EndpointMeta",
    "EndpointRegistry",
    "ProviderAliasCollisionError",
    "ProviderNotFoundError",
    "register_endpoint",
)

logger = logging.getLogger(__name__)

# (mtime_ns, ctime_ns, size, inode) for one file -- see _plugin_entry_stat.
# A cheap first gate only; see _plugin_entry_digest for the correctness guarantee.
_FileStat = tuple[int, int, int, int]

# The manifest digest and every declared path's digest for one plugin entry --
# see _plugin_entry_digest.
_ContentDigest = tuple[str, tuple[tuple[str, str], ...]]

# Manifest metadata, every manifest-declared path's metadata, and the user
# settings source mtime -- see _plugin_entry_stat.
_PluginStatSignature = tuple[_FileStat, tuple[tuple[str, _FileStat], ...], int | None]


class ProviderAliasCollisionError(ValueError):
    """A provider or provider-alias string is already claimed by a different canonical provider."""


class ProviderNotFoundError(ValueError):
    """No registered endpoint matches the requested provider, and no fallback was authorized."""


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
        # snapshot generation, full plugin stat signature, and the entry's
        # manifest + all-declared-path content digests as of the last clean
        # activate_target() call. See _revalidate_plugin_entry.
        self._validated_generation: int | None = None
        self._validated_stat: _PluginStatSignature | None = None
        self._validated_digest: _ContentDigest | None = None


class EndpointRegistry:
    _entries: ClassVar[list[_RegistryEntry]] = []
    _loaded: ClassVar[bool] = False
    _lock: ClassVar[threading.RLock] = threading.RLock()
    _plugin_registration: ClassVar[threading.local] = threading.local()

    # Canonical alias string (provider name or provider_alias, lowercased) ->
    # the canonical provider name that first claimed it. Lets a provider
    # register any number of endpoints under its own name (expected: openai
    # alone owns half a dozen entries) while still catching a *different*
    # provider trying to claim a name or alias someone else already owns.
    _alias_owners: ClassVar[dict[str, str]] = {}

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
        canonical_provider = provider.strip().lower()
        canonical_provider_aliases = tuple(
            dict.fromkeys(a.strip().lower() for a in (provider_aliases or ()))
        )
        cls._claim_provider_identity(canonical_provider, canonical_provider_aliases)

        def decorator(endpoint_cls: type) -> type:
            meta = EndpointMeta(
                provider=canonical_provider,
                endpoint=endpoint,
                endpoint_type=endpoint_type,
                aliases=tuple(aliases or ()),
                provider_aliases=canonical_provider_aliases,
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
    def _claim_provider_identity(cls, provider: str, provider_aliases: tuple[str, ...]) -> None:
        """Reject a provider/alias string already owned by a *different* canonical provider.

        Re-registering the same canonical provider (e.g. openai's chat, embed,
        batch, ... endpoints) is expected and always allowed. A collision is
        two different canonical providers claiming the same string, either as
        one's canonical name or as either one's alias.
        """
        for key in (provider, *provider_aliases):
            owner = cls._alias_owners.get(key)
            if owner is not None and owner != provider:
                raise ProviderAliasCollisionError(
                    f"provider alias {key!r} is already registered to provider "
                    f"{owner!r}; cannot also register it for provider {provider!r}"
                )
        for key in (provider, *provider_aliases):
            cls._alias_owners.setdefault(key, provider)

    @classmethod
    def match(
        cls,
        provider: str,
        endpoint: str = "",
        *,
        openai_compatible: bool = False,
        **kwargs,
    ) -> Any:
        """Find and instantiate the best matching endpoint. On a registry
        miss, consults the plugin registry (ADR-0088 D3) before falling back
        to the generic OpenAI-compatible endpoint; see docs/internals/runtime.md.

        An unrecognized ``provider`` never silently mis-routes: the generic
        OpenAI-compatible fallback only builds when ``openai_compatible=True``
        is passed explicitly, or (deprecated migration path, warns) when a
        ``base_url`` kwarg is given -- the same signal a caller already needs
        to point the fallback at a real custom host. Anything else raises
        ``ProviderNotFoundError`` naming the requested provider and every
        provider currently registered.
        """
        cls._ensure_loaded()

        matched = cls._match_registered(provider, endpoint, kwargs)
        if matched is not None:
            return matched

        if cls._consult_plugin_providers():
            matched = cls._match_registered(provider, endpoint, kwargs)
            if matched is not None:
                return matched

        if not openai_compatible:
            if kwargs.get("base_url"):
                warnings.warn(
                    f"provider {provider!r} is not registered; routing to the "
                    "generic OpenAI-compatible endpoint because base_url= was "
                    "given. This implicit fallback is deprecated -- pass "
                    "openai_compatible=True explicitly (e.g. "
                    "match_endpoint(..., openai_compatible=True)) to silence "
                    "this warning.",
                    DeprecationWarning,
                    stacklevel=3,
                )
            else:
                raise cls._provider_not_found_error(provider)

        from .endpoint import Endpoint, EndpointConfig

        config = EndpointConfig(
            provider=provider,
            endpoint=endpoint or "chat/completions",
            name="openai_compatible_chat",
            auth_type="bearer",
            content_type="application/json",
            method="POST",
            requires_tokens=True,
            openai_compatible=True,
        )
        return Endpoint(config, **kwargs)

    @classmethod
    def _provider_not_found_error(cls, provider: str) -> ProviderNotFoundError:
        known: set[str] = set()
        for entry in cls._entries:
            known.add(entry.meta.provider)
            known.update(entry.meta.provider_aliases)
        return ProviderNotFoundError(
            f"no endpoint registered for provider {provider!r}; registered "
            f"providers: {', '.join(sorted(known)) or '(none)'}. Pass "
            "openai_compatible=True to route unrecognized providers to the "
            "generic OpenAI-compatible endpoint explicitly."
        )

    @classmethod
    def _match_registered(cls, provider: str, endpoint: str, kwargs: dict[str, Any]) -> Any | None:
        """Scan currently-registered entries (built-in + any plugin-activated). ``None`` = no match."""
        provider = provider.lower()
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
            n = sum(
                1
                for e in tuple(cls._entries)
                if e.meta.provider == provider or provider in e.meta.provider_aliases
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
        ``reset()`` happened), when this plugin's manifest, any declared
        path, or user settings source changed (see ``_plugin_entry_stat``),
        or -- when that stat signature still matches -- when the entry's own
        content digest (see ``_plugin_entry_digest``) no longer matches;
        otherwise reuses that prior result.

        The stat signature alone is not a portable content-change
        guarantee: ``os.utime()`` restores a spoofed mtime after an edit,
        and on platforms where ``st_ctime_ns`` is not a metadata-change
        token (Windows CPython documents it as file *creation* time, which
        a content write or ``os.utime()`` never advances), a same-length
        in-place edit can leave the whole stat tuple looking unchanged. The
        content digest is only computed on that stat-stable path -- the
        files plugins declare are small, so paying for the read there is
        cheap -- and closes that hole on every platform: it always changes
        when the manifest or any declared capability file's bytes do.
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
    def _plugin_entry_stat(cls, plugin_name: str, target: str) -> _PluginStatSignature | None:
        """Metadata for the manifest, every declared path, and user settings.

        This is the cheap ``has anything relevant changed`` first gate for
        ``_revalidate_plugin_entry``. It is a *probabilistic* signal, not a
        correctness guarantee; see ``_plugin_entry_digest`` for the content
        guarantee this gate feeds into for every declared capability file.

        Each file metadata value is ``(mtime_ns, ctime_ns, size, inode)``.
        mtime ALONE is not a valid content-pinning signal: ``os.utime()`` lets
        a caller edit a file's bytes and then restore its original mtime.
        ctime narrows that hole on filesystems where it tracks inode metadata
        changes, but it is not portable: current CPython documents ``st_ctime``/
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

        ``None`` (unknown plugin, invalid manifest, unresolvable path, or a
        declared file missing) always forces the caller back onto the full
        ``activate_target()`` path.
        """
        from lionagi.plugins import PluginRegistry
        from lionagi.plugins._user_settings import user_settings_path
        from lionagi.plugins.discovery import _collect_declared_paths

        record = PluginRegistry.get(plugin_name)
        if record is None or record.manifest is None:
            return None
        declared_paths = set(_collect_declared_paths(record.manifest))
        if target.split(":", 1)[0] not in declared_paths:
            return None
        try:
            manifest_stat = record.manifest_path.stat()
            declared_stats = []
            for relative_path in sorted(declared_paths):
                path_stat = (record.bundle_dir / relative_path).stat()
                declared_stats.append(
                    (
                        relative_path,
                        (
                            path_stat.st_mtime_ns,
                            path_stat.st_ctime_ns,
                            path_stat.st_size,
                            path_stat.st_ino,
                        ),
                    )
                )
        except OSError:
            return None

        try:
            settings_mtime_ns = user_settings_path().stat().st_mtime_ns
        except FileNotFoundError:
            settings_mtime_ns = None
        except OSError:
            return None

        return (
            (
                manifest_stat.st_mtime_ns,
                manifest_stat.st_ctime_ns,
                manifest_stat.st_size,
                manifest_stat.st_ino,
            ),
            tuple(declared_stats),
            settings_mtime_ns,
        )

    @classmethod
    def _plugin_entry_digest(cls, plugin_name: str, target: str) -> _ContentDigest | None:
        """Content hashes for an entry's manifest and every declared path.

        Unlike any timestamp/size/inode signature, these hashes always
        change when any file covered by the plugin's trust record changes.
        They are only computed on ``_plugin_entry_stat``'s stat-stable path;
        a signature that already looks different skips straight to
        ``activate_target()``.

        ``None`` means the plugin is unknown, the target is undeclared, or a
        covered file could not be read.
        """
        from lionagi.plugins import PluginRegistry
        from lionagi.plugins.discovery import _collect_declared_paths

        record = PluginRegistry.get(plugin_name)
        if record is None or record.manifest is None:
            return None
        module_path = target.split(":", 1)[0]
        declared_paths = set(_collect_declared_paths(record.manifest))
        if module_path not in declared_paths:
            return None
        try:
            manifest_bytes = record.manifest_path.read_bytes()
            declared_digests = tuple(
                (
                    relative_path,
                    hashlib.blake2b((record.bundle_dir / relative_path).read_bytes()).hexdigest(),
                )
                for relative_path in sorted(declared_paths)
            )
        except OSError:
            return None
        return (
            hashlib.blake2b(manifest_bytes).hexdigest(),
            declared_digests,
        )

    @classmethod
    def _consult_plugin_providers(cls) -> bool:
        """Import every ACTIVE plugin's declared provider module (ADR-0088
        D3), lazily, only from ``match()`` after a registered-entry miss —
        never at import time. The reentrant lock keeps activation atomic
        across threads while allowing activated code to perform a nested
        endpoint lookup. Returns whether any import succeeded.
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
            with cls._lock:
                if any(
                    entry.plugin_name == plugin_name and entry.plugin_target == module
                    for entry in cls._entries
                ):
                    imported = True
                    continue

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
                except ProviderAliasCollisionError as exc:
                    # A plugin claiming a provider/alias another provider already
                    # owns must not crash resolution -- reject just this plugin's
                    # contribution, the same fail-soft posture as a built-in
                    # collision (_reject_builtin_collisions) or a broken import.
                    logger.warning(
                        "plugin %r provider module %r rejected: %s",
                        plugin_name,
                        module,
                        exc,
                    )
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
        except ImportError as e:
            if _is_missing_optional_dependency(e):
                logger.debug(
                    "provider module %r not registered: optional dependency "
                    "%r is not installed (%s)",
                    mod,
                    e.name,
                    e,
                )
            else:
                logger.warning(
                    "provider module %r failed to import and was not registered: %s",
                    mod,
                    e,
                )


def _is_missing_optional_dependency(exc: ImportError) -> bool:
    """Tell a genuinely-absent optional third-party dependency apart from a
    broken bundled module.

    Every bundled provider module defers its heavy optional dependencies
    (``autogen``, ``ollama``, ``nlip_sdk``, ...) to call time, guarded by
    ``is_import_installed`` or a local ``try/except ImportError`` -- none of
    them import a third-party package at module scope. So a module-scope
    ``ImportError`` here is expected only when the failing name (reported by
    Python as ``ImportError.name``) resolves to a third-party package that is
    actually not installed. Anything else -- ``.name`` unset (the
    "cannot import name X from Y" shape a broken re-export raises), a
    ``lionagi.*`` name (a bug in our own import graph), or a name that *is*
    installed (so importing it failed for some other reason) -- is a broken
    bundled module and must be logged loudly instead of vanishing silently.
    """
    from lionagi.utils import is_import_installed

    missing = getattr(exc, "name", None)
    if not missing:
        return False
    top_level = missing.split(".", 1)[0]
    if top_level == "lionagi":
        return False
    return not is_import_installed(top_level)
