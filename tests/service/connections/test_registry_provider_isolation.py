# SPDX-License-Identifier: Apache-2.0

"""A built-in provider that fails to activate is isolated by the registry and reported.

When the registry loads while one built-in provider's ``_config`` module cannot be imported,
every other built-in provider stays selectable. Selecting the failed provider by its key or
one of its aliases raises its ``ProviderActivationError`` whatever the other arguments say,
nothing can register under a name the bundled catalog declares except the built-in itself,
and ``EndpointRegistry.failed_providers`` reports the failure.
"""

from __future__ import annotations

import logging
import warnings
from types import SimpleNamespace

import pytest

from lionagi._provider_errors import ProviderActivationError
from lionagi.service.connections import _provider_compat
from lionagi.service.connections import registry as registry_module
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.provider_catalog import (
    EndpointDeclaration,
    ProviderFragment,
    ProviderManifest,
    compose_provider_catalog,
)
from lionagi.service.connections.registry import (
    EndpointRegistry,
    ProviderAliasCollisionError,
    register_endpoint,
)
from lionagi.service.imodel import iModel
from lionhub.providers import manifest

FAILED_PACKAGE = "nvidia_nim"
FAILURE = ImportError("the nvidia_nim configuration needs a module that is not installed")
ROW = next(row for row in manifest._DECLARATIONS if row.package == FAILED_PACKAGE)
HEALTHY = [row for row in manifest._DECLARATIONS if row.package != FAILED_PACKAGE]
FIRST_ENDPOINT = ROW.endpoints[0]
SECOND_ENDPOINT = ROW.endpoints[1]
CUSTOM_BASE_URL = "https://llm.example.test/v1"


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot and restore the registry so a registration never leaks into another test."""
    EndpointRegistry._ensure_loaded()
    entries = list(EndpointRegistry._entries)
    loaded = EndpointRegistry._loaded
    owners = dict(EndpointRegistry._alias_owners)
    yield
    EndpointRegistry._entries = entries
    EndpointRegistry._loaded = loaded
    EndpointRegistry._alias_owners = owners


def _selection(rows):
    """The endpoint class ``match_endpoint`` picks for each provider and endpoint of ``rows``."""
    return {
        (row.key, endpoint): type(match_endpoint(row.key, endpoint))
        for row in rows
        for endpoint in ("", *(declared.key for declared in row.endpoints))
    }


def _completed(state):
    """The registry load finished instead of aborting on the failed provider."""
    assert state.load_error is None, f"the registry load aborted: {state.load_error!r}"


@pytest.fixture
def failed_provider(monkeypatch, caplog):
    """Load the registry cold while ``FAILED_PACKAGE``'s ``_config`` raises ``ImportError``.

    The state holds what the healthy providers selected before the load, the error the load
    raised (``None`` when it finished) and the warnings it logged. Afterwards the patches are
    undone and the registry is loaded again from the unpatched manifest.
    """
    EndpointRegistry._ensure_loaded()
    state = SimpleNamespace(before=_selection(HEALTHY), load_error=None, warnings=[])
    real_load = manifest._load_config

    def load(package):
        if package == FAILED_PACKAGE:
            raise FAILURE
        return real_load(package)

    with monkeypatch.context() as patch:
        patch.setattr(manifest, "_load_config", load)
        patch.setattr(_provider_compat, "_outcome", None)
        patch.setattr(EndpointRegistry, "_loaded", False)
        manifest.get_provider_manifest.cache_clear()
        # A cold start has registered nothing for the provider that failed to activate.
        with EndpointRegistry._lock:
            EndpointRegistry._remove_entries(
                [e for e in EndpointRegistry._entries if e.meta.provider == ROW.key]
            )
        with caplog.at_level(logging.WARNING, logger=registry_module.logger.name):
            try:
                EndpointRegistry._ensure_loaded()
            except Exception as error:
                state.load_error = error
        state.warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        yield state
    manifest.get_provider_manifest.cache_clear()
    EndpointRegistry._loaded = False
    EndpointRegistry._ensure_loaded()


def test_load_finishes_and_logs_one_warning_naming_the_provider(failed_provider):
    _completed(failed_provider)
    messages = [record.getMessage() for record in failed_provider.warnings]
    assert len(messages) == 1
    assert ROW.key in messages[0]
    assert "activation_failed" in messages[0]


def test_other_providers_select_the_same_endpoints(failed_provider):
    _completed(failed_provider)
    assert _selection(HEALTHY) == failed_provider.before


@pytest.mark.parametrize(
    "extra",
    [{}, {"openai_compatible": True}, {"base_url": CUSTOM_BASE_URL}],
    ids=["plain", "openai-compatible", "base-url"],
)
@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        FIRST_ENDPOINT.key,
        FIRST_ENDPOINT.aliases[0],
        SECOND_ENDPOINT.key,
        "no-such-endpoint",
    ],
    ids=["no-endpoint", "key", "endpoint-alias", "other-endpoint", "unknown-endpoint"],
)
@pytest.mark.parametrize(
    "provider",
    [ROW.key, ROW.aliases[0], ROW.key.upper()],
    ids=["key", "alias", "upper-case"],
)
def test_selecting_the_failed_provider_raises_its_activation_error(
    failed_provider, provider, endpoint, extra
):
    _completed(failed_provider)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ProviderActivationError) as raised:
            match_endpoint(provider, endpoint, **extra)
    assert raised.value.provider == ROW.key
    assert raised.value.reason == "activation_failed"
    assert raised.value.source == _provider_compat.default_provider_catalog().source
    assert raised.value.__cause__ is FAILURE


def test_imodel_for_the_failed_provider_raises_its_activation_error(failed_provider):
    _completed(failed_provider)
    with pytest.raises(ProviderActivationError) as raised:
        iModel(provider=ROW.key, model="any-model", api_key="unused")
    assert raised.value.__cause__ is FAILURE


def test_failed_providers_lists_the_failed_provider_and_nothing_else(failed_provider):
    _completed(failed_provider)
    assert EndpointRegistry.failed_providers() == [
        {
            "provider": ROW.key,
            "endpoint": FIRST_ENDPOINT.key,
            "source": _provider_compat.default_provider_catalog().source,
            "reason": "activation_failed",
        }
    ]


def _register_plugin(provider, **kwargs):
    """Register a new endpoint class under ``provider`` and return the class."""

    class PluginEndpoint(Endpoint):
        pass

    register_endpoint(provider, "plugin-endpoint", **kwargs)(PluginEndpoint)
    return PluginEndpoint


@pytest.mark.parametrize(
    ("provider", "aliases"),
    [
        pytest.param(ROW.key, [], id="failed-key"),
        pytest.param("plugin-provider", [ROW.aliases[0]], id="failed-alias"),
        pytest.param("groq", [], id="healthy-key"),
        pytest.param("plugin-provider", ["claude"], id="healthy-alias"),
    ],
)
def test_register_refuses_a_name_the_bundled_catalog_declares(failed_provider, provider, aliases):
    _completed(failed_provider)

    class PluginEndpoint(Endpoint):
        pass

    with pytest.raises(ProviderAliasCollisionError):
        register_endpoint(provider, "plugin-endpoint", provider_aliases=aliases)(PluginEndpoint)
    assert not [e for e in EndpointRegistry._entries if e.cls is PluginEndpoint]
    assert "plugin-provider" not in EndpointRegistry._alias_owners
    with pytest.raises(ProviderActivationError):
        match_endpoint(ROW.key, "")


def test_register_refuses_a_built_in_name_when_nothing_failed():
    with pytest.raises(ProviderAliasCollisionError):
        _register_plugin("groq")
    with pytest.raises(ProviderAliasCollisionError):
        _register_plugin("plugin-provider", provider_aliases=["claude"])


def test_register_accepts_a_name_the_bundled_catalog_does_not_declare():
    registered = _register_plugin("plugin-provider", provider_aliases=["plugin-alias"])
    assert type(match_endpoint("plugin-alias", "plugin-endpoint")) is registered


def test_nothing_is_reported_when_every_provider_activates():
    assert EndpointRegistry.failed_providers() == []


def test_built_in_modules_register_the_endpoints_the_catalog_declares():
    catalog = _provider_compat.default_provider_catalog()
    declared = {d.diagnostic_id for fragment in catalog.providers for d in fragment.endpoints}
    names = {name for fragment in catalog.providers for name in (fragment.key, *fragment.aliases)}
    registered = [e for e in EndpointRegistry._entries if e.meta.provider in names]
    assert registered
    for entry in registered:
        assert f"{entry.cls.__module__}:{entry.cls.__qualname__}" in declared


def test_activation_error_of_a_declared_default_is_not_replaced_by_the_registry_scan(
    monkeypatch,
):
    cause = ImportError("the declared module cannot be imported")

    def factory():
        raise ProviderActivationError(
            provider="declared-only", endpoint="chat", source="a stub", reason="activation_failed"
        ) from cause

    fragment = ProviderFragment(
        contract_version=1,
        key="declared-only",
        aliases=(),
        endpoints=(
            EndpointDeclaration(
                key="chat",
                aliases=(),
                kind="api",
                factory=factory,
                requirements=(),
                settings={},
                diagnostic_id="a_stub_module:Endpoint",
            ),
        ),
        default_endpoint="chat",
        context_windows=None,
    )
    catalog = compose_provider_catalog(
        ProviderManifest(contract_version=1, source="a stub", fragments=(fragment,))
    )
    monkeypatch.setattr(_provider_compat, "default_provider_catalog", lambda: catalog)
    with pytest.raises(ProviderActivationError) as raised:
        EndpointRegistry.match("declared-only")
    assert raised.value.__cause__ is cause
