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


class TestConcurrentRegistrationAndRemoval:
    """Alias ownership must be atomic across concurrent registrations, and
    released when the owning entry is removed (plugin revalidation
    failure), so a legitimate replacement isn't rejected by a ledger
    entry with no backing registration."""

    def test_concurrent_distinct_providers_claiming_same_alias_only_one_wins(self):
        import threading

        start = threading.Barrier(2)
        errors: list[BaseException] = []
        registered: list[str] = []
        lock = threading.Lock()

        def _register(provider_name):
            start.wait(timeout=5)
            try:

                @EndpointRegistry.register(
                    provider=provider_name,
                    endpoint="chat",
                    provider_aliases=["shared-concurrent-alias"],
                )
                class _Stub(Endpoint):
                    pass

            except ProviderAliasCollisionError as exc:
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    registered.append(provider_name)

        threads = [
            threading.Thread(target=_register, args=("concurrent-alpha",)),
            threading.Thread(target=_register, args=("concurrent-beta",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Exactly one registration wins; the other observes the collision.
        assert len(registered) == 1
        assert len(errors) == 1
        owner = EndpointRegistry._alias_owners["shared-concurrent-alias"]
        assert owner == registered[0]
        # No entry exists for the loser under that alias.
        entries_for_alias = [
            e
            for e in EndpointRegistry._entries
            if "shared-concurrent-alias" in e.meta.provider_aliases
        ]
        assert {e.meta.provider for e in entries_for_alias} == {registered[0]}

    def test_plugin_removal_releases_alias_for_legitimate_replacement(self):
        from lionagi.plugins.registry import PluginActivationError

        @EndpointRegistry.register(provider="old-plugin-provider", endpoint="chat")
        class _OldPluginEndpoint(Endpoint):
            pass

        entry = next(e for e in EndpointRegistry._entries if e.cls is _OldPluginEndpoint)
        # Mark it as plugin-owned so _revalidate_plugin_entry's removal path
        # is reachable without standing up a real plugin manifest.
        entry.plugin_name = "old-plugin"
        entry.plugin_target = "old-plugin:_OldPluginEndpoint"

        def _boom(*_args, **_kwargs):
            raise PluginActivationError(
                "old-plugin", "old-plugin:_OldPluginEndpoint", "simulated activation failure"
            )

        import lionagi.plugins as plugins_mod

        original_activate = plugins_mod.PluginRegistry.activate_target
        plugins_mod.PluginRegistry.activate_target = staticmethod(_boom)
        try:
            assert EndpointRegistry._revalidate_plugin_entry(entry) is False
        finally:
            plugins_mod.PluginRegistry.activate_target = original_activate

        assert entry not in EndpointRegistry._entries
        assert "old-plugin-provider" not in EndpointRegistry._alias_owners

        # A brand-new provider can now legitimately claim that freed name.
        @EndpointRegistry.register(provider="old-plugin-provider", endpoint="chat")
        class _NewPluginEndpoint(Endpoint):
            pass

        assert EndpointRegistry._alias_owners["old-plugin-provider"] == "old-plugin-provider"


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
