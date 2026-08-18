# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fresh-process characterization of the legacy provider runtime authority.

The plugin filesystem/trust matrix remains owned by
``test_plugin_provider_consumer.py``. In particular, its built-in-collision and
noncolliding-sibling tests pin the current fail-soft behavior without repeating
that heavyweight setup here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKED_INVENTORY = REPO_ROOT / "tests" / "contracts" / "data" / "provider_authority_inventory.json"


def _run_fresh(
    script: str,
    tmp_path: Path,
    *,
    hash_seed: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run one isolated interpreter rooted outside the repository."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env.update(
        {
            "LIONAGI_HOME": str(tmp_path / ".lionagi"),
            "PROVIDER_AUTHORITY_INVENTORY_PATH": str(CHECKED_INVENTORY),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": str(hash_seed),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    return subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        text=True,
    )


def test_checked_inventory_runtime_seam_exists() -> None:
    """The runtime contract has one checked, schema-versioned comparison seam."""
    assert CHECKED_INVENTORY.is_file(), f"missing checked provider inventory: {CHECKED_INVENTORY}"
    payload = json.loads(CHECKED_INVENTORY.read_text(encoding="utf-8"))

    assert payload["schema"] == "lionagi.provider-authority-inventory"
    assert payload["version"] == 1


def test_fresh_runner_strips_parent_python_injection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    monkeypatch.setenv("PYTHONPATH", "/tmp/parent-python-path")
    monkeypatch.setenv("PYTHONUSERBASE", "/tmp/parent-python-user-base")

    _run_fresh(
        """
        import os
        import site
        import sys
        from pathlib import Path

        assert sys.flags.optimize == 0
        assert site.ENABLE_USER_SITE is False
        inventory_path = Path(os.environ["PROVIDER_AUTHORITY_INVENTORY_PATH"])
        assert os.environ["PYTHONPATH"] == str(inventory_path.parents[3])
        assert "PYTHONUSERBASE" not in os.environ
        """,
        tmp_path,
    )


def test_config_import_is_inert_but_implementation_import_and_reload_append(
    tmp_path: Path,
) -> None:
    _run_fresh(
        """
        import importlib

        from lionagi.service.connections.registry import EndpointRegistry

        assert EndpointRegistry._entries == []
        assert EndpointRegistry._loaded is False

        config = importlib.import_module("lionagi.providers.openai._config")
        assert EndpointRegistry._entries == []
        importlib.reload(config)
        assert EndpointRegistry._entries == []

        implementation = importlib.import_module("lionagi.providers.openai.chat")
        assert len(EndpointRegistry._entries) == 1
        first = EndpointRegistry._entries[0]
        assert (first.meta.provider, first.meta.endpoint) == ("openai", "chat/completions")

        importlib.reload(implementation)
        assert len(EndpointRegistry._entries) == 2
        second = EndpointRegistry._entries[1]
        assert first.meta == second.meta
        assert first.cls is not second.cls
        assert EndpointRegistry._loaded is False
        """,
        tmp_path,
    )


def test_no_hint_context_lookup_imports_all_sources_and_mutates_registry(
    tmp_path: Path,
) -> None:
    _run_fresh(
        """
        import sys

        from lionagi.service.connections.registry import EndpointRegistry
        from lionagi.service import token_budget

        expected_modules = (
            "lionagi.providers.openai._config",
            "lionagi.providers.anthropic.messages",
            "lionagi.providers.anthropic.claude_code",
            "lionagi.providers.openai.codex",
            "lionagi.providers.deepseek.chat",
            "lionagi.providers.nvidia_nim.chat",
            "lionagi.providers.perplexity.chat",
            "lionagi.providers.google.gemini_code",
            "lionagi.providers.pi.cli",
            "lionagi.providers.groq.chat",
            "lionagi.providers.google.chat",
            "lionagi.providers.openrouter.chat",
        )
        assert tuple(token_budget._PROVIDER_MODULES.values()) == expected_modules
        assert EndpointRegistry._entries == []
        assert token_budget._provider_cache == {}

        assert token_budget.lookup_context_window("totally-unknown-model") == 128_000

        observed_modules = tuple(name for name in sys.modules if name in expected_modules)
        assert observed_modules == expected_modules
        assert tuple(token_budget._provider_cache) == tuple(token_budget._PROVIDER_MODULES)
        assert EndpointRegistry._loaded is False
        assert [(entry.meta.provider, entry.meta.endpoint) for entry in EndpointRegistry._entries] == [
            ("anthropic", "messages"),
            ("claude_code", "query_cli"),
            ("codex", "query_cli"),
            ("deepseek", "chat/completions"),
            ("nvidia_nim", "chat/completions"),
            ("perplexity", "chat/completions"),
            ("gemini_code", "query_cli"),
            ("pi", "query_cli"),
            ("groq", "chat/completions"),
            ("gemini", "chat/completions"),
            ("openrouter", "chat/completions"),
        ]
        """,
        tmp_path,
    )


def test_context_lookup_preserves_gemini_hint_and_global_precedence(tmp_path: Path) -> None:
    _run_fresh(
        """
        from lionagi.service.token_budget import lookup_context_window

        assert lookup_context_window("gemini-2.5-pro") == 2_097_152
        assert lookup_context_window("gemini-2.5-pro", provider="gemini") == 1_048_576
        assert lookup_context_window("gemini-2.5-pro", provider="gemini-api") == 2_097_152
        """,
        tmp_path,
    )


def test_public_provider_authority_imports_and_signatures_are_exact(tmp_path: Path) -> None:
    _run_fresh(
        """
        import inspect

        from lionagi import iModel as root_imodel
        from lionagi.service.connections import (
            EndpointRegistry,
            match_endpoint,
            register_endpoint,
        )
        from lionagi.service.connections.match_endpoint import match_endpoint as defined_match
        from lionagi.service.connections.provider_config import ProviderConfig
        from lionagi.service.connections.registry import (
            EndpointRegistry as DefinedEndpointRegistry,
        )
        from lionagi.service.connections.registry import register_endpoint as defined_register
        from lionagi.service.imodel import iModel
        from lionagi.service.token_budget import get_context_window, lookup_context_window

        assert EndpointRegistry is DefinedEndpointRegistry
        assert register_endpoint is defined_register
        assert match_endpoint is defined_match
        assert root_imodel is iModel

        register_signature = (
            "(provider: 'str', endpoint: 'str', aliases: 'list[str] | None' = None, "
            "endpoint_type: 'EndpointType' = <EndpointType.API: 'api'>, "
            "provider_aliases: 'list[str] | None' = None, "
            "options: 'type[BaseModel] | None' = None, base_url: 'str | None' = None, "
            "auth_type: 'str | None' = None, content_type: 'str | None' = None, "
            "api_key_env: 'str | None' = None)"
        )
        assert str(inspect.signature(EndpointRegistry.register)) == register_signature
        assert str(inspect.signature(register_endpoint)) == register_signature
        assert str(inspect.signature(EndpointRegistry.match)) == (
            "(provider: 'str', endpoint: 'str' = '', *, "
            "openai_compatible: 'bool' = False, **kwargs) -> 'Any'"
        )
        assert str(inspect.signature(EndpointRegistry.list_providers)) == (
            "() -> 'list[dict[str, Any]]'"
        )
        assert str(inspect.signature(ProviderConfig.register)) == "(self, cls=None)"
        assert str(inspect.signature(match_endpoint)) == (
            "(provider: str, endpoint: str, *, openai_compatible: bool = False, **kwargs) "
            "-> lionagi.service.connections.endpoint.Endpoint"
        )
        assert str(inspect.signature(iModel.__init__)) == (
            "(self, provider: 'str | None' = None, base_url: 'str | None' = None, "
            "endpoint: 'str | Endpoint' = 'chat', api_key: 'str | None' = None, "
            "queue_capacity: 'int | None' = None, capacity_refresh_time: 'float' = 60, "
            "interval: 'float | None' = None, limit_requests: 'int | None' = None, "
            "limit_tokens: 'int | None' = None, concurrency_limit: 'int | None' = None, "
            "streaming_process_func: 'Callable | None' = None, "
            "provider_metadata: 'dict | None' = None, "
            "hook_registry: 'HookRegistry | dict | None' = None, exit_hook: 'bool' = False, "
            "id: 'UUID | str | None' = None, created_at: 'float | None' = None, "
            "**kwargs) -> 'None'"
        )
        assert str(inspect.signature(lookup_context_window)) == (
            "(model_name: 'str', provider: 'str | None' = None) -> 'int'"
        )
        assert str(inspect.signature(get_context_window)) == "(branch: 'Branch') -> 'int'"
        assert EndpointRegistry._entries == []
        assert EndpointRegistry._loaded is False
        """,
        tmp_path,
    )


def test_list_providers_has_exact_legacy_projection_and_stable_order(tmp_path: Path) -> None:
    script = """
        import json
        import os
        from pathlib import Path

        from lionagi.service.connections.registry import EndpointRegistry

        inventory = json.loads(
            Path(os.environ["PROVIDER_AUTHORITY_INVENTORY_PATH"]).read_text(encoding="utf-8")
        )
        expected_rows = [
            {
                "provider": row["provider"],
                "endpoint": row["endpoint"],
                "aliases": row["endpoint_aliases"],
                "type": row["endpoint_type"],
                "class": row["implementation_ref"].rsplit(":", 1)[-1],
                "options": (
                    row["options_ref"].rsplit(":", 1)[-1]
                    if row["options_ref"] is not None
                    else None
                ),
            }
            for row in inventory["endpoint_rows"]
        ]
        rows = EndpointRegistry.list_providers()
        assert len(rows) == 36
        assert len({row["provider"] for row in rows}) == 18
        assert all(
            tuple(row) == ("provider", "endpoint", "aliases", "type", "class", "options")
            for row in rows
        )
        assert rows == expected_rows
        assert EndpointRegistry._loaded is True
        print(json.dumps(rows, separators=(",", ":")))
    """
    first = _run_fresh(script, tmp_path, hash_seed=0)
    second = _run_fresh(script, tmp_path, hash_seed=1)

    assert first.stdout == second.stdout


def test_full_runtime_match_matrix_selects_inventory_rows_without_provider_construction(
    tmp_path: Path,
) -> None:
    _run_fresh(
        """
        import json
        import os
        from pathlib import Path

        from lionagi.plugins import PluginRegistry
        from lionagi.service.connections.endpoint import Endpoint
        from lionagi.service.connections.registry import EndpointRegistry

        PluginRegistry.active_provider_targets = classmethod(lambda cls: [])
        inventory = json.loads(
            Path(os.environ["PROVIDER_AUTHORITY_INVENTORY_PATH"]).read_text(encoding="utf-8")
        )
        rows = inventory["endpoint_rows"]

        EndpointRegistry._ensure_loaded()
        assert len(EndpointRegistry._entries) == len(rows) == 36

        def marker(index):
            def construct(_config=None, **_kwargs):
                return index

            return construct

        def qualified_ref(value):
            if value is None:
                return None
            return f"{value.__module__}:{value.__qualname__}"

        for index, (entry, row) in enumerate(zip(EndpointRegistry._entries, rows, strict=True)):
            assert {
                "provider": entry.meta.provider,
                "provider_aliases": list(entry.meta.provider_aliases),
                "endpoint": entry.meta.endpoint,
                "endpoint_aliases": list(entry.meta.aliases),
                "endpoint_type": entry.meta.endpoint_type.value,
                "options_ref": qualified_ref(entry.meta.options),
                "implementation_ref": qualified_ref(entry.cls),
                "base_url": entry.meta.base_url,
                "auth_type": entry.meta.auth_type,
                "content_type": entry.meta.content_type,
                "api_key_env": entry.meta.api_key_env,
            } == {key: row[key] for key in (
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
            )}
            entry.cls = marker(index)

        provider_spellings = []
        provider_rows = {}
        for index, row in enumerate(rows):
            provider_rows.setdefault(row["provider"], []).append(index)
            for spelling in (row["provider"], *row["provider_aliases"]):
                if spelling not in provider_spellings:
                    provider_spellings.append(spelling)
                for endpoint in (row["endpoint"], *row["endpoint_aliases"]):
                    assert EndpointRegistry.match(spelling, endpoint) == index

        assert len(provider_spellings) == 31
        for spelling in provider_spellings:
            matching_indices = [
                index
                for index, row in enumerate(rows)
                if spelling == row["provider"] or spelling in row["provider_aliases"]
            ]
            assert EndpointRegistry.match(spelling, "") == matching_indices[0]

            missing = EndpointRegistry.match(spelling, "__missing_endpoint__")
            if len(provider_rows[rows[matching_indices[0]]["provider"]]) == 1:
                assert missing == matching_indices[0]
            else:
                assert type(missing) is Endpoint
                assert missing.config.endpoint == "__missing_endpoint__"
                assert missing.config.openai_compatible is True
        """,
        tmp_path,
    )


def test_registered_matching_preserves_legacy_case_empty_and_miss_quirks(
    tmp_path: Path,
) -> None:
    _run_fresh(
        """
        from lionagi.providers.openai.chat import OpenaiChatEndpoint
        from lionagi.providers.openai.codex import CodexCLIEndpoint
        from lionagi.plugins import PluginRegistry
        from lionagi.service.connections.endpoint import Endpoint
        from lionagi.service.connections.match_endpoint import match_endpoint

        PluginRegistry.active_provider_targets = classmethod(lambda cls: [])

        assert isinstance(match_endpoint("OPENAI", "chat"), OpenaiChatEndpoint)

        endpoint_case_miss = match_endpoint("openai", "CHAT")
        assert type(endpoint_case_miss) is Endpoint
        assert endpoint_case_miss.config.endpoint == "CHAT"
        assert endpoint_case_miss.config.openai_compatible is True

        provider_whitespace_miss = match_endpoint(" openai ", "chat")
        assert type(provider_whitespace_miss) is Endpoint
        assert provider_whitespace_miss.config.provider == "openai"

        empty_endpoint = match_endpoint("openai", "")
        assert isinstance(empty_endpoint, OpenaiChatEndpoint)
        assert empty_endpoint.config.endpoint == "chat/completions"

        arbitrary_single_endpoint = match_endpoint("codex", "not-a-real-endpoint")
        assert isinstance(arbitrary_single_endpoint, CodexCLIEndpoint)
        assert arbitrary_single_endpoint.config.endpoint == "query_cli"

        known_provider_miss = match_endpoint("openai", "not-a-real-endpoint")
        assert type(known_provider_miss) is Endpoint
        assert known_provider_miss.config.provider == "openai"
        assert known_provider_miss.config.endpoint == "not-a-real-endpoint"
        assert known_provider_miss.config.openai_compatible is True
        """,
        tmp_path,
    )


def test_unknown_provider_error_and_compatible_fallback_are_exact(tmp_path: Path) -> None:
    _run_fresh(
        """
        import warnings

        from lionagi.plugins import PluginRegistry
        from lionagi.service.connections.endpoint import Endpoint
        from lionagi.service.connections.match_endpoint import match_endpoint
        from lionagi.service.connections.registry import ProviderNotFoundError

        PluginRegistry.active_provider_targets = classmethod(lambda cls: [])

        expected_error = (
            "no endpoint registered for provider 'unknown-provider'; registered providers: "
            "ag2, anthropic, autogen, claude, claude-code, claude_code, codex, deepseek, "
            "exa, firecrawl, gemini, gemini-api, gemini-cli, gemini-code, gemini_cli, "
            "gemini_code, groq, nim, nvidia, nvidia_nim, ollama, open-router, openai, "
            "openai-codex, openrouter, perplexity, pi, pi-code, pi_code, scripted, tavily. "
            "Pass openai_compatible=True to route unrecognized providers to the generic "
            "OpenAI-compatible endpoint explicitly."
        )
        try:
            match_endpoint("unknown-provider", "chat")
        except ProviderNotFoundError as exc:
            assert type(exc) is ProviderNotFoundError
            assert str(exc) == expected_error
        else:
            raise AssertionError("unknown provider unexpectedly resolved")

        explicit = match_endpoint(
            "unknown-provider",
            "chat",
            openai_compatible=True,
        )
        assert type(explicit) is Endpoint
        assert explicit.config.provider == "unknown-provider"
        assert explicit.config.endpoint == "chat"
        assert explicit.config.openai_compatible is True

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            implicit = match_endpoint(
                "unknown-provider",
                "chat",
                base_url="https://example.invalid/v1",
            )
        assert type(implicit) is Endpoint
        assert len(caught) == 1
        assert caught[0].category is DeprecationWarning
        assert str(caught[0].message) == (
            "provider 'unknown-provider' is not registered; routing to the generic "
            "OpenAI-compatible endpoint because base_url= was given. This implicit fallback "
            "is deprecated -- pass openai_compatible=True explicitly (e.g. "
            "match_endpoint(..., openai_compatible=True)) to silence this warning."
        )
        """,
        tmp_path,
    )
