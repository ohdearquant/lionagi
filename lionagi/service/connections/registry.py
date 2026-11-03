# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import threading
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from lionagi.ln import (
    HashAlgorithm,
    compute_hash,
    copy,
    hash_file,
    import_module,
    is_import_installed,
)
from lionagi.ln.types import Enum

if TYPE_CHECKING:
    from lionagi._feature_errors import MissingFeatureError
    from lionagi._provider_errors import ProviderActivationError

    from .provider_catalog import EndpointDeclaration, ProviderCatalog, ProviderFragment

__all__ = (
    "EndpointType",
    "EndpointMeta",
    "EndpointRegistry",
    "ProviderAliasCollisionError",
    "ProviderNotFoundError",
    "register_endpoint",
)

logger = logging.getLogger(__name__)

# The module that registers the test-only scripted provider, and the feature name the
# registry's typed absence errors carry.
_SCRIPTED_MODULE = "lionagi.testing._endpoint"
_PROVIDERS_FEATURE = "providers"

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

        api_key_explicit = "api_key" in overrides
        is_agentic = self.endpoint_type == EndpointType.AGENTIC
        api_key: Any = "internal" if is_agentic else None
        if not is_agentic and self.api_key_env and "api_key" not in overrides:
            api_key = self.api_key_env
        defaults = dict(
            name=f"{self.provider}_{self.endpoint}",
            provider=self.provider,
            base_url=self.base_url or ("internal" if is_agentic else ""),
            endpoint=self.endpoint,
            api_key=api_key,
            api_key_env=None if is_agentic else self.api_key_env,
            request_options=self.options,
            timeout=3600 if is_agentic else 600,
            auth_type=self.auth_type or ("bearer" if not is_agentic else "bearer"),
            content_type=self.content_type or "application/json",
            method="POST",
        )
        defaults.update(overrides)
        config = EndpointConfig(**defaults)
        config._api_key_explicit = api_key_explicit
        return config


@dataclass(frozen=True, slots=True)
class _FailedProvider:
    """A provider the bundled catalog declares whose activation failed when the registry loaded.

    ``names`` holds the provider's key and aliases; ``error`` is the typed error activating its
    declaration raised: a ``ProviderActivationError``, or a ``MissingFeatureError`` when a
    declared dependency is not installed.
    """

    provider: str
    names: tuple[str, ...]
    error: ProviderActivationError | MissingFeatureError


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

    # The bundled-catalog providers whose activation failed in the last full load, in catalog
    # order. Only the full load (``_ensure_loaded``) fills it.
    _failed: ClassVar[tuple[_FailedProvider, ...]] = ()

    # The failure of each bundled-catalog declaration whose factory raised, whether a selection
    # or the full load called it, keyed by ``id(declaration)``. The declaration rides along so
    # the id stays unique while the failure is kept.
    _activation_failures: ClassVar[dict[int, tuple[EndpointDeclaration, Exception]]] = {}

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

        def decorator(endpoint_cls: type) -> type:
            provenance = getattr(cls._plugin_registration, "provenance", None)
            try:
                cls._refuse_declared_provider(
                    endpoint_cls, canonical_provider, canonical_provider_aliases
                )
            except ProviderAliasCollisionError as exc:
                if provenance is None:
                    raise
                # A plugin provider module keeps running: only the refused class is skipped,
                # so the module's other endpoints still register.
                logger.warning(
                    "plugin %r provider module %r rejected endpoint class %r: %s",
                    provenance[0],
                    provenance[1],
                    endpoint_cls.__qualname__,
                    exc,
                )
                return endpoint_cls
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
            if provenance is not None:
                entry.plugin_name, entry.plugin_target = provenance
            # Alias-ownership check and the claim itself run as one atomic
            # transaction under the lock (a split check-then-claim lets two
            # concurrent registrations both pass), with entry publication
            # folded into the same critical section.
            with cls._lock:
                cls._claim_provider_identity(canonical_provider, canonical_provider_aliases)
                cls._entries.append(entry)
            return endpoint_cls

        return decorator

    @classmethod
    def _refuse_declared_provider(
        cls, endpoint_cls: type, provider: str, provider_aliases: tuple[str, ...]
    ) -> None:
        """Refuse a provider key or alias that the bundled provider catalog declares, unless
        ``f"{endpoint_cls.__module__}:{endpoint_cls.__qualname__}"`` equals the
        ``diagnostic_id`` of one of the endpoints the catalog declares for that provider.

        Only that string is compared, so a class with the module and qualified name of a
        declared endpoint passes whether or not it is the object the catalog loads. The
        built-in provider modules register the classes the catalog names, so they pass.
        The refusal holds whether or not the built-in provider activated: a provider whose
        activation failed still owns its names. When the
        catalog cannot load it declares nothing and nothing is refused here.

        Raises ``ProviderAliasCollisionError`` for a refused name. ``register`` lets it reach
        its caller, except while a plugin provider module is registering: there the refused
        class is skipped with a warning and the module keeps executing."""
        from lionagi._feature_errors import MissingFeatureError
        from lionagi._provider_errors import ProviderActivationError, ProviderContractVersionError

        from ._provider_compat import default_provider_catalog

        try:
            catalog = default_provider_catalog()
        except (MissingFeatureError, ProviderActivationError, ProviderContractVersionError):
            return
        implementation = f"{endpoint_cls.__module__}:{endpoint_cls.__qualname__}"
        for name in (provider, *provider_aliases):
            fragment = catalog.fragment(name)
            if fragment is None or any(
                declaration.diagnostic_id == implementation for declaration in fragment.endpoints
            ):
                continue
            raise ProviderAliasCollisionError(
                f"provider name {name!r} is declared by the bundled provider catalog for "
                f"provider {fragment.key!r}; only that provider's own endpoints can register "
                f"under it"
            )

    @classmethod
    def _claim_provider_identity(cls, provider: str, provider_aliases: tuple[str, ...]) -> None:
        """Reject a provider/alias string already owned by a *different* canonical
        provider; re-registering the same provider's own endpoints is always allowed.
        Callers must hold ``cls._lock`` — check-then-claim, atomic only under the lock."""
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
    def _remove_entries(cls, entries: list[_RegistryEntry]) -> None:
        """Drop ``entries`` from ``_entries`` and rebuild ``_alias_owners``. Must run
        under ``cls._lock``; the sole removal path, so the alias ledger never drifts."""
        if not entries:
            return
        drop = {id(e) for e in entries}
        cls._entries[:] = [e for e in cls._entries if id(e) not in drop]
        cls._rebuild_alias_owners()

    @classmethod
    def _rebuild_alias_owners(cls) -> None:
        """Recompute ``_alias_owners`` from the surviving ``_entries``. Must run under
        ``cls._lock`` after any entry removal, else a removed entry's alias keeps
        naming an owner with no remaining registration. First-registration-wins."""
        owners: dict[str, str] = {}
        for entry in cls._entries:
            for key in (entry.meta.provider, *entry.meta.provider_aliases):
                owners.setdefault(key, entry.meta.provider)
        cls._alias_owners = owners

    @classmethod
    def match(
        cls,
        provider: str,
        endpoint: str = "",
        *,
        openai_compatible: bool = False,
        provider_catalog: ProviderCatalog | None = None,
        **kwargs,
    ) -> Any:
        """Find and instantiate the best matching endpoint. With no supplied catalog the
        bundled provider catalog is read first, for its declarations only, and a request it
        selects imports that one declaration's implementation and nothing else: the provider
        is matched by key or alias with letter case folded, a request naming no endpoint
        selects the declared default endpoint, an endpoint key or alias selects that endpoint,
        and a provider declaring exactly one endpoint selects it for any endpoint name. A
        failure to activate the selected declaration raises at once, and neither the generic
        OpenAI-compatible endpoint nor another endpoint answers in its place, whatever
        ``openai_compatible`` or ``base_url`` say: an internal import failure, a syntax error or
        a defect in the factory raises ``ProviderActivationError`` with the original exception
        chained, and a declared optional dependency that is not installed raises
        ``MissingFeatureError`` naming the provider, endpoint and distribution. A
        ``ModuleNotFoundError`` counts as an absent dependency only when its ``name`` equals one
        of the declared top-level import roots. The failure of a factory is kept per
        declaration: later selections raise a copy of it without calling the factory again and
        without repeating its warning. A declared provider is answered by its declarations
        alone, never by the registered entries, which hold only what has activated so far: asked
        for an endpoint it does not declare (when it declares more than one), it activates its
        default declaration, so a provider that cannot activate still raises, and otherwise
        routes to the generic endpoint.

        A provider the catalog does not declare scans the registered entries (the test-only
        scripted provider, plugin entries and classes registered by the caller). On a registry
        miss, consults the plugin registry before falling back to the generic OpenAI-compatible
        endpoint. When the bundled catalog cannot load, only the registered entries and plugins
        answer: a request they do not answer raises the load failure, with the requested
        provider named on a ``MissingFeatureError``, and the generic endpoint never replaces
        it, whatever ``openai_compatible`` or ``base_url`` say: a hub that is not installed leaves
        the installation incomplete, and a hub that is installed but broken is a defect, and
        neither is hidden. See docs/internals/core.md#endpointregistry-match for the
        provider-vs-endpoint rejection contract.

        Supplying ``provider_catalog`` selects from that catalog's declarations alone, in
        declaration order, by the same provider, endpoint and single-endpoint rules: the
        bundled catalog, the registered entries and the plugin registry are never read, and
        an empty catalog is not the same as omitting the argument. A declaration whose
        implementation fails to import raises instead of being skipped. A provider the
        catalog declares, asked for an endpoint it does not declare when it declares more
        than one, raises a ``ProviderNotFoundError`` naming the catalog and the endpoints it
        declares for that provider; ``openai_compatible=`` and ``base_url=`` do not change
        that, because the generic endpoint never extends an explicitly supplied catalog. A
        provider the catalog does not declare gets the same ``openai_compatible=`` and
        ``base_url=`` outcomes, and otherwise a ``ProviderNotFoundError`` naming the
        catalog."""
        if provider_catalog is not None:
            matched = cls._match_catalog(provider_catalog, provider, endpoint, kwargs)
            if matched is not None:
                return matched
            return cls._generic_endpoint(
                provider, endpoint, openai_compatible, kwargs, provider_catalog
            )

        catalog, unavailable = cls._bundled_catalog()
        if catalog is not None:
            fragment = catalog.fragment(provider)
            if fragment is not None:
                # A declared provider is answered by its declarations alone. The registered
                # entries hold only the endpoints that happened to activate earlier, so
                # scanning them would make the answer depend on that history.
                declared = cls._match_declared(catalog, fragment, provider, endpoint, kwargs)
                if declared is not None:
                    return declared
                return cls._generic_endpoint(provider, endpoint, openai_compatible, kwargs, None)

        matched = cls._match_registered(provider, endpoint, kwargs)
        if matched is None:
            # The scripted provider registers on the first lookup nothing else answered.
            _import_provider_module(_SCRIPTED_MODULE)
            matched = cls._match_registered(provider, endpoint, kwargs)
        if matched is not None:
            return matched

        if cls._consult_plugin_providers():
            matched = cls._match_registered(provider, endpoint, kwargs)
            if matched is not None:
                return matched

        if unavailable is not None:
            raise cls._unavailable_catalog_error(unavailable, provider)
        return cls._generic_endpoint(provider, endpoint, openai_compatible, kwargs, None)

    @classmethod
    def _bundled_catalog(cls) -> tuple[ProviderCatalog | None, Exception | None]:
        """The catalog of the bundled provider manifest, or the typed error that kept it from
        loading, as ``(catalog, None)`` or ``(None, error)``. Reading the catalog imports no
        endpoint implementation."""
        from lionagi._feature_errors import MissingFeatureError
        from lionagi._provider_errors import ProviderActivationError, ProviderContractVersionError

        from ._provider_compat import default_provider_catalog

        try:
            return default_provider_catalog(), None
        except (
            MissingFeatureError,
            ProviderActivationError,
            ProviderContractVersionError,
        ) as error:
            return None, error

    @classmethod
    def _unavailable_catalog_error(cls, error: Exception, provider: str) -> Exception:
        """The error a selection raises when the bundled catalog could not load: ``error``
        itself, except that a ``MissingFeatureError`` is rebuilt with the requested
        ``provider`` named on it and chained from the same cause."""
        from lionagi._feature_errors import MissingFeatureError

        if not isinstance(error, MissingFeatureError):
            return error
        named = MissingFeatureError(
            feature=error.feature,
            missing_distribution=error.missing_distribution,
            install_hint=error.install_hint,
            extra=error.extra,
            requested_symbol=error.requested_symbol,
            provider=provider,
            endpoint=error.endpoint,
            reason=error.reason,
            source=error.source,
            import_root=error.name,
        )
        named.__cause__ = error.__cause__
        return named

    @classmethod
    def _match_declared(
        cls,
        catalog: ProviderCatalog,
        fragment: ProviderFragment,
        provider: str,
        endpoint: str,
        kwargs: dict[str, Any],
    ) -> Any | None:
        """Build the endpoint ``catalog`` declares for ``provider`` (whose fragment is
        ``fragment``) and ``endpoint``, importing only the selected declaration's
        implementation. ``None`` when the provider declares several endpoints and none matches
        the named one. The provider's default declaration is activated first in that case (see
        :meth:`_activate`), so a provider that cannot activate raises here rather than reaching
        the generic endpoint."""
        declaration = catalog.endpoint(provider, endpoint)
        if declaration is None:
            cls._activate(catalog, fragment, catalog.endpoint(provider))
            return None
        endpoint_cls = cls._activate(catalog, fragment, declaration)
        try:
            return endpoint_cls(None, **kwargs)
        except ModuleNotFoundError as error:
            # Constructors check their optional dependency too: an endpoint can raise this
            # for a declared import root only when it is built.
            absent = cls._absent_dependency(catalog, fragment, declaration, error)
            if absent is None:
                raise
            raise absent from error

    @classmethod
    def _activate(
        cls, catalog: ProviderCatalog, fragment: ProviderFragment, declaration: EndpointDeclaration
    ) -> type:
        """Call ``declaration``'s factory and return the endpoint class it names.

        An exception from the factory becomes the typed error :meth:`_activation_failure`
        builds. That error is kept for ``declaration``, logged once, and raised as a copy on this
        and every later activation, whether a selection or the full load makes it, so the factory
        is not called again and no traceback or exception object is shared between callers. Only
        ``Exception`` is translated; an interrupt passes through and is not kept."""
        key = id(declaration)
        kept = cls._activation_failures.get(key)
        if kept is None:
            try:
                return declaration.factory()
            except Exception as error:
                failure = cls._activation_failure(catalog, fragment, declaration, error)
            failure.with_traceback(None)
            kept = cls._activation_failures.setdefault(key, (declaration, failure))
            if kept[1] is failure:
                cls._log_activation_failure(fragment, declaration, failure)
        raise copy(kept[1], deep=False) from kept[1].__cause__

    @classmethod
    def _activation_failure(
        cls,
        catalog: ProviderCatalog,
        fragment: ProviderFragment,
        declaration: EndpointDeclaration,
        error: Exception,
    ) -> MissingFeatureError | ProviderActivationError:
        """The typed error for ``error``, raised by ``declaration``'s factory, chained from it.

        A ``MissingFeatureError`` or ``ProviderActivationError`` the factory raised itself is
        already typed and is returned as it is. A ``ModuleNotFoundError`` for a declared
        top-level import root is an absent optional dependency (``MissingFeatureError``).
        Everything else, an internal import failure, a syntax error or a defect in the factory,
        is a ``ProviderActivationError`` with reason ``activation_failed``."""
        from lionagi._feature_errors import MissingFeatureError
        from lionagi._provider_errors import ProviderActivationError

        if isinstance(error, (MissingFeatureError, ProviderActivationError)):
            return error
        absent = cls._absent_dependency(catalog, fragment, declaration, error)
        if absent is not None:
            return absent
        failure = ProviderActivationError(
            provider=fragment.key,
            endpoint=declaration.key,
            source=catalog.source,
            reason="activation_failed",
        )
        failure.__cause__ = error
        return failure

    @classmethod
    def _absent_dependency(
        cls,
        catalog: ProviderCatalog,
        fragment: ProviderFragment,
        declaration: EndpointDeclaration,
        error: BaseException,
    ) -> MissingFeatureError | None:
        """A ``MissingFeatureError`` for the dependency ``error`` reports absent, or ``None``.

        ``error`` counts only when it is a ``ModuleNotFoundError`` whose ``name`` equals, exactly,
        one of the top-level import roots a requirement of ``declaration`` lists. A module
        missing from inside the provider's own packages, or a submodule of a declared root, is
        a defect and not an absence."""
        from lionagi._feature_errors import MissingFeatureError

        if not isinstance(error, ModuleNotFoundError) or error.name is None:
            return None
        for requirement in declaration.requirements:
            if error.name in requirement.import_roots:
                absent = MissingFeatureError(
                    feature=_PROVIDERS_FEATURE,
                    missing_distribution=requirement.distribution,
                    install_hint=requirement.install_hint,
                    provider=fragment.key,
                    endpoint=declaration.key,
                    reason="missing_dependency",
                    source=catalog.source,
                    import_root=error.name,
                )
                absent.__cause__ = error
                return absent
        return None

    @classmethod
    def _log_activation_failure(
        cls,
        fragment: ProviderFragment,
        declaration: EndpointDeclaration,
        failure: MissingFeatureError | ProviderActivationError,
    ) -> None:
        """Log the first failure of one declaration. The message holds no exception text; the
        original exception is the ``__cause__`` of what selection raises."""
        from lionagi._feature_errors import MissingFeatureError

        if isinstance(failure, MissingFeatureError):
            logger.debug(
                "provider %r endpoint %r is not available: a declared dependency is not installed",
                fragment.key,
                declaration.key,
            )
            return
        logger.warning(
            "provider %r endpoint %r could not be activated (%s); selecting it raises %s",
            fragment.key,
            declaration.key,
            failure.reason,
            type(failure).__name__,
        )

    @classmethod
    def _match_catalog(
        cls, catalog: ProviderCatalog, provider: str, endpoint: str, kwargs: dict[str, Any]
    ) -> Any | None:
        """Build the endpoint ``catalog`` declares for ``provider`` and ``endpoint``: the
        declaration :meth:`ProviderCatalog.endpoint` selects. ``None`` when the catalog
        declares no such provider, or declares several endpoints for it and none matches the
        named one. The declaration's ``factory()`` runs outside any ``try``, so an exception
        it raises, ``ImportError`` included, reaches the caller instead of selecting another
        declaration."""
        declaration = catalog.endpoint(provider, endpoint)
        if declaration is None:
            return None
        return declaration.factory()(None, **kwargs)

    @classmethod
    def _generic_endpoint(
        cls,
        provider: str,
        endpoint: str,
        openai_compatible: bool,
        kwargs: dict[str, Any],
        catalog: ProviderCatalog | None,
    ) -> Any:
        """Settle a lookup no declared or registered endpoint matched: reject an unrecognized
        ``provider`` or route it to the generic OpenAI-compatible endpoint. A provider is
        recognized when a registered entry claimed it or, with ``catalog`` supplied, when that
        catalog declares it. Without ``catalog``, a recognized provider always reaches the
        generic endpoint. With ``catalog`` supplied, a provider that catalog declares raises
        ``ProviderNotFoundError`` instead, whatever ``openai_compatible`` or ``base_url``
        say: an explicitly supplied catalog is not extended by the generic endpoint."""
        if catalog is None:
            known = cls._is_known_provider(provider)
        else:
            known = catalog.fragment(provider) is not None
            if known:
                raise cls._catalog_endpoint_not_declared_error(provider, endpoint, catalog)
        if not openai_compatible and not known:
            if kwargs.get("base_url"):
                warnings.warn(
                    f"provider {provider!r} is not registered; routing to the "
                    "generic OpenAI-compatible endpoint because base_url= was "
                    "given. This implicit fallback is deprecated -- pass "
                    "openai_compatible=True explicitly (e.g. "
                    "match_endpoint(..., openai_compatible=True)) to silence "
                    "this warning.",
                    DeprecationWarning,
                    stacklevel=4,
                )
            elif catalog is None:
                raise cls._provider_not_found_error(provider)
            else:
                raise cls._catalog_not_found_error(provider, catalog)

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
    def _is_known_provider(cls, provider: str) -> bool:
        """Whether ``provider`` (case-insensitive) is a canonical provider
        name or provider-alias that some registered entry already claimed or the
        bundled provider catalog declares -- i.e. whether rejecting it as
        unrecognized would be wrong regardless of which specific endpoint was
        requested for it."""
        if provider.lower() in cls._alias_owners:
            return True
        catalog, _ = cls._bundled_catalog()
        return catalog is not None and catalog.fragment(provider) is not None

    @classmethod
    def _provider_not_found_error(cls, provider: str) -> ProviderNotFoundError:
        known: set[str] = set()
        for entry in cls._entries:
            known.add(entry.meta.provider)
            known.update(entry.meta.provider_aliases)
        catalog, _ = cls._bundled_catalog()
        if catalog is not None:
            for fragment in catalog.providers:
                known.add(fragment.key)
                known.update(fragment.aliases)
        return ProviderNotFoundError(
            f"no endpoint registered for provider {provider!r}; registered "
            f"providers: {', '.join(sorted(known)) or '(none)'}. Pass "
            "openai_compatible=True to route unrecognized providers to the "
            "generic OpenAI-compatible endpoint explicitly."
        )

    @classmethod
    def _catalog_not_found_error(
        cls, provider: str, catalog: ProviderCatalog
    ) -> ProviderNotFoundError:
        declared = ", ".join(fragment.key for fragment in catalog.providers) or "(none)"
        return ProviderNotFoundError(
            f"no endpoint declared for provider {provider!r} by the provider catalog "
            f"{catalog.source!r}, which was supplied explicitly; declared providers: "
            f"{declared}. Pass openai_compatible=True to route unrecognized providers "
            "to the generic OpenAI-compatible endpoint explicitly."
        )

    @classmethod
    def _catalog_endpoint_not_declared_error(
        cls, provider: str, endpoint: str, catalog: ProviderCatalog
    ) -> ProviderNotFoundError:
        declared = ", ".join(
            declaration.key for declaration in catalog.fragment(provider).endpoints
        )
        requested = f"endpoint {endpoint!r}" if endpoint else "a request that names no endpoint"
        return ProviderNotFoundError(
            f"the provider catalog {catalog.source!r}, which was supplied explicitly, declares "
            f"provider {provider!r} but selects none of its endpoints for {requested}; declared "
            f"endpoints: {declared}. An explicitly supplied catalog is not extended by the "
            "generic OpenAI-compatible endpoint, so openai_compatible= and base_url= do not "
            "apply."
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
        """Keep a plugin entry available only while its declared target remains
        trusted, caching the expensive `PluginRegistry.activate_target()` rescan
        behind a stat+digest fast path. See
        docs/internals/core.md#endpointregistry-plugin-revalidation."""
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
            with cls._lock:
                cls._remove_entries([entry])
            return False

        entry._validated_generation = generation
        entry._validated_stat = cls._plugin_entry_stat(entry.plugin_name, entry.plugin_target)
        entry._validated_digest = cls._plugin_entry_digest(entry.plugin_name, entry.plugin_target)
        return True

    @classmethod
    def _plugin_entry_stat(cls, plugin_name: str, target: str) -> _PluginStatSignature | None:
        """Cheap ``(mtime_ns, ctime_ns, size, inode)`` first-gate signature for the
        manifest, every declared path, and user settings -- probabilistic, not a
        correctness guarantee (see docs/internals/core.md#endpointregistry-plugin-revalidation).
        ``None`` forces the caller back onto the full ``activate_target()`` path."""
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
        """Content hashes for an entry's manifest and every declared path -- the
        correctness guarantee `_plugin_entry_stat`'s cheap signature feeds into.
        ``None`` means the plugin is unknown, the target is undeclared, or a
        covered file could not be read."""
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
                    hash_file(record.bundle_dir / relative_path, algorithm=HashAlgorithm.BLAKE2B),
                )
                for relative_path in sorted(declared_paths)
            )
        except OSError:
            return None
        return (
            compute_hash(manifest_bytes, algorithm=HashAlgorithm.BLAKE2B, max_bytes=None),
            declared_digests,
        )

    @classmethod
    def _consult_plugin_providers(cls) -> bool:
        """Import every ACTIVE plugin's declared provider module, lazily, only
        from ``match()`` after a registered-entry miss -- never at import time.
        The reentrant lock keeps activation atomic while allowing nested lookups.
        Returns whether any import succeeded."""
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
                    cls._reject_builtin_collisions(plugin_name, module)
                    imported = True
                except PluginActivationError as exc:
                    # A provider module that registers a name another provider owns fails
                    # to run, and the plugin registry reports that as an activation error.
                    if isinstance(exc.__cause__, ProviderAliasCollisionError):
                        logger.warning(
                            "plugin %r provider module %r rejected: %s",
                            plugin_name,
                            module,
                            exc.__cause__,
                        )
                    continue
                except ProviderAliasCollisionError as exc:
                    # Fail-soft, same as a built-in collision: reject only this
                    # plugin's contribution, don't crash resolution.
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
    def _reject_builtin_collisions(cls, plugin_name: str, module: str) -> None:
        """A plugin provider must never silently take over a provider name a
        built-in already serves. Drop (and log) any entry this activation just
        added whose provider name/alias matches an already-registered built-in;
        the built-in stays authoritative. Entries are identified by recorded
        provenance, not by ``__module__`` (a provider module may register a
        class defined in a helper module)."""
        builtin_names: set[str] = set()
        for entry in cls._entries:
            if entry.plugin_name is None:
                builtin_names.add(entry.meta.provider)
                builtin_names.update(entry.meta.provider_aliases)
        if not builtin_names:
            return

        rejected: list[_RegistryEntry] = []
        for entry in cls._entries:
            is_this_activation = entry.plugin_name == plugin_name and entry.plugin_target == module
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
                rejected.append(entry)
        cls._remove_entries(rejected)

    @classmethod
    def _ensure_loaded(cls):
        """Import every declared provider implementation, once. Only the methods whose result
        needs every built-in class (``list_providers``, ``failed_providers``) call this; a
        selection through :meth:`match` and :meth:`canonical_provider` activate only what they
        need and never do."""
        if cls._loaded:
            return
        with cls._lock:
            if cls._loaded:
                return
            from lionagi._feature_errors import MissingFeatureError
            from lionagi._provider_errors import (
                ProviderActivationError,
                ProviderContractVersionError,
            )

            from ._provider_compat import default_provider_catalog

            # Built-in providers register by importing the module each declaration of the
            # bundled provider catalog names; importing a provider module runs its
            # registration decorators. A catalog that cannot load leaves no built-in
            # provider registered, and one warning says why. Each declaration is activated
            # the way a selection activates it, so its failure is translated to the typed
            # error a selection raises, kept per declaration and logged once there. A failed
            # declaration does not stop the provider's other declarations or the other
            # providers from loading, so the rows that follow do not depend on which
            # declarations a selection activated first. A provider is recorded once, with
            # the error of its first failed declaration.
            failed: list[_FailedProvider] = []
            try:
                catalog = default_provider_catalog()
            except (
                MissingFeatureError,
                ProviderActivationError,
                ProviderContractVersionError,
            ) as error:
                logger.warning(
                    "built-in providers were not registered: "
                    "the provider catalog could not load: %s: %s",
                    type(error).__name__,
                    error,
                )
            else:
                for provider in catalog.providers:
                    first_failure = None
                    for declaration in provider.endpoints:
                        try:
                            cls._activate(catalog, provider, declaration)
                        except (MissingFeatureError, ProviderActivationError) as error:
                            if first_failure is None:
                                first_failure = error.with_traceback(None)
                    if first_failure is not None:
                        failed.append(
                            _FailedProvider(
                                provider=provider.key,
                                names=(provider.key, *provider.aliases),
                                error=first_failure,
                            )
                        )
            # Test-only scripted provider (provider="scripted"): a leaf core module,
            # always loadable, gated behind LIONAGI_CHAT_PROVIDER=scripted. Imported after
            # the declared providers so it stays last among the registered entries. A lookup
            # no declared provider answered may have registered it earlier; its entries move
            # behind everything else registered since, as a cold load leaves them.
            _import_provider_module(_SCRIPTED_MODULE)
            scripted = [e for e in cls._entries if e.cls.__module__ == _SCRIPTED_MODULE]
            if scripted:
                moved = {id(e) for e in scripted}
                cls._entries[:] = [e for e in cls._entries if id(e) not in moved] + scripted
            cls._failed = tuple(failed)
            cls._loaded = True

    @classmethod
    def canonical_provider(cls, provider: str) -> str:
        """The provider a name or provider-alias resolves to when matched
        ("openai-codex" -> "codex", "claude" -> "claude_code"); a name no entry
        claims comes back folded the same way. The name is folded as the match
        folds it, letter case only, so a padded name comes back padded and names
        no provider, as the match finds none for it. A provider the bundled
        provider catalog declares resolves from its declaration, without importing
        an endpoint implementation; any other name resolves from the registered
        entries' claims."""
        key = provider.lower()
        catalog, _ = cls._bundled_catalog()
        if catalog is not None:
            fragment = catalog.fragment(key)
            if fragment is not None:
                return fragment.key
        return cls._alias_owners.get(key, key)

    @classmethod
    def list_providers(cls) -> list[dict[str, Any]]:
        """One row per registered endpoint, after every declared provider has been imported.

        The rows of the endpoints the bundled catalog declares come first, in the catalog's
        order whichever providers were selected before. An entry is a declared endpoint's when
        its class has the module and qualified name of that declaration's ``diagnostic_id``,
        the rule :meth:`_refuse_declared_provider` uses. The rows of every other entry follow
        in the order they registered in, with the test-only scripted provider's behind the
        entries registered before the load. Without a bundled catalog every row keeps its
        registration order."""
        cls._ensure_loaded()
        entries = list(cls._entries)
        catalog, _ = cls._bundled_catalog()
        if catalog is not None:
            position = {
                declaration.diagnostic_id: index
                for index, declaration in enumerate(
                    d for fragment in catalog.providers for d in fragment.endpoints
                )
            }

            def implementation(entry: _RegistryEntry) -> str:
                return f"{entry.cls.__module__}:{entry.cls.__qualname__}"

            entries = sorted(
                (e for e in entries if implementation(e) in position),
                key=lambda e: position[implementation(e)],
            ) + [e for e in entries if implementation(e) not in position]
        return [
            {
                "provider": e.meta.provider,
                "endpoint": e.meta.endpoint,
                "aliases": list(e.meta.aliases),
                "type": e.meta.endpoint_type.value,
                "class": e.cls.__name__,
                "options": e.meta.options.__name__ if e.meta.options else None,
            }
            for e in entries
        ]

    @classmethod
    def failed_providers(cls) -> list[dict[str, Any]]:
        """One row per provider the bundled provider catalog declares whose activation failed
        when the registry loaded, in catalog order whichever providers were selected before;
        empty when none failed. Each row has the ``provider`` key, the ``endpoint`` key the
        failure was raised for (or ``None``), the ``source`` of the declaration and the
        ``reason``. The failure is the error a selection of that endpoint raises, a
        ``ProviderActivationError`` or a ``MissingFeatureError``, and a row carries no
        exception text."""
        cls._ensure_loaded()
        return [
            {
                "provider": failed.provider,
                "endpoint": failed.error.endpoint,
                "source": failed.error.source,
                "reason": failed.error.reason,
            }
            for failed in cls._failed
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


# Declared, not inferred: the optional third-party dependency each module
# imported through ``_import_provider_module`` needs, keyed by dotted module
# path. A module with no entry here has none; add one the day such a module
# imports its optional dependency at module scope (none does today).
_PROVIDER_OPTIONAL_DEPENDENCIES: dict[str, tuple[str, ...]] = {}


def _import_provider_module(mod: str) -> None:
    """Preflight-check ``mod``'s declared optional dependencies, then import.
    A missing dependency skips the import entirely (debug log, module body
    never runs); once dependencies resolve, any ``ImportError`` is
    unconditionally a provider-load failure (warning)."""
    missing = [
        dep for dep in _PROVIDER_OPTIONAL_DEPENDENCIES.get(mod, ()) if not is_import_installed(dep)
    ]
    if missing:
        logger.debug(
            "provider module %r not registered: optional dependency %r is not installed",
            mod,
            missing[0],
        )
        return
    try:
        import_module(mod)
    except ImportError as e:
        logger.warning(
            "provider module %r failed to import and was not registered: %s",
            mod,
            e,
        )
