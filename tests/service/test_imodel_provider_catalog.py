# SPDX-License-Identifier: Apache-2.0

"""``iModel(provider_catalog=...)`` and the token budget that follows it.

A model built with a provider catalog selects its endpoint from that catalog only, routes an
effort-suffixed model name through the catalog's own provider keys, carries the catalog through
``copy`` and ``from_dict``, and reads its context window from it. In every test that supplies a
catalog the automatic source is patched to raise, so a hidden automatic load fails the test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi.service.connections import _provider_compat
from lionagi.service.connections.endpoint import Endpoint
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
