# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Independent static contract for the production provider authority."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from ._provider_inventory import (
    collect_provider_authority_inventory,
    load_checked_inventory,
)

_ROOT = Path(__file__).resolve().parents[3]
_INVENTORY = _ROOT / "tests" / "contracts" / "data" / "provider_authority_inventory.json"


def _checked_inventory():
    return load_checked_inventory(_INVENTORY)


def _assert_inventory_matches(source_overrides: Mapping[str, str] | None = None) -> None:
    expected = _checked_inventory()
    actual = collect_provider_authority_inventory(_ROOT, source_overrides=source_overrides)
    mismatched = [key for key in expected if actual.get(key) != expected[key]]
    extra = [key for key in actual if key not in expected]
    assert not mismatched and not extra, (
        "provider authority inventory drift: "
        f"mismatched={mismatched or 'none'}, extra={extra or 'none'}"
    )


def _mutate_source(relative: str, old: str, new: str) -> dict[str, str]:
    source = (_ROOT / relative).read_text(encoding="utf-8")
    assert old in source, f"mutation anchor missing from {relative}: {old!r}"
    return {relative: source.replace(old, new, 1)}


def test_checked_inventory_matches_static_production_sources():
    _assert_inventory_matches()


def test_checked_inventory_is_a_closed_literal():
    inventory = _checked_inventory()
    assert tuple(inventory) == (
        "schema",
        "version",
        "bootstrap_modules",
        "optional_dependencies",
        "endpoint_rows",
        "context_windows",
        "documentation_surfaces",
        "public_authority",
        "deletion_ledger",
        "plugin_fail_soft",
    )
    assert len(inventory["bootstrap_modules"]) == 33
    assert inventory["optional_dependencies"] == {}
    assert len(inventory["endpoint_rows"]) == 36
    assert len(inventory["context_windows"]) == 12
    assert len(inventory["documentation_surfaces"]) == 20
    assert all(
        tuple(row)
        == (
            "bootstrap_module",
            "provider",
            "provider_aliases",
            "endpoint",
            "endpoint_aliases",
            "endpoint_type",
            "options_ref",
            "implementation_ref",
            "base_url",
            "auth_type",
            "content_type",
            "api_key_env",
        )
        for row in inventory["endpoint_rows"]
    )
    assert all(
        tuple(context) == ("provider", "source_module", "entries")
        and all(tuple(entry) == ("model", "context_window") for entry in context["entries"])
        for context in inventory["context_windows"]
    )
    assert all(
        tuple(surface) == ("path", "anchor", "token_counts")
        and surface["anchor"].startswith("#")
        and surface["token_counts"]
        and all(count > 0 for count in surface["token_counts"].values())
        for surface in inventory["documentation_surfaces"]
    )
    assert {surface["path"] for surface in inventory["documentation_surfaces"]} == {
        "docs/api/imodel.md",
        "docs/adr/ADR-0027-model-service-facade-and-endpoint-resolution.md",
        "docs/adr/ADR-0052-supported-validation-and-testing-surfaces.md",
        "docs/adr/ADR-0088-plugin-system.md",
        "docs/internals/core.md",
        "docs/internals/runtime.md",
        "docs/migration/0.23.0-to-0.23.1.md",
        "docs/reference/operations-service.md",
        "docs/reference/providers.md",
    }
    assert sum(row["endpoint_type"] == "api" for row in inventory["endpoint_rows"]) == 28
    assert sum(row["endpoint_type"] == "agentic" for row in inventory["endpoint_rows"]) == 8
    assert {row["provider"] for row in inventory["endpoint_rows"]} == {
        "ag2",
        "anthropic",
        "claude_code",
        "codex",
        "deepseek",
        "exa",
        "firecrawl",
        "gemini",
        "gemini_code",
        "groq",
        "nvidia_nim",
        "ollama",
        "openai",
        "openrouter",
        "perplexity",
        "pi",
        "scripted",
        "tavily",
    }
    provider_aliases = {
        alias for row in inventory["endpoint_rows"] for alias in row["provider_aliases"]
    }
    assert len(provider_aliases) == 13
    accepted_provider_spellings = {
        row["provider"] for row in inventory["endpoint_rows"]
    } | provider_aliases
    assert len(accepted_provider_spellings) == 31


def test_collector_does_not_import_provider_implementations():
    before = set(sys.modules)
    collect_provider_authority_inventory(_ROOT)
    imported = {
        module
        for module in set(sys.modules) - before
        if module.startswith(("lionagi.providers.", "lionagi.testing._endpoint"))
    }
    assert imported == set()


@pytest.mark.parametrize(
    ("relative", "old", "new", "section"),
    [
        (
            "lionagi/service/connections/registry.py",
            '        "lionagi.providers.openai.chat",\n',
            "",
            "bootstrap_modules",
        ),
        (
            "lionagi/service/connections/registry.py",
            "    def _claim_provider_identity(",
            "    def _removed_claim_provider_identity(",
            "deletion_ledger",
        ),
        (
            "lionagi/providers/google/_config.py",
            '["gemini-code", "gemini_cli", "gemini-cli"]',
            '["gemini-code", "gemini_clix", "gemini-cli"]',
            "endpoint_rows",
        ),
        (
            "lionagi/service/connections/registry.py",
            "_PROVIDER_OPTIONAL_DEPENDENCIES: dict[str, tuple[str, ...]] = {}\n",
            "_PROVIDER_OPTIONAL_DEPENDENCIES: dict[str, tuple[str, ...]] = "
            '{"lionagi.providers.ollama.chat": ("ollama",)}\n',
            "optional_dependencies",
        ),
        (
            "lionagi/providers/google/gemini_code.py",
            '    "gemini-2.5-pro": 2_097_152,\n',
            '    "gemini-2.5-pro": 2_097_153,\n',
            "context_windows",
        ),
        (
            "lionagi/service/token_budget.py",
            '    "openai": "lionagi.providers.openai._config",\n'
            '    "anthropic": "lionagi.providers.anthropic.messages",\n',
            '    "anthropic": "lionagi.providers.anthropic.messages",\n'
            '    "openai": "lionagi.providers.openai._config",\n',
            "context_windows",
        ),
        (
            "lionagi/providers/openai/_config.py",
            '    "gpt-5.5": 1_000_000,\n    "gpt-5.4-mini": 1_000_000,\n',
            '    "gpt-5.4-mini": 1_000_000,\n    "gpt-5.5": 1_000_000,\n',
            "context_windows",
        ),
        (
            "docs/reference/operations-service.md",
            "provider longest-prefix lookup",
            "provider lookup",
            "documentation_surfaces",
        ),
        (
            "docs/reference/providers.md",
            "| OpenAI |",
            "| OpenAI API |",
            "documentation_surfaces",
        ),
        (
            "docs/api/imodel.md",
            "`EndpointRegistry`",
            "`LegacyEndpointRegistry`",
            "documentation_surfaces",
        ),
        (
            "lionagi/service/connections/registry.py",
            "        rejected: list[_RegistryEntry] = []\n        for entry in cls._entries:\n",
            "        rejected: list[_RegistryEntry] = []\n"
            "        cls._remove_entries(rejected)\n"
            "        for entry in cls._entries:\n",
            "plugin_fail_soft",
        ),
        (
            "lionagi/service/connections/registry.py",
            "        cls._remove_entries(rejected)\n",
            "        pass\n",
            "plugin_fail_soft",
        ),
        (
            "lionagi/service/connections/registry.py",
            "                except ProviderAliasCollisionError as exc:\n",
            "                except ValueError as exc:\n",
            "plugin_fail_soft",
        ),
    ],
    ids=(
        "removed-bootstrap-module",
        "removed-alias-claim-authority",
        "changed-real-provider-alias",
        "added-real-optional-dependency",
        "changed-context-value",
        "changed-context-provider-order",
        "changed-context-entry-order",
        "removed-documentation-token",
        "changed-provider-catalog-doc-row",
        "changed-imodel-matching-doc",
        "moved-plugin-collision-removal-before-collection",
        "removed-plugin-collision-removal",
        "removed-plugin-alias-fail-soft-handler",
    ),
)
def test_source_override_mutations_are_detected(
    relative: str,
    old: str,
    new: str,
    section: str,
):
    overrides = _mutate_source(relative, old, new)
    with pytest.raises(AssertionError, match=section):
        _assert_inventory_matches(overrides)
