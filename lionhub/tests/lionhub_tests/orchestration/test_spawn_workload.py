# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Workload and tool declarations at the model builder boundary."

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.spawn import (
    _is_light_codex,
    _parse_profile,
    _profile_tool_settings,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._spawn_workload_test_support import _LIGHT_ROLE as _LIGHT_ROLE
from tests._spawn_workload_test_support import _PLAIN_ROLE as _PLAIN_ROLE
from tests._spawn_workload_test_support import _named_as_a_whole_word as _named_as_a_whole_word


def _kwargs(model) -> dict[str, Any]:
    return dict(model.endpoint.config.kwargs)


_TOOL_FIELDS = ("tools", "allowed_tools", "disallowed_tools")


_TOOL_FLAGS = {
    "tools": "--tools",
    "allowed_tools": "--allowedTools",
    "disallowed_tools": "--disallowedTools",
}


_ROLE_TOOLS = {
    "tools": ["Read", "Grep", "Glob"],
    "allowed_tools": ["Read", "Grep"],
    "disallowed_tools": ["WebFetch"],
}


_ROLE_ARGV = {
    "tools": ["Read,Grep,Glob"],
    "allowed_tools": ["Read", "Grep"],
    "disallowed_tools": ["WebFetch"],
}


def _role_declaring(model: str | None = "claude_code/sonnet", **fields):
    """A role whose frontmatter sets each field; JSON is valid YAML for the values."""
    lines = "".join(f"{key}: {json.dumps(value)}\n" for key, value in fields.items())
    head = f"model: {model}\n" if model else ""
    return _parse_profile("role", f"---\n{head}{lines}---\n\nRead.\n")


def _claude_argv(model) -> list[str]:
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    return payload["request"].as_cmd_args()


def _values_after(args: list[str], flag: str) -> list[str]:
    start = args.index(flag) + 1
    end = next((i for i in range(start, len(args)) if args[i].startswith("-")), len(args))
    return args[start:end]


def _both_builders(settings, **kw):
    """The same agent built the two ways a launch builds it."""
    return (
        build_imodel_from_spec("claude_code/sonnet", tool_settings=settings, **kw),
        build_chat_model("claude_code", "sonnet", False, False, None, tool_settings=settings, **kw),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_LIGHT_ROLE, "light"),
        ("---\nworkload: heavy\n---\n", "heavy"),
        (_PLAIN_ROLE, "heavy"),
    ],
)
def test_a_role_declares_its_workload(text, expected):
    profile = _parse_profile("role", text)
    assert profile.workload == expected
    assert "workload" not in profile.extra


@pytest.mark.parametrize("raw", ["lite", "LIGHT", "true", "[light]"])
def test_a_workload_the_role_misspells_reads_as_heavy_with_a_warning(raw, caplog):
    profile = _parse_profile("role", f"---\nworkload: {raw}\n---\n")
    assert profile.workload == "heavy"
    assert "reading it as heavy" in caplog.text


def test_discovery_reports_a_light_role_and_leaves_a_heavy_one_unchanged():
    assert profile_config(_parse_profile("r", _LIGHT_ROLE))["workload"] == "light"
    assert "workload" not in profile_config(_parse_profile("r", _PLAIN_ROLE))


@pytest.mark.parametrize(
    "provider",
    [
        "codex",
        "openai-codex",
        "claude_code",
        "claude-code",
        "claude",
        "gemini_code",
        "gemini-cli",
        "pi",
        "pi-code",
        "pi_code",
    ],
)
def test_a_light_cli_leg_hands_its_workload_to_the_provider(provider):
    model = build_chat_model(provider, "m", False, False, None, workload="light")
    assert _kwargs(model)["workload"] == "light"
    spec_model = build_imodel_from_spec(f"{provider}/m", workload="light")
    assert _kwargs(spec_model)["workload"] == "light"


@pytest.mark.parametrize("provider", ["codex", "openai-codex", "Codex"])
def test_every_codex_spelling_keeps_the_light_class_and_the_containment(provider):
    asked = {
        "workload": "light",
        "contain_account_integrations": True,
        "account_connectors": ("github",),
    }
    for model in (
        build_imodel_from_spec(f"{provider}/gpt-5.5", **asked),
        build_chat_model(provider, "gpt-5.5", False, False, None, **asked),
    ):
        kwargs = _kwargs(model)
        assert kwargs["workload"] == "light"
        assert kwargs["contain_account_integrations"] is True
        assert kwargs["account_connectors"] == ["github"]
        assert _is_light_codex(model)


def test_a_heavy_leg_adds_nothing_to_the_request():
    model = build_imodel_from_spec("codex/m")
    assert "workload" not in _kwargs(model)
    assert "workload" not in _kwargs(build_imodel_from_spec("codex/m", workload="heavy"))


def test_an_api_model_takes_no_workload():
    model = build_imodel_from_spec("openai/gpt-4.1-mini", workload="light")
    assert "workload" not in _kwargs(model)


def test_a_bare_openai_codex_model_builds_an_api_endpoint_without_a_workload():
    """A slashless spec falls through to a bare model name, which iModel resolves through the
    API openai provider: nothing spawns, so there is no workload to hand a CLI."""
    from lionagi.service.connections.registry import EndpointRegistry
    from lionhub.orchestration.spawn import _add_workload_kwarg

    model = build_imodel_from_spec("openai-codex", workload="light")
    assert type(model.endpoint).__name__ == "Endpoint"
    assert EndpointRegistry.canonical_provider(model.endpoint.config.provider) == "openai"
    assert "workload" not in _kwargs(model)
    model.endpoint.config._api_key = "dummy-key"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert "workload" not in payload

    kwargs: dict = {}
    _add_workload_kwarg(kwargs, model.endpoint, "light")
    assert "workload" not in kwargs


def test_a_bare_openai_codex_model_takes_no_account_integration_settings():
    """The containment settings are codex CLI options too, and an API request would carry
    them as payload keys."""
    asked = {"contain_account_integrations": True, "account_connectors": ("github",)}
    model = build_imodel_from_spec("openai-codex", **asked)
    assert type(model.endpoint).__name__ == "Endpoint"
    assert not ({"contain_account_integrations", "account_connectors"} & set(_kwargs(model)))
    model.endpoint.config._api_key = "dummy-key"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert not ({"contain_account_integrations", "account_connectors"} & set(payload))


def test_a_slashless_model_on_a_codex_default_provider_keeps_the_containment(monkeypatch):
    """A slashless name takes the configured chat provider; when that is codex, the model is a
    codex agent and gets the containment settings."""
    from lionagi import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "settings",
        config_mod.settings.model_copy(update={"LIONAGI_CHAT_PROVIDER": "codex"}),
    )
    model = build_imodel_from_spec(
        "gpt-5.5", contain_account_integrations=True, account_connectors=("github",)
    )
    assert model.endpoint.is_cli
    assert _kwargs(model)["contain_account_integrations"] is True
    assert _kwargs(model)["account_connectors"] == ["github"]


def test_a_codex_cli_model_carries_the_workload_and_not_to_an_api_request():
    model = build_imodel_from_spec("codex/gpt-5.5", workload="light")
    assert _kwargs(model)["workload"] == "light"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert payload["request"].workload == "light"


@pytest.mark.parametrize("field", _TOOL_FIELDS)
def test_a_roles_tool_field_reaches_a_claude_request_and_its_argv(field):
    settings = _profile_tool_settings(_role_declaring(**{field: _ROLE_TOOLS[field]}))
    for model in _both_builders(settings):
        argv = _claude_argv(model)
        assert _values_after(argv, _TOOL_FLAGS[field]) == _ROLE_ARGV[field]
        for other in set(_TOOL_FIELDS) - {field}:
            assert _TOOL_FLAGS[other] not in argv


def test_a_roles_three_tool_fields_reach_one_claude_request_together():
    settings = _profile_tool_settings(_role_declaring(**_ROLE_TOOLS))
    for model in _both_builders(settings):
        argv = _claude_argv(model)
        for field in _TOOL_FIELDS:
            assert argv.count(_TOOL_FLAGS[field]) == 1
            assert _values_after(argv, _TOOL_FLAGS[field]) == _ROLE_ARGV[field]


def test_a_light_claude_agent_denies_the_roles_tools_and_the_file_writing_ones():
    settings = _profile_tool_settings(_role_declaring(**_ROLE_TOOLS))
    for model in _both_builders(settings, workload="light"):
        argv = _claude_argv(model)
        assert argv.count("--disallowedTools") == 1
        assert _values_after(argv, "--disallowedTools") == [
            "WebFetch",
            "Edit",
            "Write",
            "NotebookEdit",
        ]
        assert _values_after(argv, "--tools") == ["Read,Grep,Glob"]
        assert _values_after(argv, "--allowedTools") == ["Read", "Grep"]


def test_a_role_that_names_no_tool_field_adds_nothing_to_a_claude_request():
    assert _profile_tool_settings(_role_declaring()) == {}
    assert _profile_tool_settings(None) == {}
    assert _profile_tool_settings(SimpleNamespace(name="stub")) == {}
    for settings in (None, {}):
        model = build_imodel_from_spec("claude_code/sonnet", tool_settings=settings)
        assert not set(_TOOL_FLAGS.values()) & set(_claude_argv(model))
        assert not set(_TOOL_FIELDS) & set(_kwargs(model))
        # Nothing to carry, so the legacy builder still answers with the bare spec.
        assert (
            build_chat_model("claude_code", "sonnet", False, False, None, tool_settings=settings)
            == "claude_code/sonnet"
        )


def test_a_roles_tool_names_are_trimmed():
    settings = _profile_tool_settings(
        _role_declaring(tools=[" Read ", "Grep"], allowed_tools=["Bash(git status:*) "])
    )
    assert settings == {"tools": "Read,Grep", "allowed_tools": ["Bash(git status:*)"]}


@pytest.mark.parametrize("field", ["allowed_tools", "disallowed_tools"])
def test_an_empty_allow_or_deny_list_is_a_list_of_nothing(field):
    settings = _profile_tool_settings(_role_declaring(**{field: []}))
    assert settings == {field: []}
    for model in _both_builders(settings):
        assert _TOOL_FLAGS[field] not in _claude_argv(model)


@pytest.mark.parametrize("field", _TOOL_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        "Read",
        "Read, Grep",
        None,
        5,
        True,
        {"Read": True},
        [""],
        ["Read", ""],
        ["  "],
        ["Read", 3],
        ["Read", None],
        [["Read"]],
    ],
    ids=repr,
)
def test_a_tool_field_that_is_not_a_list_of_names_is_refused(field, value):
    role = _role_declaring(**{field: value})
    with pytest.raises(ConfigurationError, match=rf"(?<![a-z_]){field}(?![a-z_])") as refused:
        _profile_tool_settings(role)
    assert "list of" in str(refused.value)


def test_an_empty_tools_list_is_refused_rather_than_read_as_no_restriction():
    with pytest.raises(ConfigurationError, match=r"(?<![a-z_])tools(?![a-z_])"):
        _profile_tool_settings(_role_declaring(tools=[]))


@pytest.mark.parametrize("field", _TOOL_FIELDS)
@pytest.mark.parametrize(
    "spec",
    [
        "codex/gpt-5.5",
        "openai-codex/gpt-5.5",
        "gemini-cli/gemini-3-pro",
        "pi/m",
        "openai/gpt-4.1-mini",
    ],
)
def test_a_role_that_declares_a_tool_field_for_another_provider_is_refused(field, spec):
    from lionagi.service.connections.registry import EndpointRegistry

    settings = _profile_tool_settings(_role_declaring(**{field: _ROLE_TOOLS[field]}))
    provider = EndpointRegistry.canonical_provider(spec.split("/")[0])
    with pytest.raises(ConfigurationError, match=_named_as_a_whole_word(field)) as refused:
        build_imodel_from_spec(spec, tool_settings=settings)
    assert provider in str(refused.value)
    with pytest.raises(ConfigurationError, match=_named_as_a_whole_word(field)) as refused:
        build_chat_model(
            spec.split("/")[0], spec.split("/", 1)[1], False, False, None, tool_settings=settings
        )
    assert provider in str(refused.value)


def test_a_refusal_names_every_tool_field_the_role_declared():
    settings = _profile_tool_settings(_role_declaring(**_ROLE_TOOLS))
    with pytest.raises(ConfigurationError) as refused:
        build_imodel_from_spec("codex/gpt-5.5", tool_settings=settings)
    for field in _TOOL_FIELDS:
        assert field in str(refused.value)


@pytest.mark.parametrize("settings", [None, {}])
@pytest.mark.parametrize("spec", ["codex/gpt-5.5", "openai/gpt-4.1-mini"])
def test_a_role_without_tool_fields_is_not_refused_on_another_provider(spec, settings):
    model = build_imodel_from_spec(spec, tool_settings=settings)
    assert not set(_TOOL_FIELDS) & set(_kwargs(model))
