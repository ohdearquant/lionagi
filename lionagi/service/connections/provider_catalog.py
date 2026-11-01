# SPDX-License-Identifier: Apache-2.0

"""Provider catalog: immutable provider declarations and their composition.

A provider declares itself with a :class:`ProviderFragment`; a set of fragments is composed
into one immutable :class:`ProviderCatalog` by :func:`compose_provider_catalog`. The module
is provider-neutral and imports only the standard library and the provider contract errors:
it never imports an endpoint implementation or the endpoint registry, and it never calls an
endpoint factory.

Key rules mirror the ones ``EndpointRegistry.register`` applies today: a provider key and its
aliases are stored as ``strip().lower()`` with repeated aliases dropped, a provider is looked
up by its lowercased name only (padded names do not match), and endpoint names are compared
exactly against an endpoint key or one of its aliases. Like the registry, an empty endpoint
name selects a default (here the provider's ``default_endpoint``) and an unmatched name
selects the provider's only endpoint when it declares exactly one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from lionagi._provider_errors import ProviderCollisionError, ProviderContractVersionError

__all__ = (
    "SUPPORTED_CONTRACT_VERSIONS",
    "DependencyRequirement",
    "EndpointDeclaration",
    "ProviderCatalog",
    "ProviderFragment",
    "ProviderManifest",
    "compose_provider_catalog",
)

SUPPORTED_CONTRACT_VERSIONS: tuple[int, ...] = (1,)
"""Contract versions this module can compose; read at composition time."""

_ENDPOINT_KINDS: tuple[str, ...] = ("api", "agentic")


# --- shape helpers -------------------------------------------------------------


def _check_str(owner: str, name: str, value: object) -> None:
    """Require a non-blank ``str``; raise ``TypeError`` or ``ValueError`` naming the field."""
    if not isinstance(value, str):
        raise TypeError(f"{owner}.{name} must be a str; got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{owner}.{name} must not be empty")


def _items(owner: str, name: str, value: object) -> tuple[Any, ...]:
    """Return the items of a sequence-like value; a bare string or a mapping is rejected."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise TypeError(
            f"{owner}.{name} must be a tuple or list of values; got {type(value).__name__}"
        )
    return tuple(value)


def _str_tuple(owner: str, name: str, value: object) -> tuple[str, ...]:
    """Return ``value`` as a tuple of non-blank strings."""
    items = _items(owner, name, value)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{owner}.{name} must contain only str; got {type(item).__name__}")
        if not item.strip():
            raise ValueError(f"{owner}.{name} must not contain an empty string")
    return items


def _typed_tuple(owner: str, name: str, value: object, kind: type) -> tuple[Any, ...]:
    """Return ``value`` as a tuple whose items are all instances of ``kind``."""
    items = _items(owner, name, value)
    for item in items:
        if not isinstance(item, kind):
            raise TypeError(
                f"{owner}.{name} must contain only {kind.__name__} instances; "
                f"got {type(item).__name__}"
            )
    return items


def _frozen_mapping(owner: str, name: str, value: object) -> Mapping[str, Any]:
    """Return a read-only view over a shallow copy of a mapping with non-blank ``str`` keys."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner}.{name} must be a mapping; got {type(value).__name__}")
    copy = dict(value)
    for key in copy:
        if not isinstance(key, str):
            raise TypeError(f"{owner}.{name} keys must be str; got {type(key).__name__}")
        if not key.strip():
            raise ValueError(f"{owner}.{name} must not contain an empty key")
    return MappingProxyType(copy)


def _normalize_key(value: str) -> str:
    """Normalize a provider key or alias the way ``EndpointRegistry.register`` stores it."""
    return value.strip().lower()


# --- declaration types ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DependencyRequirement:
    """One distribution an endpoint needs, and how to tell the user to install it.

    ``import_roots`` are the top-level module names the distribution provides;
    ``version_constraint`` is a requirement specifier or ``None`` for any version.
    """

    distribution: str
    import_roots: tuple[str, ...]
    version_constraint: str | None
    install_hint: str

    def __post_init__(self) -> None:
        _check_str("DependencyRequirement", "distribution", self.distribution)
        object.__setattr__(
            self,
            "import_roots",
            _str_tuple("DependencyRequirement", "import_roots", self.import_roots),
        )
        if self.version_constraint is not None and not isinstance(self.version_constraint, str):
            raise TypeError(
                "DependencyRequirement.version_constraint must be a str or None; "
                f"got {type(self.version_constraint).__name__}"
            )
        if not isinstance(self.install_hint, str):
            raise TypeError(
                "DependencyRequirement.install_hint must be a str; "
                f"got {type(self.install_hint).__name__}"
            )


@dataclass(frozen=True, slots=True)
class EndpointDeclaration:
    """One endpoint a provider offers.

    ``kind`` is ``"api"`` or ``"agentic"``. ``factory`` takes no arguments and returns the
    endpoint; the catalog stores it and never calls it. ``settings`` is the settings
    specification, kept as a read-only view over a shallow copy of the mapping given.
    """

    key: str
    aliases: tuple[str, ...]
    kind: str
    factory: Callable[[], Any]
    requirements: tuple[DependencyRequirement, ...]
    settings: Mapping[str, object]
    diagnostic_id: str

    def __post_init__(self) -> None:
        _check_str("EndpointDeclaration", "key", self.key)
        object.__setattr__(
            self, "aliases", _str_tuple("EndpointDeclaration", "aliases", self.aliases)
        )
        if self.kind not in _ENDPOINT_KINDS:
            raise ValueError(
                f"EndpointDeclaration.kind must be one of {', '.join(_ENDPOINT_KINDS)}; "
                f"got {self.kind!r}"
            )
        if not callable(self.factory):
            raise TypeError("EndpointDeclaration.factory must be callable")
        object.__setattr__(
            self,
            "requirements",
            _typed_tuple(
                "EndpointDeclaration", "requirements", self.requirements, DependencyRequirement
            ),
        )
        object.__setattr__(
            self, "settings", _frozen_mapping("EndpointDeclaration", "settings", self.settings)
        )
        _check_str("EndpointDeclaration", "diagnostic_id", self.diagnostic_id)


@dataclass(frozen=True, slots=True)
class ProviderFragment:
    """One provider: its key and aliases, its endpoints and its context-window metadata.

    ``default_endpoint`` is the key of one of ``endpoints``. ``context_windows`` maps a model
    name fragment to a positive token count; ``None`` states that the provider's window
    metadata is unknown. ``contract_version`` is stored as given: composition and catalog
    construction check it.
    """

    contract_version: object
    key: str
    aliases: tuple[str, ...]
    endpoints: tuple[EndpointDeclaration, ...]
    default_endpoint: str
    context_windows: Mapping[str, int] | None

    def __post_init__(self) -> None:
        _check_str("ProviderFragment", "key", self.key)
        object.__setattr__(self, "aliases", _str_tuple("ProviderFragment", "aliases", self.aliases))
        endpoints = _typed_tuple(
            "ProviderFragment", "endpoints", self.endpoints, EndpointDeclaration
        )
        if not endpoints:
            raise ValueError("ProviderFragment.endpoints must not be empty")
        object.__setattr__(self, "endpoints", endpoints)
        _check_str("ProviderFragment", "default_endpoint", self.default_endpoint)
        if self.default_endpoint not in {endpoint.key for endpoint in endpoints}:
            raise ValueError(
                f"ProviderFragment.default_endpoint {self.default_endpoint!r} "
                "names none of the endpoint keys"
            )
        if self.context_windows is not None:
            windows = _frozen_mapping("ProviderFragment", "context_windows", self.context_windows)
            for model, window in windows.items():
                if isinstance(window, bool) or not isinstance(window, int):
                    raise TypeError(
                        f"ProviderFragment.context_windows[{model!r}] must be a positive int; "
                        f"got {type(window).__name__}"
                    )
                if window < 1:
                    raise ValueError(
                        f"ProviderFragment.context_windows[{model!r}] must be a positive int; "
                        f"got {window}"
                    )
            object.__setattr__(self, "context_windows", windows)


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    """A named set of provider fragments sharing one contract version.

    ``source`` is the stable identity of whoever supplied the fragments; composition errors
    carry it. ``contract_version`` is stored as given: composition checks it.
    """

    contract_version: object
    source: str
    fragments: tuple[ProviderFragment, ...]

    def __post_init__(self) -> None:
        _check_str("ProviderManifest", "source", self.source)
        object.__setattr__(
            self,
            "fragments",
            _typed_tuple("ProviderManifest", "fragments", self.fragments, ProviderFragment),
        )


# --- composition ---------------------------------------------------------------


def _provider_owner(fragment: ProviderFragment, position: int, source: str) -> str:
    """Name a provider declaration by its key, its position in the input and its source."""
    return f"{_normalize_key(fragment.key)} #{position} ({source})"


def _endpoint_owner(
    fragment: ProviderFragment, endpoint: EndpointDeclaration, position: int, source: str
) -> str:
    """Name an endpoint declaration by provider, endpoint key, position in its provider, source."""
    return f"{_normalize_key(fragment.key)}/{endpoint.key} #{position} ({source})"


def _reject_repeated_declarations(providers: tuple[ProviderFragment, ...], source: str) -> None:
    """Raise when one declaration object appears twice in the input."""
    seen_fragments: dict[int, str] = {}
    for position, fragment in enumerate(providers):
        owner = _provider_owner(fragment, position, source)
        first = seen_fragments.get(id(fragment))
        if first is not None:
            raise ProviderCollisionError(
                key=_normalize_key(fragment.key),
                kind="duplicate_declaration",
                first_owner=first,
                second_owner=owner,
            )
        seen_fragments[id(fragment)] = owner
    seen_endpoints: dict[int, str] = {}
    for fragment in providers:
        for position, endpoint in enumerate(fragment.endpoints):
            owner = _endpoint_owner(fragment, endpoint, position, source)
            first = seen_endpoints.get(id(endpoint))
            if first is not None:
                raise ProviderCollisionError(
                    key=endpoint.key,
                    kind="duplicate_declaration",
                    first_owner=first,
                    second_owner=owner,
                )
            seen_endpoints[id(endpoint)] = owner


def _index_providers(
    providers: tuple[ProviderFragment, ...], source: str
) -> dict[str, ProviderFragment]:
    """Map every normalized provider key and alias to its fragment, refusing any collision.

    Keys and aliases share one namespace. A declaration repeating its own alias, or using its
    own key as an alias, claims the name once.
    """
    claims: dict[str, tuple[int, bool, str]] = {}
    by_name: dict[str, ProviderFragment] = {}
    for position, fragment in enumerate(providers):
        owner = _provider_owner(fragment, position, source)
        names = [(_normalize_key(fragment.key), True)]
        names.extend((_normalize_key(alias), False) for alias in fragment.aliases)
        for name, is_key in names:
            held = claims.get(name)
            if held is None:
                claims[name] = (position, is_key, owner)
                by_name[name] = fragment
            elif held[0] != position:
                raise ProviderCollisionError(
                    key=name,
                    kind="provider" if is_key and held[1] else "provider_alias",
                    first_owner=held[2],
                    second_owner=owner,
                )
    return by_name


def _index_endpoints(fragment: ProviderFragment, source: str) -> dict[str, EndpointDeclaration]:
    """Map every endpoint key and alias of one provider to its declaration.

    Endpoint keys and aliases share one namespace per provider and are matched exactly.
    """
    claims: dict[str, tuple[int, str]] = {}
    index: dict[str, EndpointDeclaration] = {}
    for position, endpoint in enumerate(fragment.endpoints):
        owner = _endpoint_owner(fragment, endpoint, position, source)
        for name in dict.fromkeys((endpoint.key, *endpoint.aliases)):
            held = claims.get(name)
            if held is None:
                claims[name] = (position, owner)
                index[name] = endpoint
            elif held[0] != position:
                raise ProviderCollisionError(
                    key=name, kind="endpoint", first_owner=held[1], second_owner=owner
                )
    return index


def _longest_match(model_lower: str, windows: Mapping[str, int]) -> int | None:
    """Window of the longest key contained in ``model_lower``; the first declared key wins ties."""
    best: int | None = None
    best_length = 0
    for key, window in windows.items():
        if key in model_lower and len(key) > best_length:
            best = window
            best_length = len(key)
    return best


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    """An immutable, validated set of provider fragments from one source.

    Build it with :func:`compose_provider_catalog`. Construction, by either route, refuses a
    fragment whose contract version is not supported (``ProviderContractVersionError``) and
    any key collision, so a catalog never holds an unsupported fragment or two claimants for
    one name; the version check comes first. ``providers`` keeps the order the fragments were
    declared in; two catalogs composed from equal input compare equal.
    """

    providers: tuple[ProviderFragment, ...]
    source: str
    _providers_by_name: Mapping[str, ProviderFragment] = field(
        init=False, repr=False, compare=False
    )
    _endpoints_by_provider: Mapping[str, Mapping[str, EndpointDeclaration]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _check_str("ProviderCatalog", "source", self.source)
        providers = _typed_tuple("ProviderCatalog", "providers", self.providers, ProviderFragment)
        for fragment in providers:
            _require_supported_version(self.source, fragment.contract_version)
        object.__setattr__(self, "providers", providers)
        _reject_repeated_declarations(providers, self.source)
        by_name = _index_providers(providers, self.source)
        endpoints = {
            _normalize_key(fragment.key): MappingProxyType(_index_endpoints(fragment, self.source))
            for fragment in providers
        }
        object.__setattr__(self, "_providers_by_name", MappingProxyType(by_name))
        object.__setattr__(self, "_endpoints_by_provider", MappingProxyType(endpoints))

    def fragment(self, key_or_alias: str) -> ProviderFragment | None:
        """Return the fragment whose key or alias equals the lowercased name, else ``None``."""
        return self._providers_by_name.get(key_or_alias.lower())

    def endpoint(
        self, provider: str, endpoint_key_or_alias: str | None = None
    ) -> EndpointDeclaration | None:
        """Return one endpoint of a provider, or ``None`` when no endpoint is selected.

        ``provider`` is matched like :meth:`fragment`. The endpoint name follows the matching
        ``EndpointRegistry`` applies to a provider's registered endpoints, with the provider's
        ``default_endpoint`` standing in for its first registered one:

        * ``None`` or ``""`` selects the provider's default endpoint.
        * A non-empty name equal to an endpoint key or alias, compared exactly, selects that
          endpoint.
        * Any other non-empty name selects the provider's only endpoint when it declares
          exactly one, and gives ``None`` otherwise.
        """
        fragment = self.fragment(provider)
        if fragment is None:
            return None
        endpoints = self._endpoints_by_provider[_normalize_key(fragment.key)]
        if not endpoint_key_or_alias:
            return endpoints[fragment.default_endpoint]
        matched = endpoints.get(endpoint_key_or_alias)
        if matched is None and len(fragment.endpoints) == 1:
            return fragment.endpoints[0]
        return matched

    def context_window(self, model: str, provider: str | None = None) -> int | None:
        """Return the context window declared for ``model``, or ``None`` when none matches.

        The window of the longest key contained in the lowercased model name wins, and the
        first declared key wins a tie. The hinted provider's mapping is tried first, then
        every provider's mapping in declared order; a provider whose ``context_windows`` is
        ``None`` contributes nothing. No default is ever returned: a caller that needs an
        estimate keeps its own.
        """
        model_lower = model.lower()
        ordered: list[ProviderFragment] = []
        hinted = self.fragment(provider) if provider else None
        if hinted is not None:
            ordered.append(hinted)
        ordered.extend(fragment for fragment in self.providers if fragment is not hinted)
        for fragment in ordered:
            if fragment.context_windows:
                window = _longest_match(model_lower, fragment.context_windows)
                if window is not None:
                    return window
        return None


def _require_supported_version(source: str, version: object) -> None:
    """Raise ``ProviderContractVersionError`` unless ``version`` is a supported ``int``."""
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_CONTRACT_VERSIONS
    ):
        raise ProviderContractVersionError(
            source=source, expected=SUPPORTED_CONTRACT_VERSIONS, observed=version
        )


def compose_provider_catalog(
    fragments: Iterable[ProviderFragment] | ProviderManifest, *, source: str
) -> ProviderCatalog:
    """Compose provider fragments into one immutable catalog.

    ``fragments`` is an iterable of :class:`ProviderFragment` or a :class:`ProviderManifest`;
    for a manifest, ``source`` must equal the manifest's own source and the manifest's
    version must agree with every fragment's. Every declared contract version is checked
    first, ahead of the source comparison, and nothing else about the declarations is
    examined until all of them pass: a missing, non-``int``, ``bool`` or unsupported version
    raises ``ProviderContractVersionError``, and so does a fragment whose version differs
    from its manifest's. Then a ``source`` that differs from the manifest's raises
    ``ValueError``, the keys are indexed and any collision raises ``ProviderCollisionError``
    naming both claimants. The result never depends on declaration order beyond
    ``providers`` keeping it, and an empty input gives a valid empty catalog.
    """
    _check_str("compose_provider_catalog", "source", source)
    manifest: ProviderManifest | None = None
    if isinstance(fragments, ProviderManifest):
        manifest = fragments
        declared = manifest.fragments
    else:
        declared = _typed_tuple(
            "compose_provider_catalog", "fragments", fragments, ProviderFragment
        )

    if manifest is not None:
        _require_supported_version(source, manifest.contract_version)
    for fragment in declared:
        _require_supported_version(source, fragment.contract_version)
    if manifest is not None:
        for fragment in declared:
            if fragment.contract_version != manifest.contract_version:
                raise ProviderContractVersionError(
                    source=source,
                    expected=(manifest.contract_version,),
                    observed=fragment.contract_version,
                )
        if manifest.source != source:
            raise ValueError(
                f"source {source!r} does not match the manifest source {manifest.source!r}"
            )

    return ProviderCatalog(providers=declared, source=source)
