# SPDX-License-Identifier: Apache-2.0

"""Endpoint selection from an explicitly supplied provider catalog.

``EndpointRegistry.match`` and ``match_endpoint`` accept ``provider_catalog=``. A supplied
catalog is the only source of declarations: automatic loading, plugin providers and the
process registry are bypassed, and an empty catalog is distinct from omitting the argument.
Selection follows the rules the automatic path applies to registered entries, with the
catalog's declarations, in declaration order, in their place.
"""

from __future__ import annotations

import warnings

import pytest

from lionagi.service.connections import _provider_compat
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.provider_catalog import (
    EndpointDeclaration,
    ProviderFragment,
    compose_provider_catalog,
)
from lionagi.service.connections.registry import (
    EndpointRegistry,
    ProviderNotFoundError,
    register_endpoint,
)
from lionhub.providers.manifest import get_provider_manifest


@pytest.fixture(autouse=True)
def _isolate_endpoint_registry():
    """Snapshot and restore registry state so test-only registrations never leak."""
    EndpointRegistry._ensure_loaded()
    saved_entries = list(EndpointRegistry._entries)
    saved_loaded = EndpointRegistry._loaded
    saved_alias_owners = dict(EndpointRegistry._alias_owners)
    yield
    EndpointRegistry._entries = saved_entries
    EndpointRegistry._loaded = saved_loaded
    EndpointRegistry._alias_owners = saved_alias_owners


@pytest.fixture(scope="module")
def manifest_catalog():
    """The built-in provider manifest composed into a catalog; loads no implementation."""
    return compose_provider_catalog(get_provider_manifest())


@pytest.fixture
def no_plugin_providers(monkeypatch):
    """Keep the automatic path from consulting installed plugins on a registry miss."""
    monkeypatch.setattr(
        EndpointRegistry, "_consult_plugin_providers", classmethod(lambda cls: False)
    )


@pytest.fixture
def automatic_source_forbidden(monkeypatch):
    """Make every entry point of the automatic source raise when it is reached."""

    def refuse(*args, **kwargs):
        raise AssertionError("a supplied catalog must not reach the automatic source")

    monkeypatch.setattr(EndpointRegistry, "_ensure_loaded", classmethod(refuse))
    monkeypatch.setattr(EndpointRegistry, "_consult_plugin_providers", classmethod(refuse))
    monkeypatch.setattr(_provider_compat, "default_provider_catalog", refuse)


class _ChatEndpoint:
    """Endpoint stand-in that records how the catalog path constructs it."""

    def __init__(self, config, **kwargs):
        self.config = config
        self.kwargs = kwargs


class _EmbedEndpoint(_ChatEndpoint):
    pass


class _OnlyEndpoint(_ChatEndpoint):
    pass


def _declaration(key, factory, aliases=()):
    return EndpointDeclaration(
        key=key,
        aliases=aliases,
        kind="api",
        factory=factory,
        requirements=(),
        settings={},
        diagnostic_id=f"test.{key}",
    )


def _fragment(key, endpoints, default_endpoint, aliases=()):
    return ProviderFragment(
        contract_version=1,
        key=key,
        aliases=aliases,
        endpoints=tuple(endpoints),
        default_endpoint=default_endpoint,
        context_windows=None,
    )


def _acme_catalog():
    """One provider with two endpoints; the declared default is the second one."""
    return compose_provider_catalog(
        [
            _fragment(
                "acme",
                [
                    _declaration("chat", lambda: _ChatEndpoint, aliases=("c",)),
                    _declaration("embed", lambda: _EmbedEndpoint),
                ],
                "embed",
                aliases=("acme-ai",),
            )
        ],
        source="catalog-one",
    )


def _solo_catalog():
    """One provider with exactly one endpoint."""
    return compose_provider_catalog(
        [_fragment("solo", [_declaration("only", lambda: _OnlyEndpoint)], "only")],
        source="catalog-two",
    )


def _unimportable():
    raise ImportError("declared implementation is not importable")


def _broken_catalog():
    """A provider whose default endpoint cannot be imported but whose second one can."""
    return compose_provider_catalog(
        [
            _fragment(
                "broken",
                [
                    _declaration("chat", _unimportable),
                    _declaration("embed", lambda: _EmbedEndpoint),
                ],
                "chat",
            )
        ],
        source="catalog-three",
    )


def _outcome(provider, endpoint="", **kwargs):
    """What one ``EndpointRegistry.match`` call yields, as a comparable tuple.

    ``("selected", class, provider, endpoint, categories)`` carries the routing of the
    selected endpoint (set for the generic endpoint); ``("raises", exception type, None,
    None, categories)`` records a raised exception. ``categories`` is the tuple of warning
    categories the call emitted.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            selected = EndpointRegistry.match(provider, endpoint, **kwargs)
        except Exception as error:
            result = ("raises", type(error), None, None)
        else:
            config = getattr(selected, "config", None)
            result = (
                "selected",
                type(selected),
                getattr(config, "provider", None),
                getattr(config, "endpoint", None),
            )
    return (*result, tuple(warning.category for warning in caught))


def _combinations(catalog):
    """Provider names crossed with endpoint names, for every provider the catalog declares.

    A provider name is its key or an alias, written as declared and upper-cased; the name
    padded with spaces is tried with the empty endpoint. An endpoint name is each declared
    endpoint key and alias, the empty endpoint, ``"chat"`` and a name nothing declares.
    """
    combos = []
    for fragment in catalog.providers:
        endpoints = ["", "chat", "no-such-endpoint"]
        for declaration in fragment.endpoints:
            endpoints.extend((declaration.key, *declaration.aliases))
        endpoints = list(dict.fromkeys(endpoints))
        for name in (fragment.key, *fragment.aliases):
            for provider in (name, name.upper()):
                combos.extend((provider, endpoint) for endpoint in endpoints)
            combos.append((f" {name} ", ""))
    return combos


def test_explicit_selection_matches_the_automatic_path(manifest_catalog, no_plugin_providers):
    combos = _combinations(manifest_catalog)
    automatic = {combo: _outcome(*combo) for combo in combos}
    assert any(outcome[0] == "selected" for outcome in automatic.values())

    # From here the process registry is empty: the explicit path must not read it.
    EndpointRegistry._entries = []
    EndpointRegistry._alias_owners = {}
    explicit = {combo: _outcome(*combo, provider_catalog=manifest_catalog) for combo in combos}

    differing = {
        combo: (automatic[combo], explicit[combo])
        for combo in automatic
        if automatic[combo] != explicit[combo]
    }
    assert not differing, (
        f"{len(differing)} of {len(automatic)} selections differ; "
        f"first: {next(iter(differing.items()))}"
    )


@pytest.mark.parametrize(
    ("options", "kind", "result_type", "warns"),
    [
        ({"openai_compatible": True}, "selected", Endpoint, False),
        ({"base_url": "http://localhost:9"}, "selected", Endpoint, True),
        ({}, "raises", ProviderNotFoundError, False),
    ],
    ids=["openai-compatible", "base-url", "neither"],
)
def test_undeclared_provider_follows_the_automatic_path(
    manifest_catalog, no_plugin_providers, options, kind, result_type, warns
):
    automatic = _outcome("no-such-provider", "chat", **options)
    explicit = _outcome("no-such-provider", "chat", provider_catalog=manifest_catalog, **options)

    assert explicit == automatic
    assert explicit[:2] == (kind, result_type)
    assert (DeprecationWarning in explicit[4]) is warns


def test_manifest_catalog_selects_without_the_automatic_source(
    manifest_catalog, automatic_source_forbidden
):
    for provider, endpoint in (("openai", "chat"), ("openai", ""), ("anthropic", "")):
        declaration = manifest_catalog.endpoint(provider, endpoint)
        assert declaration is not None
        selected = EndpointRegistry.match(provider, endpoint, provider_catalog=manifest_catalog)
        assert type(selected) is declaration.factory()


def test_empty_catalog_is_not_omission(automatic_source_forbidden):
    empty = compose_provider_catalog((), source="catalog-empty")
    with pytest.raises(ProviderNotFoundError, match="catalog-empty"):
        EndpointRegistry.match("openai", "chat", provider_catalog=empty)


def test_omitted_catalog_keeps_the_automatic_selection(manifest_catalog):
    expected = manifest_catalog.endpoint("openai", "chat").factory()

    assert type(EndpointRegistry.match("openai", "chat")) is expected
    assert type(EndpointRegistry.match("openai", "chat", provider_catalog=None)) is expected


def test_registered_provider_absent_from_the_catalog_is_not_selected():
    @register_endpoint(provider="registry-only", endpoint="chat")
    class _RegistryOnly(Endpoint):
        pass

    assert "registry-only" in EndpointRegistry._alias_owners
    with pytest.raises(ProviderNotFoundError, match="catalog-one"):
        EndpointRegistry.match("registry-only", "chat", provider_catalog=_acme_catalog())


@pytest.mark.parametrize(
    ("provider", "endpoint", "expected"),
    [
        ("acme", "chat", _ChatEndpoint),
        ("acme", "c", _ChatEndpoint),
        ("acme-ai", "embed", _EmbedEndpoint),
        ("ACME-AI", "c", _ChatEndpoint),
        ("acme", "", _EmbedEndpoint),
    ],
    ids=["key", "endpoint-alias", "provider-alias", "provider-case", "declared-default"],
)
def test_selects_a_declared_endpoint_and_passes_kwargs(provider, endpoint, expected):
    selected = EndpointRegistry.match(
        provider, endpoint, provider_catalog=_acme_catalog(), model="m", timeout=3
    )

    assert type(selected) is expected
    assert selected.config is None
    assert selected.kwargs == {"model": "m", "timeout": 3}


@pytest.mark.parametrize("endpoint", ["", "only", "anything-else"])
def test_single_endpoint_provider_is_selected_whatever_endpoint_is_named(endpoint):
    selected = EndpointRegistry.match("solo", endpoint, provider_catalog=_solo_catalog())

    assert type(selected) is _OnlyEndpoint


def test_declared_provider_with_an_unmatched_endpoint_gets_the_generic_endpoint():
    selected = EndpointRegistry.match("acme", "nonesuch", provider_catalog=_acme_catalog())

    assert type(selected) is Endpoint
    assert selected.config.provider == "acme"
    assert selected.config.endpoint == "nonesuch"


def test_undeclared_provider_error_names_the_catalog():
    with pytest.raises(ProviderNotFoundError) as raised:
        EndpointRegistry.match("nonesuch", "chat", provider_catalog=_acme_catalog())

    message = str(raised.value)
    assert "catalog-one" in message
    assert "acme" in message
    assert "supplied explicitly" in message
    assert "regist" not in message.lower()
    assert "install" not in message.lower()


@pytest.mark.parametrize("endpoint", ["chat", ""])
def test_import_error_from_the_selected_factory_propagates(endpoint):
    catalog = _broken_catalog()

    with pytest.raises(ImportError, match="not importable"):
        EndpointRegistry.match("broken", endpoint, provider_catalog=catalog)
    selected = EndpointRegistry.match("broken", "embed", provider_catalog=catalog)
    assert type(selected) is _EmbedEndpoint


def test_match_endpoint_passes_the_catalog_through():
    selected = match_endpoint("acme", "embed", provider_catalog=_acme_catalog(), model="m")

    assert type(selected) is _EmbedEndpoint
    assert selected.kwargs == {"model": "m"}
    with pytest.raises(ProviderNotFoundError, match="catalog-one"):
        match_endpoint("nonesuch", "chat", provider_catalog=_acme_catalog())
