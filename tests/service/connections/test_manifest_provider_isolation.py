# SPDX-License-Identifier: Apache-2.0

"""A provider whose ``_config`` cannot be imported keeps its identity in the built-in manifest.

Every declaration row states its provider's key and aliases and each endpoint's key, aliases
and kind, so the manifest can name a provider without reading its ``_config``. These tests
check that the rows agree with the ``_config`` enums, that an ``ImportError`` from one
``_config`` leaves every other fragment unchanged, and that the failed provider's endpoint
factories raise ``ProviderActivationError`` and import nothing.
"""

from __future__ import annotations

import importlib

import pytest

from lionagi._provider_errors import ProviderActivationError
from lionhub.providers import manifest
from lionhub.providers.manifest import get_provider_manifest


def _describe(fragment):
    """The comparable content of a fragment: identity, windows and endpoint declarations."""
    return (
        fragment.contract_version,
        fragment.key,
        fragment.aliases,
        fragment.default_endpoint,
        None if fragment.context_windows is None else dict(fragment.context_windows),
        [
            (
                endpoint.key,
                endpoint.aliases,
                endpoint.kind,
                endpoint.requirements,
                dict(endpoint.settings),
                endpoint.diagnostic_id,
                (endpoint.factory.func, endpoint.factory.args, endpoint.factory.keywords),
            )
            for endpoint in fragment.endpoints
        ],
    )


def _identity(fragment):
    """What names a fragment: its keys and aliases and its endpoints' keys, aliases and kinds."""
    return (
        fragment.key,
        fragment.aliases,
        fragment.default_endpoint,
        [(e.key, e.aliases, e.kind, e.requirements) for e in fragment.endpoints],
    )


def _build_failing(monkeypatch, package, error):
    """Build the manifest while loading ``package``'s ``_config`` raises ``error``."""
    real = manifest._load_config

    def load(name):
        if name == package:
            raise error
        return real(name)

    get_provider_manifest.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(manifest, "_load_config", load)
            return get_provider_manifest()
    finally:
        get_provider_manifest.cache_clear()


def _isolate(monkeypatch, package, error):
    """Return the plain manifest, the one built with ``package`` failing, and its row position."""
    positions = [i for i, row in enumerate(manifest._DECLARATIONS) if row.package == package]
    assert len(positions) == 1
    get_provider_manifest.cache_clear()
    baseline = get_provider_manifest()
    return baseline, _build_failing(monkeypatch, package, error), positions[0]


@pytest.mark.parametrize("row", manifest._DECLARATIONS, ids=lambda row: row.configs)
def test_row_identity_matches_its_config_enum(row):
    configs = getattr(manifest._load_config(row.package), row.configs)
    first = configs[row.endpoints[0].member]
    assert row.key == first.provider
    assert row.aliases == tuple(first.provider_aliases)
    for endpoint in row.endpoints:
        member = configs[endpoint.member]
        assert endpoint.key == member.endpoint_path
        assert endpoint.aliases == tuple(member.aliases)
        assert endpoint.kind == member.endpoint_type.value


@pytest.mark.parametrize("error_type", [ImportError, ModuleNotFoundError])
@pytest.mark.parametrize(("package", "windowed"), [("groq", True), ("ollama", False)])
def test_failed_config_isolates_its_provider(monkeypatch, package, windowed, error_type):
    error = error_type(f"{package} dependency is missing")
    baseline, patched, index = _isolate(monkeypatch, package, error)

    assert patched.source == baseline.source
    assert patched.context_window_order == baseline.context_window_order
    assert len(patched.fragments) == len(baseline.fragments)
    for position, (after, before) in enumerate(zip(patched.fragments, baseline.fragments)):
        if position != index:
            assert _describe(after) == _describe(before)

    after, before = patched.fragments[index], baseline.fragments[index]
    assert (before.context_windows is not None) is windowed
    assert _identity(after) == _identity(before)
    assert after.context_windows is None
    assert all(not endpoint.settings for endpoint in after.endpoints)


def test_failed_provider_factories_raise_activation_error_and_import_nothing(monkeypatch):
    error = ImportError("ollama dependency is missing")
    _, patched, index = _isolate(monkeypatch, "ollama", error)
    fragment = patched.fragments[index]
    real_import = importlib.import_module

    def guarded(name, package=None):
        assert not name.startswith("lionhub"), f"unexpected import of {name}"
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded)
    assert fragment.endpoints
    for endpoint in fragment.endpoints:
        with pytest.raises(ProviderActivationError) as raised:
            endpoint.factory()
        assert raised.value.reason == "activation_failed"
        assert raised.value.provider == fragment.key
        assert raised.value.endpoint == endpoint.key
        assert raised.value.source == patched.source
        assert raised.value.__cause__ is error


def test_other_config_errors_still_propagate(monkeypatch):
    with pytest.raises(RuntimeError, match="broken config"):
        _build_failing(monkeypatch, "ollama", RuntimeError("broken config"))
