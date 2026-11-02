# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterize registry projection and matching before authority changes."""

import importlib
import re
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from lionagi.service.connections.registry import EndpointRegistry, EndpointType


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(EndpointRegistry, "_entries", [])
    monkeypatch.setattr(EndpointRegistry, "_alias_owners", {})
    monkeypatch.setattr(EndpointRegistry, "_loaded", True)
    monkeypatch.setattr(EndpointRegistry._plugin_registration, "provenance", None, raising=False)
    return EndpointRegistry


class _Echo:
    def __init__(self, config, **kwargs):
        self.config = config
        self.kwargs = kwargs


@pytest.mark.parametrize(
    ("provider", "endpoint", "matches_first"),
    [
        ("route", "", True),
        ("route", "Alias", True),
        ("route", "alias", False),
        ("route", " Alias ", False),
        (" route ", "Alias", False),
    ],
    ids=["empty-first", "exact-alias", "alias-case", "alias-space", "provider-space"],
)
def test_registered_match_preserves_legacy_boundaries(registry, provider, endpoint, matches_first):
    @registry.register(provider="route", endpoint="first", aliases=["Alias"])
    class First(_Echo):
        pass

    @registry.register(provider="route", endpoint="second")
    class Second(_Echo):
        pass

    marker = object()
    result = registry._match_registered(provider, endpoint, {"marker": marker})

    if matches_first:
        assert type(result) is First
        assert result.config is None
        assert result.kwargs == {"marker": marker}
    else:
        assert result is None


def test_single_endpoint_accepts_an_unmatched_endpoint_string(registry):
    @registry.register(provider="single", endpoint="first")
    class Only(_Echo):
        pass

    assert type(registry._match_registered("single", "unlisted", {})) is Only


def test_list_providers_exposes_only_the_six_existing_fields(registry):
    class Options(BaseModel):
        pass

    @registry.register(
        provider="projection",
        provider_aliases=["projection-alias"],
        endpoint="query",
        aliases=["ask"],
        endpoint_type=EndpointType.AGENTIC,
        options=Options,
        base_url="internal",
        auth_type="bearer",
        content_type="application/json",
        api_key_env="PROJECTION_KEY",
    )
    class Projected(_Echo):
        pass

    assert registry.list_providers() == [
        {
            "provider": "projection",
            "endpoint": "query",
            "aliases": ["ask"],
            "type": "agentic",
            "class": "Projected",
            "options": "Options",
        }
    ]


def test_documented_provider_registers_when_its_endpoint_module_is_imported(
    registry, tmp_path, monkeypatch
):
    from lionhub.providers import manifest

    document = Path(__file__).resolve().parents[3] / "docs/reference/providers.md"
    guide = document.read_text(encoding="utf-8").split("## Adding a new provider", 1)[1]
    config, endpoint, declaration = re.findall(r"```python\n(.*?)\n```", guide, re.DOTALL)[:3]
    package = "_provider_docs_example"
    assert package not in sys.modules
    directory = tmp_path / package / "myprovider"
    directory.mkdir(parents=True)
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "_config.py").write_text(
        config.replace("lionhub.providers.myprovider", package + ".myprovider"), encoding="utf-8"
    )
    (directory / "chat.py").write_text(endpoint, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # The manifest resolves a row's package and endpoint module under this root, so the row the
    # guide shows loads the example package instead of the hub's providers.
    monkeypatch.setattr(manifest, "_PROVIDERS", package)
    namespace = {"_Provider": manifest._Provider, "_Endpoint": manifest._Endpoint}
    exec(declaration, namespace)
    (row,) = namespace["_DECLARATIONS"]

    try:
        importlib.import_module(package + ".myprovider._config")
        assert registry.list_providers() == []

        # The row the guide shows is declared the way the built-ins are: reading the config
        # registers nothing, and calling each declaration's factory registers the provider.
        fragment = manifest._declare_provider(row)
        assert registry.list_providers() == []

        classes = [declared.factory() for declared in fragment.endpoints]
        imported = importlib.import_module(package + ".myprovider.chat")
        assert classes == [imported.MyProviderChatEndpoint]
        assert registry.list_providers() == [
            {
                "provider": "myprovider",
                "endpoint": "chat/completions",
                "aliases": ["chat"],
                "type": "api",
                "class": "MyProviderChatEndpoint",
                "options": "MyChatRequest",
            }
        ]
        assert imported.MyProviderChatEndpoint._ENDPOINT_META.options is imported.MyChatRequest
    finally:
        for name in tuple(sys.modules):
            if name == package or name.startswith(package + "."):
                sys.modules.pop(name)
        importlib.invalidate_caches()
