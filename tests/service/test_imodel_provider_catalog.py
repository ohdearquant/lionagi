# SPDX-License-Identifier: Apache-2.0

"""``iModel(provider_catalog=...)`` and the token budget that follows it.

A model built with a provider catalog selects its endpoint from that catalog only, routes an
effort-suffixed model name through the catalog's own provider keys, carries the catalog through
``copy`` and ``from_dict``, and reads its context window from it. A catalog that declares the
provider but not the requested endpoint refuses it, at construction and in ``from_dict`` alike.
In every test that supplies a catalog the automatic source is patched to raise, so a hidden
automatic load fails the test.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
from types import SimpleNamespace

import pytest

from lionagi.service.connections import _provider_compat
from lionagi.service.connections.endpoint import Endpoint, EndpointConfig
from lionagi.service.connections.provider_catalog import (
    EndpointDeclaration,
    ProviderFragment,
    compose_provider_catalog,
)
from lionagi.service.connections.registry import EndpointRegistry, ProviderNotFoundError
from lionagi.service.imodel import iModel
from lionagi.service.token_budget import get_context_window
from lionhub.providers.manifest import get_provider_manifest

_API_KEY = "test-key"
_UNUSUAL_MODEL = "unusual-test-model"
_UNUSUAL_WINDOW = 12345


@pytest.fixture(scope="module")
def manifest_catalog():
    """The built-in provider manifest composed into a catalog; loads no implementation."""
    return compose_provider_catalog(get_provider_manifest())


@pytest.fixture
def empty_catalog():
    return compose_provider_catalog((), source="catalog-empty")


@pytest.fixture
def forbid_automatic_source(monkeypatch):
    """Return a callable that makes every way into the automatic source raise.

    Call it after building any baseline that is meant to use the automatic source.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("a supplied catalog must not reach the automatic source")

    def apply() -> None:
        monkeypatch.setattr(EndpointRegistry, "_ensure_loaded", classmethod(refuse))
        monkeypatch.setattr(EndpointRegistry, "_consult_plugin_providers", classmethod(refuse))
        monkeypatch.setattr(EndpointRegistry, "canonical_provider", classmethod(refuse))
        monkeypatch.setattr(_provider_compat, "default_provider_catalog", refuse)

    return apply


def _refuse_factory(*args, **kwargs):
    raise AssertionError("the window catalog's endpoint is never built")


def _window_catalog():
    """A one-fragment catalog that gives one test model an unusual context window."""
    declaration = EndpointDeclaration(
        key="chat",
        aliases=(),
        kind="api",
        factory=_refuse_factory,
        requirements=(),
        settings={},
        diagnostic_id="test.chat",
    )
    fragment = ProviderFragment(
        contract_version=1,
        key="openai",
        aliases=(),
        endpoints=(declaration,),
        default_endpoint="chat",
        context_windows={_UNUSUAL_MODEL: _UNUSUAL_WINDOW},
    )
    return compose_provider_catalog((fragment,), source="catalog-window")


def _build(spec, **extra):
    return iModel(**spec, api_key=_API_KEY, **extra)


_SELECTION_SPECS = {
    "openai-default-endpoint": {"provider": "openai", "model": "gpt-4.1-mini"},
    "openai-explicit-chat": {"provider": "openai", "model": "gpt-4.1-mini", "endpoint": "chat"},
    "agentic-provider": {"provider": "codex", "model": "gpt-5.4", "endpoint": "query_cli"},
    "provider-alias": {"provider": "openai-codex", "model": "gpt-5.4", "endpoint": "query_cli"},
}

_EFFORT_SPECS = {
    "agentic-provider": {"provider": "codex", "model": "gpt-5.4-max", "endpoint": "query_cli"},
    "provider-alias": {
        "provider": "openai-codex",
        "model": "gpt-5.4-max",
        "endpoint": "query_cli",
    },
}


@pytest.mark.parametrize("spec", list(_SELECTION_SPECS.values()), ids=list(_SELECTION_SPECS))
def test_catalog_selects_the_endpoint_class_the_automatic_path_selects(
    spec, manifest_catalog, forbid_automatic_source
):
    baseline = _build(spec)
    forbid_automatic_source()
    selected = _build(spec, provider_catalog=manifest_catalog)

    assert type(selected.endpoint) is type(baseline.endpoint)
    assert selected.provider_catalog is manifest_catalog
    assert baseline.provider_catalog is None


@pytest.mark.parametrize("spec", list(_EFFORT_SPECS.values()), ids=list(_EFFORT_SPECS))
def test_effort_suffix_routes_to_the_effort_kwarg_with_a_catalog(
    spec, manifest_catalog, forbid_automatic_source
):
    baseline = _build(spec)
    forbid_automatic_source()
    selected = _build(spec, provider_catalog=manifest_catalog)

    assert selected.endpoint.config.kwargs["model"] == "gpt-5.4"
    assert selected.endpoint.config.kwargs["reasoning_effort"] == "max"
    assert selected.endpoint.config.kwargs["model"] == baseline.endpoint.config.kwargs["model"]
    assert (
        selected.endpoint.config.kwargs["reasoning_effort"]
        == baseline.endpoint.config.kwargs["reasoning_effort"]
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"provider": "openai", "model": "gpt-4.1-mini"},
        {"provider": "codex", "model": "gpt-5.4-max", "endpoint": "query_cli"},
        {"provider": "openai"},
    ],
    ids=["openai-with-model", "effort-suffixed-model", "no-model"],
)
def test_empty_catalog_has_no_provider(spec, empty_catalog, forbid_automatic_source):
    forbid_automatic_source()

    with pytest.raises(ProviderNotFoundError, match="catalog-empty"):
        _build(spec, provider_catalog=empty_catalog)


def test_endpoint_instance_and_catalog_are_refused(manifest_catalog, forbid_automatic_source):
    endpoint = _build(_SELECTION_SPECS["openai-explicit-chat"]).endpoint
    assert isinstance(endpoint, Endpoint)
    forbid_automatic_source()

    with pytest.raises(ValueError, match="not both"):
        iModel(endpoint=endpoint, provider_catalog=manifest_catalog)


def test_endpoint_instance_without_catalog_carries_none():
    endpoint = _build(_SELECTION_SPECS["openai-explicit-chat"]).endpoint

    assert iModel(endpoint=endpoint).provider_catalog is None


@pytest.mark.parametrize("share_executor", [False, True])
def test_copy_keeps_the_same_catalog(share_executor, manifest_catalog, forbid_automatic_source):
    model = _build(_SELECTION_SPECS["openai-explicit-chat"], provider_catalog=manifest_catalog)
    forbid_automatic_source()

    copied = model.copy(share_executor=share_executor)

    assert copied.provider_catalog is manifest_catalog
    assert copied.id != model.id
    assert (copied.executor is model.executor) is share_executor


def test_copy_without_catalog_carries_none():
    model = _build(_SELECTION_SPECS["openai-explicit-chat"])

    assert model.copy().provider_catalog is None


def test_to_dict_never_writes_the_catalog(manifest_catalog, forbid_automatic_source):
    model = _build(_SELECTION_SPECS["openai-explicit-chat"], provider_catalog=manifest_catalog)
    forbid_automatic_source()

    data = model.to_dict()

    assert model.provider_catalog is manifest_catalog
    assert "provider_catalog" not in data
    assert "provider_catalog" not in repr(data)


def test_from_dict_with_catalog_restores_a_model_that_carries_it(
    manifest_catalog, forbid_automatic_source
):
    original = _build(_SELECTION_SPECS["openai-explicit-chat"])
    data = original.to_dict()
    forbid_automatic_source()

    restored = iModel.from_dict(data, provider_catalog=manifest_catalog, api_key=_API_KEY)

    assert restored.provider_catalog is manifest_catalog
    assert type(restored.endpoint) is type(original.endpoint)
    assert restored.endpoint.config.kwargs["model"] == "gpt-4.1-mini"


def test_from_dict_selects_the_registered_subclass_from_the_catalog_only(
    empty_catalog, forbid_automatic_source
):
    data = _build(_SELECTION_SPECS["openai-explicit-chat"]).to_dict()
    forbid_automatic_source()

    restored = iModel.from_dict(data, provider_catalog=empty_catalog, api_key=_API_KEY)

    assert restored.provider_catalog is empty_catalog


def test_from_dict_without_catalog_carries_none():
    data = _build(_SELECTION_SPECS["openai-explicit-chat"]).to_dict()

    restored = iModel.from_dict(data, api_key=_API_KEY)

    assert restored.provider_catalog is None


_URL = "https://example.invalid/v1"
_UNDECLARED_ENDPOINT = "no-such-endpoint"


def _declared_keys(catalog, provider):
    """The endpoint keys ``catalog`` declares for ``provider``, as the refusal message lists them."""
    return ", ".join(declaration.key for declaration in catalog.fragment(provider).endpoints)


@pytest.fixture
def narrow_openai_catalog(manifest_catalog):
    """The manifest's catalog with ``openai`` cut down to its first two endpoints."""
    fragments = []
    for fragment in manifest_catalog.providers:
        if fragment.key == "openai":
            kept = fragment.endpoints[:2]
            fragment = dataclasses.replace(fragment, endpoints=kept, default_endpoint=kept[0].key)
        fragments.append(fragment)
    return compose_provider_catalog(fragments, source="catalog-narrow")


def _responses_dump():
    """The dump of a model built from the automatic source on openai's ``responses`` endpoint."""
    model = _build({"provider": "openai", "model": "gpt-4.1-mini", "endpoint": "responses"})
    assert model.endpoint.config.endpoint == "responses"
    return model.to_dict()


def _hand_built_dump():
    """The dump of a model that wraps a hand-built endpoint of provider ``openai``."""
    config = EndpointConfig(
        provider="openai",
        endpoint="v1/custom",
        name="custom",
        auth_type="bearer",
        content_type="application/json",
        method="POST",
        requires_tokens=True,
        openai_compatible=True,
        base_url=_URL,
        api_key=_API_KEY,
    )
    return iModel(endpoint=Endpoint(config)).to_dict()


@pytest.fixture(params=["built-model", "hand-built-endpoint"])
def stored_undeclared_endpoint(request, manifest_catalog, narrow_openai_catalog):
    """``(dump, catalog, stored endpoint name)``: the catalog declares the dump's provider
    (several endpoints) but not the endpoint the dump stores."""
    if request.param == "built-model":
        data, catalog, stored = _responses_dump(), narrow_openai_catalog, "responses"
    else:
        data, catalog, stored = _hand_built_dump(), manifest_catalog, "v1/custom"
    assert len(catalog.fragment("openai").endpoints) > 1
    assert catalog.endpoint("openai", stored) is None
    return data, catalog, stored


def test_from_dict_refuses_an_endpoint_the_supplied_catalog_does_not_declare(
    stored_undeclared_endpoint, forbid_automatic_source
):
    data, catalog, stored = stored_undeclared_endpoint
    forbid_automatic_source()

    with pytest.raises(ProviderNotFoundError) as raised:
        iModel.from_dict(data, provider_catalog=catalog, api_key=_API_KEY)

    message = str(raised.value)
    assert repr(catalog.source) in message
    assert "'openai'" in message
    assert repr(stored) in message
    assert _declared_keys(catalog, "openai") in message


def test_from_dict_without_a_catalog_keeps_the_stored_endpoint(stored_undeclared_endpoint):
    data, _, stored = stored_undeclared_endpoint

    restored = iModel.from_dict(data, api_key=_API_KEY)

    assert restored.provider_catalog is None
    assert restored.endpoint.config.endpoint == stored


def test_from_dict_with_a_catalog_that_declares_the_stored_endpoint(
    manifest_catalog, forbid_automatic_source
):
    data = _responses_dump()
    declaration = manifest_catalog.endpoint("openai", "responses")
    assert declaration is not None
    forbid_automatic_source()

    restored = iModel.from_dict(data, provider_catalog=manifest_catalog, api_key=_API_KEY)

    assert restored.provider_catalog is manifest_catalog
    assert type(restored.endpoint) is declaration.factory()
    assert restored.endpoint.config.endpoint == "responses"


@pytest.mark.parametrize(
    "options",
    [{"base_url": _URL}, {"openai_compatible": True}],
    ids=["base-url", "openai-compatible"],
)
def test_construction_refuses_an_endpoint_the_supplied_catalog_does_not_declare(
    options, manifest_catalog, forbid_automatic_source
):
    assert len(manifest_catalog.fragment("openai").endpoints) > 1
    assert manifest_catalog.endpoint("openai", _UNDECLARED_ENDPOINT) is None
    forbid_automatic_source()

    with pytest.raises(ProviderNotFoundError) as raised:
        iModel(
            provider="openai",
            endpoint=_UNDECLARED_ENDPOINT,
            provider_catalog=manifest_catalog,
            api_key=_API_KEY,
            **options,
        )

    message = str(raised.value)
    assert repr(manifest_catalog.source) in message
    assert "'openai'" in message
    assert repr(_UNDECLARED_ENDPOINT) in message
    assert _declared_keys(manifest_catalog, "openai") in message


def test_single_endpoint_providers_select_their_endpoint_from_the_catalog(
    manifest_catalog, forbid_automatic_source, monkeypatch
):
    """Every manifest provider that declares exactly one endpoint is built as an ``iModel`` with
    a dummy ``api_key``, so no credential is read. The provider's only endpoint is selected
    whether none or an undeclared one is named, and the generic endpoint is never built."""
    single = [fragment for fragment in manifest_catalog.providers if len(fragment.endpoints) == 1]
    assert single

    def refuse(*args, **kwargs):
        raise AssertionError(
            "the generic endpoint must not be built for a single-endpoint provider"
        )

    forbid_automatic_source()
    monkeypatch.setattr(EndpointRegistry, "_generic_endpoint", classmethod(refuse))

    for fragment in single:
        declaration = fragment.endpoints[0]
        for named in ({}, {"endpoint": _UNDECLARED_ENDPOINT}):
            model = iModel(
                provider=fragment.key, provider_catalog=manifest_catalog, api_key=_API_KEY, **named
            )
            where = f"{fragment.key} {named}"
            assert type(model.endpoint) is declaration.factory(), where
            assert type(model.endpoint) is not Endpoint, where
            assert model.endpoint.config.endpoint == declaration.key, where
            assert model.provider_catalog is manifest_catalog, where


_DEFAULT_ENDPOINT_NAME = inspect.signature(iModel.__init__).parameters["endpoint"].default


def _split_by_default_endpoint(catalog):
    """Split ``catalog``'s providers by what ``iModel``'s default endpoint name selects.

    Returns ``(refused, built)``. ``refused`` holds each provider that declares several
    endpoints, none of them keyed or aliased with the default name, so the catalog selects no
    endpoint for it. ``built`` holds every other provider.
    """
    assert isinstance(_DEFAULT_ENDPOINT_NAME, str) and _DEFAULT_ENDPOINT_NAME
    refused, built = [], []
    for fragment in catalog.providers:
        names_default = any(
            _DEFAULT_ENDPOINT_NAME == declaration.key
            or _DEFAULT_ENDPOINT_NAME in declaration.aliases
            for declaration in fragment.endpoints
        )
        (built if len(fragment.endpoints) == 1 or names_default else refused).append(fragment)
    return refused, built


def _unmet_requirements(declaration):
    """The requirements of ``declaration`` with an import root that cannot be imported now."""
    return [
        requirement
        for requirement in declaration.requirements
        if not all(importlib.util.find_spec(root) is not None for root in requirement.import_roots)
    ]


def test_default_endpoint_name_is_refused_where_the_catalog_selects_no_endpoint(
    manifest_catalog, forbid_automatic_source
):
    """With no endpoint argument, a manifest provider that declares several endpoints and none
    under the default endpoint name is refused, not routed to the generic endpoint."""
    refused, _ = _split_by_default_endpoint(manifest_catalog)
    assert refused
    assert "tavily" in {fragment.key for fragment in refused}
    forbid_automatic_source()

    for fragment in refused:
        try:
            iModel(provider=fragment.key, provider_catalog=manifest_catalog, api_key=_API_KEY)
        except ProviderNotFoundError as error:
            message = str(error)
        else:
            pytest.fail(f"{fragment.key!r} was built although the catalog selects no endpoint")
        assert repr(fragment.key) in message, fragment.key
        assert repr(_DEFAULT_ENDPOINT_NAME) in message, fragment.key
        assert _declared_keys(manifest_catalog, fragment.key) in message, fragment.key


def test_default_endpoint_name_builds_every_other_manifest_provider(
    manifest_catalog, forbid_automatic_source, monkeypatch
):
    """The control for the refusal above: every other manifest provider is built from its
    declared endpoint with the same call, and the generic endpoint is never built.

    Some declared endpoints need an optional extra. When every import root of the selected
    declaration's requirements is importable, the provider must build. When one is not, the
    declared endpoint may refuse with a ``ModuleNotFoundError`` that names the missing
    requirement's install hint, or build without importing the extra, as the ``ag2`` endpoint
    does: either way the call reached the declared endpoint, since the generic one raises here.
    """
    refused, built = _split_by_default_endpoint(manifest_catalog)
    assert built
    assert len(refused) + len(built) == len(manifest_catalog.providers)

    def refuse(*args, **kwargs):
        raise AssertionError("the generic endpoint must not be built for a selected endpoint")

    # Importing a provider module registers it against the automatic source, so load the
    # registry before that source is forbidden; the test then runs alone as well as in the module.
    EndpointRegistry._ensure_loaded()
    forbid_automatic_source()
    monkeypatch.setattr(EndpointRegistry, "_generic_endpoint", classmethod(refuse))

    for fragment in built:
        declaration = manifest_catalog.endpoint(fragment.key, _DEFAULT_ENDPOINT_NAME)
        assert declaration is not None, fragment.key
        unmet = _unmet_requirements(declaration)
        try:
            model = iModel(
                provider=fragment.key, provider_catalog=manifest_catalog, api_key=_API_KEY
            )
        except ModuleNotFoundError as error:
            if not unmet:
                raise
            assert any(req.install_hint in str(error) for req in unmet), (fragment.key, str(error))
            continue
        assert type(model.endpoint) is declaration.factory(), fragment.key
        assert type(model.endpoint) is not Endpoint, fragment.key
        assert model.provider_catalog is manifest_catalog, fragment.key


def _stand_in_branch(chat_model_extra):
    config = SimpleNamespace(
        context_window=None, provider="openai", kwargs={"model": _UNUSUAL_MODEL}
    )
    chat_model = SimpleNamespace(endpoint=SimpleNamespace(config=config), **chat_model_extra)
    return SimpleNamespace(chat_model=chat_model)


def test_context_window_reads_the_model_catalog(forbid_automatic_source):
    catalog = _window_catalog()
    forbid_automatic_source()

    window = get_context_window(_stand_in_branch({"provider_catalog": catalog}))

    assert window == _UNUSUAL_WINDOW


def test_context_window_of_a_model_built_with_a_catalog(manifest_catalog, forbid_automatic_source):
    model = _build(_SELECTION_SPECS["openai-explicit-chat"], provider_catalog=manifest_catalog)
    expected = manifest_catalog.context_window("gpt-4.1-mini", "openai")
    assert expected is not None
    forbid_automatic_source()

    assert get_context_window(SimpleNamespace(chat_model=model)) == expected


def test_context_window_without_a_catalog_uses_the_default_source(monkeypatch):
    catalog = _window_catalog()
    monkeypatch.setattr(_provider_compat, "default_provider_catalog", lambda: catalog)

    assert get_context_window(_stand_in_branch({})) == _UNUSUAL_WINDOW
    assert get_context_window(_stand_in_branch({"provider_catalog": None})) == _UNUSUAL_WINDOW
