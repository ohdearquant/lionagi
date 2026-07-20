# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``EndpointRegistry`` registration-time diagnostics:
provider/alias canonicalization + collision rejection, and the
optional-dependency-vs-broken-module classification used while importing
bundled provider modules.
"""

from __future__ import annotations

import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.registry import (
    EndpointRegistry,
    ProviderAliasCollisionError,
    _is_missing_optional_dependency,
)


@pytest.fixture(autouse=True)
def _isolate_endpoint_registry():
    """Snapshot/restore registry state so test-only registrations never leak
    into other test files (mirrors the fixture in test_plugin_provider_consumer.py)."""
    EndpointRegistry._ensure_loaded()
    saved_entries = list(EndpointRegistry._entries)
    saved_loaded = EndpointRegistry._loaded
    saved_alias_owners = dict(EndpointRegistry._alias_owners)
    yield
    EndpointRegistry._entries = saved_entries
    EndpointRegistry._loaded = saved_loaded
    EndpointRegistry._alias_owners = saved_alias_owners


class TestProviderAliasCanonicalization:
    def test_provider_and_aliases_are_lowercased_and_stripped(self):
        @EndpointRegistry.register(
            provider="  MyTestProvider  ",
            endpoint="chat",
            provider_aliases=["  MyTP  ", "MYTESTPROVIDER-ALT"],
        )
        class _StubEndpoint(Endpoint):
            pass

        entry = next(e for e in EndpointRegistry._entries if e.cls is _StubEndpoint)
        assert entry.meta.provider == "mytestprovider"
        assert entry.meta.provider_aliases == ("mytp", "mytestprovider-alt")

    def test_registering_the_same_provider_twice_is_not_a_collision(self):
        @EndpointRegistry.register(provider="dup-provider", endpoint="chat")
        class _First(Endpoint):
            pass

        @EndpointRegistry.register(provider="dup-provider", endpoint="embed")
        class _Second(Endpoint):
            pass

        providers = {
            e.meta.provider for e in EndpointRegistry._entries if e.cls in (_First, _Second)
        }
        assert providers == {"dup-provider"}


class TestProviderAliasCollision:
    def test_alias_colliding_with_another_providers_canonical_name_raises(self):
        @EndpointRegistry.register(provider="owner-provider", endpoint="chat")
        class _Owner(Endpoint):
            pass

        with pytest.raises(ProviderAliasCollisionError, match="owner-provider"):

            @EndpointRegistry.register(
                provider="claimant-provider",
                endpoint="chat",
                provider_aliases=["owner-provider"],
            )
            class _Claimant(Endpoint):
                pass

    def test_alias_colliding_with_another_providers_alias_names_both_claimants(self):
        @EndpointRegistry.register(
            provider="first-provider",
            endpoint="chat",
            provider_aliases=["shared-alias"],
        )
        class _First(Endpoint):
            pass

        with pytest.raises(ProviderAliasCollisionError) as excinfo:

            @EndpointRegistry.register(
                provider="second-provider",
                endpoint="chat",
                provider_aliases=["shared-alias"],
            )
            class _Second(Endpoint):
                pass

        message = str(excinfo.value)
        assert "first-provider" in message
        assert "second-provider" in message
        assert "shared-alias" in message

    def test_collision_check_is_case_and_whitespace_insensitive(self):
        @EndpointRegistry.register(provider="canon-provider", endpoint="chat")
        class _Owner(Endpoint):
            pass

        with pytest.raises(ProviderAliasCollisionError):

            @EndpointRegistry.register(
                provider="other-provider",
                endpoint="chat",
                provider_aliases=["  Canon-Provider  "],
            )
            class _Claimant(Endpoint):
                pass


class TestOptionalDependencyClassification:
    """``_is_missing_optional_dependency`` distinguishes a genuinely-absent
    third-party dependency (quiet) from a broken bundled module (loud)."""

    def test_missing_third_party_package_is_optional(self):
        exc = ModuleNotFoundError("No module named 'this_package_definitely_does_not_exist_xyz'")
        exc.name = "this_package_definitely_does_not_exist_xyz"
        assert _is_missing_optional_dependency(exc) is True

    def test_missing_third_party_submodule_is_optional(self):
        exc = ModuleNotFoundError(
            "No module named 'this_package_definitely_does_not_exist_xyz.submodule'"
        )
        exc.name = "this_package_definitely_does_not_exist_xyz.submodule"
        assert _is_missing_optional_dependency(exc) is True

    def test_missing_internal_lionagi_name_is_not_optional(self):
        exc = ModuleNotFoundError("No module named 'lionagi.providers.nonexistent'")
        exc.name = "lionagi.providers.nonexistent"
        assert _is_missing_optional_dependency(exc) is False

    def test_installed_package_failing_for_other_reasons_is_not_optional(self):
        exc = ModuleNotFoundError("No module named 'os.nonexistent_submodule'")
        exc.name = "os"
        assert _is_missing_optional_dependency(exc) is False

    def test_import_error_without_a_name_is_not_optional(self):
        # The "cannot import name X from Y" shape a broken re-export raises;
        # CPython does not set .name for this ImportError shape.
        exc = ImportError("cannot import name 'Foo' from 'lionagi.bar'")
        assert _is_missing_optional_dependency(exc) is False
