# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Credential declarations and exact grants define command environments."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest


class _MetadataType(type):
    def __call__(cls, *args, **kwargs):
        raise AssertionError("credential metadata must not construct settings")

    def __getattribute__(cls, name):
        if name in {"SECRET_FIELD", "PLAIN_SETTING"}:
            raise AssertionError("credential metadata must not read setting values")
        return super().__getattribute__(name)


def _settings_metadata(annotation, *, alias=None, prefix=""):
    return _MetadataType(
        "MetadataSettings",
        (),
        {
            "model_fields": {
                "SECRET_FIELD": SimpleNamespace(
                    annotation=annotation,
                    alias=alias,
                    validation_alias=None,
                    serialization_alias=None,
                ),
                "PLAIN_SETTING": SimpleNamespace(annotation=str),
            },
            "model_config": {"env_prefix": prefix},
            "SECRET_FIELD": "synthetic-value-that-must-not-be-read",
            "PLAIN_SETTING": "ordinary-value-that-is-not-a-name",
        },
    )


@pytest.mark.parametrize("shape", ["scalar", "nullable", "union", "annotated"])
def test_credential_names_use_resolved_annotations_and_explicit_metadata(monkeypatch, shape):
    from typing import Annotated, Union

    from pydantic import SecretStr

    from lionagi._command_env import credential_env_names

    annotation = {
        "scalar": SecretStr,
        "nullable": SecretStr | None,
        "union": Union[None, SecretStr],
        "annotated": Annotated[SecretStr | None, "metadata-only"],
    }[shape]

    class NoEnvironmentReads(Mapping):
        def __getitem__(self, key):
            raise AssertionError("name collection must not resolve environment values")

        def __iter__(self):
            raise AssertionError("name collection must not enumerate the environment")

        def __len__(self):
            raise AssertionError("name collection must not inspect the environment")

    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", NoEnvironmentReads())
        result = credential_env_names(
            settings_type=_settings_metadata(annotation),
            provider_env_names=("Provider_Reference",),
            account_env_names=("Gh_Token",),
            lookup_names=("Lookup_Reference",),
            mcp_auth_env_names=("Mcp_Reference",),
            active_reference_names=("Active_Reference", "provider_reference"),
        )

    assert result == frozenset(
        {
            "SECRET_FIELD",
            "PROVIDER_REFERENCE",
            "GH_TOKEN",
            "LOOKUP_REFERENCE",
            "MCP_REFERENCE",
            "ACTIVE_REFERENCE",
            "GITHUB_TOKEN",
            "LIONAGI_STUDIO_AUTH_TOKEN",
        }
    )


@pytest.mark.parametrize("shape", ["string", "union-forward-ref"])
def test_unresolved_secret_annotations_are_refused(shape):
    from typing import ForwardRef, Union

    from pydantic import SecretStr

    from lionagi._command_env import credential_env_names

    annotation = "SecretStr" if shape == "string" else Union[SecretStr, ForwardRef("Other")]
    with pytest.raises(ValueError, match="already be resolved"):
        credential_env_names(settings_type=_settings_metadata(annotation))


@pytest.mark.parametrize("shape", ["alias", "prefix"])
def test_unsupported_settings_name_translations_are_explicitly_refused(shape):
    from pydantic import SecretStr

    from lionagi._command_env import credential_env_names

    metadata = _settings_metadata(
        SecretStr,
        alias="RENAMED_SECRET" if shape == "alias" else None,
        prefix="APP_" if shape == "prefix" else "",
    )
    with pytest.raises(ValueError, match="explicit metadata support"):
        credential_env_names(settings_type=metadata)


def test_owned_sources_supply_names_without_constructing_or_resolving_secrets(monkeypatch):
    import lionagi._settings as settings_source
    import lionagi.config as config
    import lionagi.providers._secret_resolution as secret_resolution
    from lionagi._command_env_metadata import configured_command_env_names
    from lionagi.service.connections.registry import EndpointRegistry

    isolated = config.AppSettings.model_construct(
        ANTHROPIC_ACCOUNTS=(SimpleNamespace(key_env="GH_TOKEN"),)
    )
    monkeypatch.setattr(config, "settings", isolated)
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("name metadata must not construct, discover, or resolve credentials")

    def global_metadata(*, include_project):
        calls.append(include_project)
        assert include_project is False
        return {
            "secrets": {
                "lookup": {
                    "argv": ["/synthetic/not-executed", "{name}"],
                    "names": ["LOOKUP_ONLY"],
                }
            }
        }

    monkeypatch.setattr(config.AppSettings, "__init__", forbidden)
    monkeypatch.setattr(config.AppSettings, "get_secret", forbidden)
    monkeypatch.setattr(settings_source, "load_settings", global_metadata)
    monkeypatch.setattr(secret_resolution, "lookup_one_name", forbidden)
    monkeypatch.setattr(EndpointRegistry, "_ensure_loaded", classmethod(forbidden))
    monkeypatch.setattr(EndpointRegistry, "list_providers", classmethod(forbidden))
    monkeypatch.setattr(
        EndpointRegistry,
        "_entries",
        [
            SimpleNamespace(meta=SimpleNamespace(api_key_env="Registry_Auth")),
            SimpleNamespace(meta=SimpleNamespace(api_key_env=None)),
        ],
    )

    result = configured_command_env_names(
        active_reference_names=("Active_Auth",), mcp_auth_env_names=("Selected_Auth",)
    )

    assert calls == [False]
    assert result == frozenset(
        {
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "OLLAMA_API_KEY",
            "EXA_API_KEY",
            "TAVILY_API_KEY",
            "FIRECRAWL_API_KEY",
            "PERPLEXITY_API_KEY",
            "GROQ_API_KEY",
            "ANTHROPIC_API_KEY",
            "NVIDIA_NIM_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GITHUB_TOKEN",
            "LIONAGI_STUDIO_AUTH_TOKEN",
            "REGISTRY_AUTH",
            "GH_TOKEN",
            "LOOKUP_ONLY",
            "ACTIVE_AUTH",
            "SELECTED_AUTH",
        }
    )


@pytest.mark.parametrize(
    "names,error_type",
    [
        pytest.param("TOKEN", TypeError, id="bare-string"),
        pytest.param(b"TOKEN", TypeError, id="bytes"),
        pytest.param({"TOKEN": "synthetic-not-for-errors"}, TypeError, id="mapping"),
        pytest.param(re.compile("TOKEN"), TypeError, id="compiled-regex"),
        pytest.param(["API_.*"], ValueError, id="pattern-string"),
        pytest.param(["TOKEN=synthetic-not-for-errors"], ValueError, id="value-string"),
        pytest.param(["9TOKEN"], ValueError, id="nonportable"),
    ],
)
def test_operator_allowlist_accepts_only_literal_portable_names(names, error_type):
    from lionagi._command_env import normalize_command_env_allowlist

    with pytest.raises(error_type) as caught:
        normalize_command_env_allowlist(names)
    assert "synthetic-not-for-errors" not in str(caught.value)


def test_operator_allowlist_preserves_case_order_and_exact_deduplication():
    from lionagi._command_env import normalize_command_env_allowlist

    names = ["UPPER", "mixed_Name", "UPPER", "mixed_name"]
    assert normalize_command_env_allowlist(names) == ("UPPER", "mixed_Name", "mixed_name")
    assert names == ["UPPER", "mixed_Name", "UPPER", "mixed_name"]


@pytest.mark.parametrize(
    "grant", ["SOMEVENDOR_API_KEY", "somevendor_api_key"], ids=["upper", "lower"]
)
def test_operator_grants_are_exact_and_never_restore_the_state_store_url(grant):
    from lionagi._command_env import scrub_command_env

    parent = {
        "SOMEVENDOR_API_KEY": "synthetic-upper",
        "somevendor_api_key": "synthetic-lower",
        "LIONAGI_STATE_DB_URL": "sqlite:///synthetic-upper.db",
        "lionagi_state_db_url": "sqlite:///synthetic-lower.db",
        "MY_TOKENIZER_PATH": "literal=path with spaces",
    }
    before = dict(parent)
    result = scrub_command_env(
        parent,
        declared_names=(),
        command_env_allowlist=(
            grant,
            "MISSING_NAME",
            "LIONAGI_STATE_DB_URL",
            "lionagi_state_db_url",
        ),
    )
    assert result == {grant: parent[grant], "MY_TOKENIZER_PATH": parent["MY_TOKENIZER_PATH"]}, (
        "an exact operator grant must not release a case alias or the state-store URL"
    )
    assert parent == before


@pytest.mark.parametrize(
    ("global_metadata", "detail"),
    [("unreadable", "could not be read"), ("invalid-lookup", "secrets.lookup section")],
)
def test_unknown_declarations_refuse_instead_of_using_a_partial_deny_set(
    monkeypatch, global_metadata, detail
):
    import lionagi._settings as settings_source
    from lionagi._command_env_metadata import command_environment
    from lionagi._errors import ConfigurationError

    file_text = "synthetic-value-quoted-by-the-parser"

    def load(*, include_project):
        assert include_project is False
        if global_metadata == "unreadable":
            raise ValueError(f"cannot parse near {file_text}")
        return {"secrets": {"lookup": {"names": "GH_TOKEN"}}}

    monkeypatch.setattr(settings_source, "load_settings", load)

    with pytest.raises(ConfigurationError) as excinfo:
        command_environment()

    message = str(excinfo.value)
    assert "~/.lionagi/settings.yaml" in message
    assert detail in message
    assert file_text not in message
    assert excinfo.value.__cause__ is None
    if global_metadata == "unreadable":
        assert excinfo.value.__suppress_context__ is True


@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
def test_gh_token_is_denied_only_when_declared(declared):
    from lionagi._command_env import scrub_command_env

    parent = {"GH_TOKEN": "synthetic-account-key", "MY_TOKENIZER_PATH": "synthetic-path"}
    result = scrub_command_env(parent, declared_names=("gh_token",) if declared else ())
    assert result.get("GH_TOKEN") == (None if declared else parent["GH_TOKEN"])
    assert result["MY_TOKENIZER_PATH"] == parent["MY_TOKENIZER_PATH"]
    assert parent["GH_TOKEN"] == "synthetic-account-key"


@pytest.mark.parametrize("granted", [False, True], ids=["default", "exact-startup-grant"])
def test_shell_startup_names_require_an_exact_operator_grant(granted):
    from lionagi._command_env import scrub_command_env

    parent = {"BASH_ENV": "/synthetic/bashrc", "ENV": "/synthetic/envrc", "KEEP": "unchanged"}
    result = scrub_command_env(
        parent, declared_names=(), command_env_allowlist=("BASH_ENV", "ENV") if granted else ()
    )
    assert result == (parent if granted else {"KEEP": "unchanged"})
    assert result is not parent


def test_scrub_uses_mcp_pattern_object(monkeypatch):
    from lionagi._command_env import scrub_command_env
    from lionagi.service.connections import mcp_wrapper

    iterations = []
    fully_copied = []

    class ObservedPatterns(frozenset):
        def __iter__(self):
            iterations.append(self)
            yield "API_KEY"
            fully_copied.append(self)
            yield "SECRET"

    patterns = ObservedPatterns(("API_KEY", "SECRET"))
    monkeypatch.setattr(mcp_wrapper, "_SENSITIVE_ENV_PATTERNS", patterns)
    parent = {"FIRST_API_KEY": "synthetic-first", "SECOND_API_KEY": "synthetic-second"}

    assert scrub_command_env(parent, declared_names=()) == {}
    assert len(iterations) == len(parent) and all(item is patterns for item in iterations), (
        "coding policy must iterate the MCP pattern object for each checked name"
    )
    assert fully_copied == [], "coding policy must not copy the MCP pattern catalog"


def test_sensitive_patterns_have_one_literal_owner():
    checkout = Path(__file__).resolve().parents[1]
    owner = checkout / "lionagi/service/connections/mcp_wrapper.py"
    owner_tree = ast.parse(owner.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in ast.walk(owner_tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SENSITIVE_ENV_PATTERNS"
            for target in node.targets
        )
    ]
    assert len(definitions) == 1
    expression = definitions[0].value
    assert isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
    assert expression.func.id == "frozenset" and len(expression.args) == 1
    canonical = set(ast.literal_eval(expression.args[0]))
    assert canonical
    catalogs = []
    assignments = []
    for path in sorted((checkout / "lionagi").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_SENSITIVE_ENV_PATTERNS"
                for target in node.targets
            ):
                assignments.append(path.relative_to(checkout).as_posix())
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset"
                and len(node.args) == 1
                and isinstance(node.args[0], (ast.Set, ast.Tuple, ast.List))
                and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in node.args[0].elts
                )
            ):
                continue
            if set(ast.literal_eval(node.args[0])) == canonical:
                catalogs.append(path.relative_to(checkout).as_posix())
    assert assignments == ["lionagi/service/connections/mcp_wrapper.py"]
    assert catalogs == ["lionagi/service/connections/mcp_wrapper.py"], (
        "the sensitive pattern catalog must have one literal owner"
    )


def test_command_env_import_does_not_load_service(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    for name in ["home", "lionagi", "codex", "tmp"]:
        (tmp_path / name).mkdir()
    program = textwrap.dedent(
        """
        import importlib
        import json
        from pathlib import Path
        import sys

        root = Path(sys.argv[1]).resolve()
        sys.path.insert(0, str(root))
        module = importlib.import_module('lionagi._command_env')
        services = sorted(name for name in sys.modules if name == 'lionagi.service' or name.startswith('lionagi.service.'))
        assert Path(module.__file__).resolve() == root / 'lionagi/_command_env.py'
        assert not services, services
        print(json.dumps({'file': str(Path(module.__file__).resolve()), 'services': services, 'isolated': sys.flags.isolated}))
        """
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LIONAGI_HOME": str(tmp_path / "lionagi"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "TMP": str(tmp_path / "tmp"),
        "TEMP": str(tmp_path / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DOCKER_HOST": "unix:///nonexistent",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(checkout)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "file": str(checkout / "lionagi/_command_env.py"),
        "services": [],
        "isolated": 1,
    }


@pytest.mark.asyncio
async def test_mcp_pattern_override_does_not_narrow_coding_deny(monkeypatch, tmp_path):
    from lionagi.service.connections import mcp_wrapper
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import CodingToolkit

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in [workspace, home, temporary]:
        path.mkdir()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "SOMEVENDOR_API_KEY": "synthetic-pattern-only",
        "MY_TOKENIZER_PATH": "synthetic-benign",
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.chdir(workspace)
    security = mcp_wrapper.MCPSecurityConfig(env_denylist_patterns=frozenset())
    assert (
        mcp_wrapper._filter_env(environment, security)["SOMEVENDOR_API_KEY"]
        == environment["SOMEVENDOR_API_KEY"]
    )
    model = iModel(provider="openai", model="no-request", api_key="dummy-key-for-testing")
    try:
        branch = Branch(chat_model=model)
        toolkit = CodingToolkit(notify=False, workspace_root=workspace, tools=("bash",))
        tools = toolkit.bind(branch, _command_env_names=frozenset())
        bash = next(tool for tool in tools if tool.func_callable.__name__ == "bash")
        result = await asyncio.wait_for(
            bash.func_callable(command="/usr/bin/env", timeout=5000), timeout=10
        )
        assert result["return_code"] == 0, result
        observed = dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)
        assert "SOMEVENDOR_API_KEY" not in observed, (
            "an MCP-only override must not weaken coding command pattern denial"
        )
        assert observed["MY_TOKENIZER_PATH"] == environment["MY_TOKENIZER_PATH"]
        assert environment["SOMEVENDOR_API_KEY"] == "synthetic-pattern-only"
        assert model.calls_sent == 0
    finally:
        await asyncio.wait_for(model.close(), timeout=5)
