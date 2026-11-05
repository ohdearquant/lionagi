# SPDX-License-Identifier: Apache-2.0

"""The built-in provider manifest loads no implementation and matches the endpoint registry.

Both checks run in a fresh interpreter: the first measures what importing and calling the
manifest loads, the second compares the manifest with the entries the registry discovers on its
first use. No endpoint module is removed from or run again in the test process.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

from lionagi.service.connections.provider_catalog import compose_provider_catalog
from lionhub.providers.manifest import get_provider_manifest

ROOT = Path(__file__).resolve().parents[3]
_LIONHUB_SRC = str(ROOT / "lionhub" / "src")
_PROVIDERS = "lionhub.providers"

# Every provider package that owns a ``_config`` module.
_CONFIG_PACKAGES = (
    "ag2",
    "anthropic",
    "deepseek",
    "exa",
    "firecrawl",
    "google",
    "groq",
    "lattice",
    "nvidia_nim",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "pi",
    "tavily",
)

# Provider key -> (package, context-window table in its ``_config``). Any other provider
# declares no window table.
_WINDOW_TABLES = {
    "openai": ("openai", "CONTEXT_WINDOWS"),
    "codex": ("openai", "CODEX_CONTEXT_WINDOWS"),
    "anthropic": ("anthropic", "ANTHROPIC_CONTEXT_WINDOWS"),
    "claude_code": ("anthropic", "CLAUDE_CODE_CONTEXT_WINDOWS"),
    "gemini": ("google", "GEMINI_CONTEXT_WINDOWS"),
    "gemini_code": ("google", "GEMINI_CODE_CONTEXT_WINDOWS"),
    "deepseek": ("deepseek", "CONTEXT_WINDOWS"),
    "nvidia_nim": ("nvidia_nim", "CONTEXT_WINDOWS"),
    "perplexity": ("perplexity", "CONTEXT_WINDOWS"),
    "pi": ("pi", "CONTEXT_WINDOWS"),
    "groq": ("groq", "CONTEXT_WINDOWS"),
    "openrouter": ("openrouter", "CONTEXT_WINDOWS"),
}

_PURITY_CHILD = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
config_modules = json.loads(sys.argv[2])
implementation_modules = json.loads(sys.argv[3])

from lionagi.service.connections.registry import EndpointRegistry


def loaded(names):
    return sorted(name for name in names if name in sys.modules)


baseline = len(EndpointRegistry._entries)
manifest_module = importlib.import_module("lionhub.providers.manifest")
imported = {
    "configs": loaded(config_modules),
    "implementations": loaded(implementation_modules),
    "new_entries": len(EndpointRegistry._entries) - baseline,
}

manifest = manifest_module.get_provider_manifest()
declared = [d for fragment in manifest.fragments for d in fragment.endpoints]
diagnostic_modules = sorted({d.diagnostic_id.partition(":")[0] for d in declared})
called = {
    "configs": loaded(config_modules),
    "implementations": loaded(implementation_modules),
    "diagnostic_modules": diagnostic_modules,
    "diagnostic_loaded": loaded(diagnostic_modules),
    "new_entries": len(EndpointRegistry._entries) - baseline,
}

control_endpoint = declared[0]
control_module = control_endpoint.diagnostic_id.partition(":")[0]
control_class = control_endpoint.factory()
control = {
    "module": control_module,
    "module_loaded": control_module in sys.modules,
    "class_module": control_class.__module__,
    "new_entries": len(EndpointRegistry._entries) - baseline,
}
print(json.dumps({"imported": imported, "called": called, "control": control}))
"""

# Compares the manifest with what the registry discovers on first use in a fresh interpreter and
# prints one JSON line: every difference found, and how much was compared.
_PARITY_CHILD = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
providers_package = sys.argv[2]
window_tables = json.loads(sys.argv[3])

from lionagi.service.connections.provider_catalog import (
    DependencyRequirement,
    compose_provider_catalog,
)
from lionagi.service.connections.provider_config import LazyType
from lionagi.service.connections.registry import EndpointRegistry
from lionhub.providers.manifest import get_provider_manifest

OLLAMA = (
    DependencyRequirement(
        distribution="ollama",
        import_roots=("ollama",),
        version_constraint=">=0.6.1",
        install_hint="pip install lionagi[ollama]",
    ),
)
AG2 = (
    DependencyRequirement(
        distribution="ag2",
        import_roots=("autogen",),
        version_constraint=">=0.13.0",
        install_hint="pip install lionagi[ag2]",
    ),
)

mismatches = []


def record(provider, endpoint, field, got, want):
    mismatches.append(
        {
            "provider": provider,
            "endpoint": endpoint,
            "field": field,
            "manifest": repr(got),
            "registry": repr(want),
        }
    )


def expect_equal(provider, endpoint, field, got, want):
    if got != want:
        record(provider, endpoint, field, got, want)


def expect_same(provider, endpoint, field, got, want):
    if got is not want:
        record(provider, endpoint, field, got, want)


def attempt(call):
    try:
        return call()
    except Exception as exc:
        return exc


# The first use of the registry in a fresh interpreter discovers the built-in providers. The
# entries are grouped before any factory runs, so importing an implementation cannot change the
# order compared. Scripted and plugin entries are not built-in providers.
EndpointRegistry._ensure_loaded()
groups = {}
for entry in EndpointRegistry._entries:
    if entry.plugin_name is not None or entry.meta.provider == "scripted":
        continue
    groups.setdefault(entry.meta.provider, []).append(entry)

manifest = get_provider_manifest()
expect_same("manifest", None, "cached", get_provider_manifest(), manifest)
expect_equal("manifest", None, "contract_version", manifest.contract_version, 1)
expect_equal("manifest", None, "source", manifest.source, "lionhub.providers")
expect_equal(
    "manifest", None, "fragment_keys", [f.key for f in manifest.fragments], list(groups)
)
catalog_keys = attempt(lambda: [p.key for p in compose_provider_catalog(manifest).providers])
expect_equal("manifest", None, "catalog_keys", catalog_keys, list(groups))

compared = 0
for fragment in manifest.fragments:
    provider = fragment.key
    entries = groups.get(provider, [])
    expect_equal(provider, None, "contract_version", fragment.contract_version, 1)
    expect_equal(provider, None, "endpoint_count", len(fragment.endpoints), len(entries))
    expect_equal(
        provider,
        None,
        "default_endpoint",
        fragment.default_endpoint,
        entries[0].meta.endpoint if entries else None,
    )

    # The fragment keeps a read-only copy of the table, so the check is equality.
    if provider in window_tables:
        package, name = window_tables[provider]
        config = importlib.import_module(providers_package + "." + package + "._config")
        table = getattr(config, name)
        expect_equal(provider, None, "window_table_populated", bool(table), True)
        expect_equal(provider, None, "context_windows", fragment.context_windows, table)
    else:
        expect_same(provider, None, "context_windows", fragment.context_windows, None)

    # A count mismatch is reported above; zip stops at the shorter side and ``compared`` falls
    # short of the expected total.
    for declaration, entry in zip(fragment.endpoints, entries):
        meta = entry.meta
        endpoint = meta.endpoint
        expect_equal(
            provider, endpoint, "provider_aliases", fragment.aliases, meta.provider_aliases
        )
        expect_equal(provider, endpoint, "key", declaration.key, meta.endpoint)
        expect_equal(provider, endpoint, "aliases", declaration.aliases, tuple(meta.aliases))
        expect_equal(provider, endpoint, "kind", declaration.kind, meta.endpoint_type.value)

        settings = declaration.settings
        expect_equal(
            provider,
            endpoint,
            "settings_keys",
            sorted(settings),
            ["api_key_env", "auth_type", "base_url", "content_type", "request_options"],
        )
        for setting in ("base_url", "auth_type", "content_type", "api_key_env"):
            expect_equal(
                provider,
                endpoint,
                "settings." + setting,
                settings.get(setting),
                getattr(meta, setting),
            )
        options = settings.get("request_options")
        resolved = attempt(options.resolve) if isinstance(options, LazyType) else options
        expect_same(provider, endpoint, "settings.request_options", resolved, meta.options)

        expect_same(provider, endpoint, "factory", attempt(declaration.factory), entry.cls)
        diagnostic_id = entry.cls.__module__ + ":" + entry.cls.__qualname__
        expect_equal(provider, endpoint, "diagnostic_id", declaration.diagnostic_id, diagnostic_id)
        if declaration.diagnostic_id not in repr(declaration.factory):
            record(
                provider,
                endpoint,
                "factory_repr",
                repr(declaration.factory),
                "a repr containing " + declaration.diagnostic_id,
            )

        if provider == "ollama":
            requirements = OLLAMA
        elif provider == "ag2" and not declaration.diagnostic_id.endswith(":AG2NlipEndpoint"):
            requirements = AG2
        else:
            requirements = ()
        expect_equal(provider, endpoint, "requirements", declaration.requirements, requirements)
        compared += 1

counts = {"fragments": len(manifest.fragments), "compared": compared}
print(json.dumps({"mismatches": mismatches, "counts": counts}))
"""


def _run_child(code: str, *args: str) -> dict:
    """Run ``code`` in a fresh interpreter with this process's environment; parse its JSON line."""
    proc = subprocess.run(  # noqa: S603 -- this interpreter, fixed script
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _describe(mismatch: dict) -> str:
    """One line naming the provider, endpoint and field that differ, with both values."""
    where = mismatch["provider"]
    if mismatch["endpoint"] is not None:
        where = f"{where}/{mismatch['endpoint']}"
    return (
        f"{where} {mismatch['field']}: "
        f"manifest={mismatch['manifest']} registry={mismatch['registry']}"
    )


def test_manifest_import_and_call_load_no_implementation():
    config_modules = sorted(f"{_PROVIDERS}.{package}._config" for package in _CONFIG_PACKAGES)
    implementation_modules = sorted(
        {
            declaration.diagnostic_id.partition(":")[0]
            for fragment in get_provider_manifest().fragments
            for declaration in fragment.endpoints
        }
    )
    # A shrinking population would make the absence checks below vacuous.
    assert len(config_modules) == 15
    assert len(implementation_modules) == 34

    result = _run_child(
        _PURITY_CHILD,
        _LIONHUB_SRC,
        json.dumps(config_modules),
        json.dumps(implementation_modules),
    )

    assert result["imported"] == {"configs": [], "implementations": [], "new_entries": 0}
    # Calling the function reads the configuration modules and nothing more.
    assert result["called"]["configs"] == config_modules
    assert result["called"]["implementations"] == []
    assert result["called"]["diagnostic_modules"] == implementation_modules
    assert result["called"]["diagnostic_loaded"] == []
    assert result["called"]["new_entries"] == 0
    # Calling one factory must be visible to the same checks.
    assert result["control"]["module_loaded"] is True
    assert result["control"]["class_module"] == result["control"]["module"]
    assert result["control"]["new_entries"] > 0


def test_manifest_matches_registry_registration():
    result = _run_child(_PARITY_CHILD, _LIONHUB_SRC, _PROVIDERS, json.dumps(_WINDOW_TABLES))

    mismatches = result["mismatches"]
    assert not mismatches, "manifest differs from the registry:\n" + "\n".join(
        _describe(mismatch) for mismatch in mismatches
    )
    # A shrinking registry population would make the comparisons above vacuous.
    assert result["counts"] == {"fragments": 18, "compared": 37}


# The order in which an unhinted context-window lookup scans the providers' tables.
_WINDOW_ORDER = (
    "openai",
    "anthropic",
    "claude_code",
    "codex",
    "deepseek",
    "nvidia_nim",
    "perplexity",
    "gemini_code",
    "pi",
    "groq",
    "gemini",
    "openrouter",
)


def test_catalog_composed_from_the_manifest_scans_context_windows_in_the_declared_order():
    manifest = get_provider_manifest()
    order = manifest.context_window_order
    position = {key: index for index, key in enumerate(order)}
    # Every model name a bundled table holds has the same window under the declared order and
    # under the order the fragments are declared in, so the order is measured on probe names
    # instead of on a real model. A provider at position N of the declared order holds the probe
    # names 00..N, each with the window N + 1: probe-NN is then held by every provider declared
    # at NN or later, and the window a scan returns for it names the first of them the scan
    # reached.
    probed = tuple(
        dataclasses.replace(
            fragment,
            context_windows={
                f"probe-{n:02d}": position[fragment.key] + 1
                for n in range(position[fragment.key] + 1)
            },
        )
        if fragment.context_windows is not None
        else fragment
        for fragment in manifest.fragments
    )
    declared = compose_provider_catalog(probed, source=manifest.source, context_window_order=order)
    unordered = compose_provider_catalog(probed, source=manifest.source)

    probes = [f"probe-{n:02d}" for n in range(len(order))]
    in_declared_order = list(range(1, len(order) + 1))
    assert [declared.context_window(probe) for probe in probes] == in_declared_order
    # Without the declared order the scan follows the order the fragments are declared in, which
    # is a different order, so the same probes answer differently.
    assert [unordered.context_window(probe) for probe in probes] != in_declared_order


def test_manifest_declares_a_context_window_order_naming_every_windowed_provider():
    manifest = get_provider_manifest()
    windowed = {fragment.key for fragment in manifest.fragments if fragment.context_windows}

    assert manifest.context_window_order == _WINDOW_ORDER
    assert set(_WINDOW_ORDER) == windowed == set(_WINDOW_TABLES)
