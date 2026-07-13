# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-provider consumer wiring in ``EndpointRegistry.match``.

On a provider-resolution miss, an active (trusted + enabled + still-trusted)
plugin's declared provider modules are imported and the match is re-run once
before falling back to the generic OpenAI-compatible endpoint (ADR-0088 D3).
A plugin that is untrusted, disabled, or whose declared files changed since
trust was recorded contributes nothing, and the fallback stays byte-for-byte
identical to today's behavior.
"""

from __future__ import annotations

import sys

import pytest

from lionagi.plugins._user_settings import read_user_settings, write_user_settings
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginActivationError, PluginRegistry, PluginState
from lionagi.plugins.trust import trust_plugin
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  providers:
    - module: providers/endpoint.py
"""

PROVIDER_MODULE = """\
from lionagi.service.connections.registry import register_endpoint
from lionagi.service.connections.endpoint import Endpoint


@register_endpoint(provider="{provider}", endpoint="chat")
class PluginProviderEndpoint(Endpoint):
    pass
"""


def _clear_plugin_modules() -> None:
    """Drop every ``sys.modules`` entry left by ``PluginRegistry.activate_target``.

    ``PluginRegistry.reset()`` (already run around every test by the repo-wide
    ``_reset_plugin_registry`` autouse fixture) only clears the registry's own
    scan/activation caches -- the actual module object it installed into
    ``sys.modules`` stays there for the life of the worker process. Without
    this, a module imported by one test would still satisfy
    ``key in sys.modules`` in a later test in the same xdist worker even
    though that later test's plugin was never consulted, which is exactly
    the false-positive this file's "was it actually imported" assertions
    guard against.
    """
    for key in [k for k in sys.modules if k.startswith("_lionagi_plugin_")]:
        del sys.modules[key]


@pytest.fixture(autouse=True)
def _isolate_endpoint_registry():
    """Snapshot/restore ``EndpointRegistry``'s class-level entries around each test.

    A plugin provider module self-registers via ``@register_endpoint`` as a
    process-lifetime side effect of import, same as any built-in provider
    module -- without this, one test's plugin-registered entry would leak
    into every later test in this file (and, if xdist schedules another
    provider-routing test into the same worker, into that file too).
    """
    saved_entries = list(EndpointRegistry._entries)
    saved_loaded = EndpointRegistry._loaded
    _clear_plugin_modules()
    yield
    EndpointRegistry._entries = saved_entries
    EndpointRegistry._loaded = saved_loaded
    _clear_plugin_modules()


def _trust(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _module_key(plugin_name: str) -> str:
    """The exact ``sys.modules`` key ``PluginRegistry.activate_target`` uses for the
    provider module declared in this test file's manifests, so tests can assert
    it either was or was not actually imported (the real side-effect signal)."""
    return f"_lionagi_plugin_{plugin_name}__providers_endpoint.py"


def _write_provider_plugin(
    write_plugin,
    dir_name: str,
    *,
    name: str | None = None,
    provider: str = "acme-llm",
    trust: bool = True,
):
    plugin_name = name or dir_name
    bundle = write_plugin(
        dir_name,
        MANIFEST.format(name=plugin_name),
        files={"providers/endpoint.py": PROVIDER_MODULE.format(provider=provider)},
    )
    if trust:
        _trust(dir_name)
    return bundle


class TestPluginProviderHit:
    def test_active_plugin_supplies_the_missing_provider(self, write_plugin):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "PluginProviderEndpoint"
        assert result.config.provider == "acme-llm"
        assert _module_key("web-research") in sys.modules

    def test_re_running_the_match_finds_the_now_registered_endpoint(self, write_plugin):
        """The second call (after the plugin module import) must return the concrete
        plugin-registered class, not the generic fallback that a naive one-shot scan
        would have produced before the import fired."""
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(first).__name__ == type(second).__name__ == "PluginProviderEndpoint"


class TestPluginProviderMiss:
    def test_genuinely_unknown_provider_falls_back_identically(self, write_plugin):
        # An active plugin exists but declares an unrelated provider -- a miss
        # for a provider no plugin declares must produce the same fallback
        # shape as when no plugin is installed at all.
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        result = match_endpoint(provider="totally-unknown", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "totally-unknown"
        assert result.config.endpoint == "chat"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"

    def test_no_plugins_installed_falls_back_identically(self, write_plugin):
        # `write_plugin` only gives us the isolated HOME; write nothing.
        result = match_endpoint(provider="totally-unknown", endpoint="")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "totally-unknown"
        assert result.config.endpoint == "chat/completions"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"


class TestPluginProviderExclusion:
    def test_untrusted_plugin_is_not_consulted(self, write_plugin):
        _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm", trust=False
        )

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "acme-llm"
        assert _module_key("web-research") not in sys.modules

    def test_disabled_plugin_is_not_consulted(self, write_plugin):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")
        settings = read_user_settings()
        settings.setdefault("plugins", {})["web-research"] = {"enabled": False}
        write_user_settings(settings)
        PluginRegistry.reset()

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules

    def test_changed_plugin_is_not_consulted(self, write_plugin):
        # Edit the declared provider file *after* trust was recorded but
        # before anything ever asked the registry about this plugin -- the
        # content-pinned hash mismatch reverts it to `changed` at discovery.
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )
        (bundle / "providers" / "endpoint.py").write_text(
            PROVIDER_MODULE.format(provider="acme-llm") + "\n# tampered after trust\n"
        )

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules

    def test_incompatible_version_range_is_not_consulted(self, write_plugin):
        bundle = write_plugin(
            "wr",
            (
                'name: web-research\nversion: "0.1.0"\nlionagi: ">=999.0"\n\n'
                "capabilities:\n  providers:\n    - module: providers/endpoint.py\n"
            ),
            files={"providers/endpoint.py": PROVIDER_MODULE.format(provider="acme-llm")},
        )
        _trust("wr")

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules


class TestPluginProviderStaleSnapshotRegression:
    def test_manifest_target_replacement_resolves_without_a_registry_reset(self, write_plugin):
        """``PluginRegistry._snapshot`` is process-cached; a plugin can be edited to
        declare a *different* provider module and re-trusted without anyone calling
        ``PluginRegistry.reset()``. The old target's own failure (no longer declared)
        must not poison resolution of the new one -- enumeration has to come from the
        same fresh rescan as the trust check, not the stale cached manifest."""
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-old"
        )

        # Freeze the process-cached snapshot (state=ACTIVE, manifest declaring
        # providers/endpoint.py) via an unrelated call, before anything changes.
        assert PluginRegistry.get("web-research").state is PluginState.ACTIVE

        (bundle / "providers" / "endpoint_b.py").write_text(
            PROVIDER_MODULE.format(provider="acme-new")
        )
        (bundle / "plugin.yaml").write_text(
            MANIFEST.format(name="web-research").replace(
                "providers/endpoint.py", "providers/endpoint_b.py"
            )
        )
        _trust("wr")

        # The stale, no-longer-declared target fails on its own merits...
        with pytest.raises(PluginActivationError, match="not declared"):
            PluginRegistry.activate_target("web-research", "providers/endpoint.py")

        # ...but that failure must not block resolution of the provider the
        # manifest actually declares now, with no PluginRegistry.reset() in between.
        result = match_endpoint(provider="acme-new", endpoint="chat")

        assert type(result).__name__ == "PluginProviderEndpoint"
        assert result.config.provider == "acme-new"
