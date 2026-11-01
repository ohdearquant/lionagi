# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``EndpointRegistry`` registration-time diagnostics:
provider/alias canonicalization + collision rejection, and the declared-
dependency preflight used while importing bundled provider modules.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from lionagi._feature_errors import MissingFeatureError
from lionagi._provider_errors import ProviderActivationError, ProviderContractVersionError
from lionagi.service.connections import _provider_compat
from lionagi.service.connections import registry as _registry_mod
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.registry import (
    EndpointRegistry,
    ProviderAliasCollisionError,
    ProviderNotFoundError,
    _import_provider_module,
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


class TestCanonicalProvider:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("codex", "codex"),
            ("openai-codex", "codex"),
            ("OpenAI-Codex", "codex"),
            ("Codex", "codex"),
            ("claude", "claude_code"),
            ("gemini-cli", "gemini_code"),
            ("pi_code", "pi"),
            ("openai", "openai"),
            ("not-a-provider", "not-a-provider"),
        ],
    )
    def test_a_name_resolves_to_the_provider_it_matches(self, name, expected):
        assert EndpointRegistry.canonical_provider(name) == expected

    def test_a_padded_name_is_not_trimmed_into_a_provider(self):
        # The endpoint match does not trim a name either, so " codex " runs no codex.
        assert EndpointRegistry.canonical_provider(" codex ") == " codex "
        assert EndpointRegistry._match_registered(" codex ", "", {}) is None


def test_a_padded_provider_name_is_not_trimmed_into_the_generic_endpoint():
    from lionagi.service.connections.registry import EndpointRegistry

    with pytest.raises(ProviderNotFoundError, match=" codex"):
        EndpointRegistry.match(" codex", "chat/completions")


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

    def test_builtin_collision_rejection_releases_unique_alias_for_replacement(self):
        """A plugin entry whose canonical provider collides with a built-in
        (openai) is dropped by ``_reject_builtin_collisions``; its otherwise
        unique alias must not stay claimed in ``_alias_owners`` afterward."""

        @EndpointRegistry.register(
            provider="openai",
            endpoint="chat",
            provider_aliases=["orphan-alias-f2359"],
        )
        class _RejectedPluginEndpoint(Endpoint):
            pass

        entry = next(e for e in EndpointRegistry._entries if e.cls is _RejectedPluginEndpoint)
        entry.plugin_name = "rejected-plugin"
        entry.plugin_target = "rejected-plugin:_RejectedPluginEndpoint"

        EndpointRegistry._reject_builtin_collisions(
            "rejected-plugin",
            "rejected-plugin:_RejectedPluginEndpoint",
        )

        assert entry not in EndpointRegistry._entries
        assert "orphan-alias-f2359" not in EndpointRegistry._alias_owners

        with pytest.raises(ProviderNotFoundError):
            EndpointRegistry.match("orphan-alias-f2359")

        @EndpointRegistry.register(
            provider="replacement-provider-f2359",
            endpoint="chat",
            provider_aliases=["orphan-alias-f2359"],
        )
        class _ReplacementEndpoint(Endpoint):
            pass

        assert EndpointRegistry._alias_owners["orphan-alias-f2359"] == "replacement-provider-f2359"

    def test_builtin_collision_rejection_covers_helper_module_registration(self):
        """A provider module may register an endpoint class that a helper or
        generated module supplies. The entry carries the activation's
        provenance but a different ``__module__``, and the built-in must
        still win."""

        class _HelperSuppliedEndpoint(Endpoint):
            pass

        _HelperSuppliedEndpoint.__module__ = "helper_plugin._generated_endpoints"
        EndpointRegistry.register(
            provider="openai",
            endpoint="chat",
            provider_aliases=["orphan-alias-a71c4"],
        )(_HelperSuppliedEndpoint)

        entry = next(e for e in EndpointRegistry._entries if e.cls is _HelperSuppliedEndpoint)
        entry.plugin_name = "helper-plugin"
        entry.plugin_target = "helper_plugin.providers"

        EndpointRegistry._reject_builtin_collisions("helper-plugin", "helper_plugin.providers")

        assert entry not in EndpointRegistry._entries
        assert "orphan-alias-a71c4" not in EndpointRegistry._alias_owners


class TestOptionalDependencyPreflight:
    """``_import_provider_module`` classifies by a declared dependency table
    checked via ``find_spec`` *before* import, never by inspecting the
    metadata of an ``ImportError`` raised during import."""

    def test_module_skipped_when_declared_dependency_is_absent(self, monkeypatch, caplog):
        mod = "tests.service.connections._fixture_absent_dep_provider"
        monkeypatch.setitem(
            _registry_mod._PROVIDER_OPTIONAL_DEPENDENCIES,
            mod,
            ("this_pkg_definitely_does_not_exist_xyz",),
        )
        with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
            _import_provider_module(mod)  # would raise RuntimeError if executed

        assert mod not in sys.modules
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("not installed" in r.getMessage() for r in debug_records)
        assert not warning_records

    def test_forged_import_error_with_resolved_deps_is_a_load_failure(self, monkeypatch, caplog):
        # Nothing declared for this module -- the preflight has nothing to
        # check and passes trivially, so the import is attempted.
        mod = "tests.service.connections._fixture_forged_import_provider"
        monkeypatch.delitem(_registry_mod._PROVIDER_OPTIONAL_DEPENDENCIES, mod, raising=False)
        with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
            _import_provider_module(mod)

        assert mod not in sys.modules
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "a forged/buggy ImportError must be a loud load failure"
        assert not debug_records, "no metadata-based quiet classification should ever occur"


@pytest.fixture
def provider_import_package(tmp_path, monkeypatch):
    name = "_provider_registry_import_case"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name, root
    for module_name in tuple(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            sys.modules.pop(module_name, None)


def test_provider_loading_registers_a_real_endpoint_through_the_shared_importer(
    provider_import_package, monkeypatch
):
    from lionagi.ln import import_module

    name, root = provider_import_package
    target = name + ".endpoint"
    (root / "endpoint.py").write_text(
        "from lionagi.service.connections.endpoint import Endpoint\n"
        "from lionagi.service.connections.registry import EndpointRegistry\n"
        "@EndpointRegistry.register(provider='import-contract-provider', endpoint='chat')\n"
        "class ExampleEndpoint(Endpoint):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(_registry_mod._PROVIDER_OPTIONAL_DEPENDENCIES, target, ("sys",))
    bound_importer = getattr(_registry_mod, "import_module", None)
    calls = []

    def observed_import(module_name, *args, **kwargs):
        calls.append((module_name, args, kwargs))
        return import_module(module_name, *args, **kwargs)

    monkeypatch.setattr(_registry_mod, "import_module", observed_import, raising=False)
    _registry_mod._import_provider_module(target)
    _registry_mod._import_provider_module(target)

    endpoint = sys.modules[target].ExampleEndpoint
    entries = [entry for entry in EndpointRegistry._entries if entry.cls is endpoint]
    assert len(entries) == 1
    assert entries[0].meta.provider == "import-contract-provider"
    assert bound_importer is import_module
    assert [call[0] for call in calls] == [target, target]
    assert all(not args and kwargs.get("import_name") is None for _, args, kwargs in calls)


def test_provider_dependency_preflight_prevents_module_execution_and_import_delegation(
    provider_import_package, monkeypatch, caplog
):
    from lionagi.ln import import_module

    name, root = provider_import_package
    target = name + ".blocked"
    marker = root / "executed"
    (root / "blocked.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('provider body must not run')\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(
        _registry_mod._PROVIDER_OPTIONAL_DEPENDENCIES,
        target,
        ("_absent_provider_dependency_for_import_contract",),
    )
    calls = []

    def observed_import(module_name, *args, **kwargs):
        calls.append(module_name)
        return import_module(module_name, *args, **kwargs)

    monkeypatch.setattr(_registry_mod, "import_module", observed_import, raising=False)
    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        _registry_mod._import_provider_module(target)

    assert not marker.exists()
    assert target not in sys.modules
    assert calls == []
    records = [record for record in caplog.records if record.name == _registry_mod.logger.name]
    assert any(
        record.levelno == logging.DEBUG and "not installed" in record.getMessage()
        for record in records
    )
    assert not any(record.levelno >= logging.WARNING for record in records)


@pytest.mark.parametrize("error_kind", ["ordinary", "subclass", "missing"])
def test_provider_import_failures_warn_without_reclassifying_exception_metadata(
    provider_import_package, monkeypatch, caplog, error_kind
):
    import types

    name, root = provider_import_package

    class ProviderImportError(ImportError):
        pass

    error_type = {
        "ordinary": ImportError,
        "subclass": ProviderImportError,
        "missing": ModuleNotFoundError,
    }[error_kind]
    original = error_type("provider body failed", name="forged_optional_dependency", path="body.py")
    state = types.ModuleType(name + ".state")
    state.error = original
    monkeypatch.setitem(sys.modules, state.__name__, state)
    target = name + ".broken"
    (root / "broken.py").write_text("from .state import error\nraise error\n", encoding="utf-8")
    monkeypatch.setitem(_registry_mod._PROVIDER_OPTIONAL_DEPENDENCIES, target, ("sys",))

    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        assert _registry_mod._import_provider_module(target) is None

    assert target not in sys.modules
    records = [record for record in caplog.records if record.name == _registry_mod.logger.name]
    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not any(record.levelno == logging.DEBUG for record in records)
    assert "failed to import and was not registered" in warnings[0].getMessage()
    reported = warnings[0].args[1]
    if error_kind == "missing":
        assert reported is original
        assert reported.name == "forged_optional_dependency"
        assert reported.path == "body.py"
        assert reported.msg == "provider body failed"
    else:
        assert type(reported) is ImportError
        assert str(reported) == f"Failed to import module {target}: {original}"
        assert reported.msg == str(reported)
        assert reported.name is None
        assert reported.path is None
        assert reported.__cause__ is original


# -- built-in providers load from the provider catalog's declarations --------------------

_CORE_SCRIPTED_MODULE = "lionagi.testing._endpoint"


@pytest.fixture
def cold_registry(monkeypatch):
    """Make the next ``_ensure_loaded`` a cold load that registers nothing.

    Returns the list that records, in order, every declared factory the load calls and every
    core module it imports (as ``core:<module>``). The restore of the registry state is the
    autouse fixture above.
    """
    calls: list[str] = []
    monkeypatch.setattr(EndpointRegistry, "_loaded", False)
    monkeypatch.setattr(
        _registry_mod, "_import_provider_module", lambda mod: calls.append(f"core:{mod}")
    )
    return calls


def _install_catalog(monkeypatch, outcome):
    """Make ``default_provider_catalog`` return ``outcome``, or raise it when it is an error."""

    def load():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(_provider_compat, "default_provider_catalog", load)


def _declared(calls, module, name="Endpoint", error=None):
    """A declaration whose factory records its call and then raises ``error`` if one is given."""

    def factory():
        calls.append(f"{module}:{name}")
        if error is not None:
            raise error

    return SimpleNamespace(factory=factory, diagnostic_id=f"{module}:{name}")


def _catalog_of(*providers):
    """A catalog of providers, each given as the declarations of its endpoints, in order."""
    return SimpleNamespace(
        providers=tuple(SimpleNamespace(endpoints=tuple(group)) for group in providers)
    )


def test_cold_load_calls_every_declared_factory_in_catalog_order(
    monkeypatch, cold_registry, caplog
):
    calls = cold_registry
    catalog = _catalog_of(
        [
            _declared(calls, "stub_providers.alpha.chat"),
            _declared(calls, "stub_providers.alpha.embed"),
        ],
        [_declared(calls, "stub_providers.beta.search")],
    )
    _install_catalog(monkeypatch, catalog)

    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        EndpointRegistry._ensure_loaded()

    # The scripted core module is imported through the shared helper after the declared
    # providers, so it stays last among the registered entries.
    assert calls == [
        "stub_providers.alpha.chat:Endpoint",
        "stub_providers.alpha.embed:Endpoint",
        "stub_providers.beta.search:Endpoint",
        f"core:{_CORE_SCRIPTED_MODULE}",
    ]
    assert EndpointRegistry._loaded is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_cold_load_warns_for_a_declaration_whose_module_fails_to_import_and_goes_on(
    monkeypatch, cold_registry, caplog
):
    calls = cold_registry
    failure = ImportError("No module named 'a_missing_provider_dependency'")
    catalog = _catalog_of(
        [_declared(calls, "stub_providers.alpha.chat")],
        [_declared(calls, "stub_providers.beta.broken", name="BrokenEndpoint", error=failure)],
        [_declared(calls, "stub_providers.gamma.chat")],
    )
    _install_catalog(monkeypatch, catalog)

    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        EndpointRegistry._ensure_loaded()

    assert calls == [
        "stub_providers.alpha.chat:Endpoint",
        "stub_providers.beta.broken:BrokenEndpoint",
        "stub_providers.gamma.chat:Endpoint",
        f"core:{_CORE_SCRIPTED_MODULE}",
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].name == _registry_mod.logger.name
    assert "failed to import and was not registered" in warnings[0].getMessage()
    assert warnings[0].args[0] == "stub_providers.beta.broken"
    assert warnings[0].args[1] is failure
    assert EndpointRegistry._loaded is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            MissingFeatureError(
                feature="provider hub",
                missing_distribution="lionhub",
                install_hint="reinstall lionagi",
            ),
            id="missing-feature",
        ),
        pytest.param(
            ProviderActivationError(
                provider="openai", endpoint=None, source="a stub", reason="activation_failed"
            ),
            id="activation",
        ),
        pytest.param(
            ProviderContractVersionError(source="a stub", expected=(1,), observed=99),
            id="contract-version",
        ),
    ],
)
def test_cold_load_logs_one_warning_when_the_catalog_cannot_load(
    monkeypatch, cold_registry, caplog, error
):
    calls = cold_registry
    _install_catalog(monkeypatch, error)

    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        EndpointRegistry._ensure_loaded()

    assert calls == [f"core:{_CORE_SCRIPTED_MODULE}"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].name == _registry_mod.logger.name
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage().startswith(
        "built-in providers were not registered: the provider catalog could not load: "
        f"{type(error).__name__}"
    )
    assert EndpointRegistry._loaded is True


def test_cold_load_lets_an_unexpected_catalog_error_propagate(
    monkeypatch, cold_registry, caplog
):
    failure = RuntimeError("the catalog failed in a way the registry does not expect")
    _install_catalog(monkeypatch, failure)

    with caplog.at_level(logging.DEBUG, logger=_registry_mod.logger.name):
        with pytest.raises(RuntimeError) as raised:
            EndpointRegistry._ensure_loaded()

    assert raised.value is failure
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert EndpointRegistry._loaded is False
