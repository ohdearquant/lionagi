# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CodexCodeRequest.contain_account_integrations and account_connectors.

Neither field set leaves the argv as it was. Containment turns the account's
`apps` and `plugins` features off and strips every spelling that would turn
them back on; naming a connector lifts it. The connector names are a grant:
they reach the live call and are never written down.
"""

from __future__ import annotations

import logging

import pytest

from lionagi._errors import CLIInputRefusedError
from lionagi.cli._providers import (
    AgentProfile,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from lionagi.providers.openai.codex import CodexCodeRequest
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.imodel import iModel


def _features_switched(args: list[str]) -> list[tuple[str, str]]:
    return [
        (args[i], args[i + 1])
        for i in range(len(args) - 1)
        if args[i] in ("--enable", "--disable") and args[i + 1] in ("apps", "plugins")
    ]


def _overrides(args: list[str]) -> list[str]:
    return [args[i + 1] for i in range(len(args) - 1) if args[i] == "-c"]


_BUSY = dict(
    prompt="hello",
    enable_features=["apps", "web_search", "plugins"],
    disable_features=["plugins", "shell_snapshot"],
    config_overrides={
        "features.apps": True,
        'features."\\u0070lugins"': True,
        "features": {"apps": True, "web_search": True},
        "model_verbosity": "low",
    },
)


# ── neither field set: today's argv ─────────────────────────────────────


def test_unset_fields_leave_the_argv_unchanged(tmp_path):
    req = CodexCodeRequest(**_BUSY)
    assert req.contain_account_integrations is False
    assert req.account_connectors is None
    args = req.as_cmd_args()
    assert ("--enable", "apps") in _features_switched(args)
    assert ("--enable", "plugins") in _features_switched(args)
    assert "features.apps=true" in _overrides(args)
    assert any(o.startswith("features=") and "apps" in o for o in _overrides(args))
    assert args == CodexCodeRequest(**_BUSY, account_connectors=None).as_cmd_args()

    plain_args = CodexCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
    assert plain_args == [
        "exec",
        "--json",
        "-m",
        "gpt-5.3-codex",
        "--skip-git-repo-check",
        "--disable",
        "tool_search",
        "-C",
        str(tmp_path),
        "--",
        "-",
    ]

    busy_args = CodexCodeRequest(**_BUSY, repo=tmp_path).as_cmd_args()
    assert busy_args == [
        "exec",
        "--json",
        "-m",
        "gpt-5.3-codex",
        "--skip-git-repo-check",
        "--enable",
        "apps",
        "--enable",
        "web_search",
        "--enable",
        "plugins",
        "--disable",
        "plugins",
        "--disable",
        "shell_snapshot",
        "--disable",
        "tool_search",
        "-c",
        "features.apps=true",
        "-c",
        'features."\\u0070lugins"=true',
        "-c",
        "features={ apps = true, web_search = true }",
        "-c",
        'model_verbosity="low"',
        "-C",
        str(tmp_path),
        "--",
        "-",
    ]


def test_unset_fields_do_not_read_an_unparseable_override_key():
    # Without containment no override key is parsed, so a key TOML cannot read
    # reaches the CLI exactly as it did before.
    args = CodexCodeRequest(prompt="hi", config_overrides={"a b": 1}).as_cmd_args()
    assert "a b=1" in _overrides(args)


# ── contained ───────────────────────────────────────────────────────────


def test_containment_disables_both_features_and_strips_every_enabling_spelling():
    args = CodexCodeRequest(**_BUSY, contain_account_integrations=True).as_cmd_args()
    switched = _features_switched(args)
    assert ("--enable", "apps") not in switched
    assert ("--enable", "plugins") not in switched
    assert switched.count(("--disable", "plugins")) == 1
    assert switched.count(("--disable", "apps")) == 1
    # The caller's other features are left as they were.
    assert args[args.index("--enable") + 1] == "web_search"
    assert "shell_snapshot" in args
    overrides = _overrides(args)
    assert not any(o.startswith("features.apps=") for o in overrides)
    assert not any("plugins" in o for o in overrides)
    assert "features={ web_search = true }" in overrides
    assert 'model_verbosity="low"' in overrides


def test_containment_drops_a_features_table_left_empty():
    args = CodexCodeRequest(
        prompt="hi",
        contain_account_integrations=True,
        config_overrides={"features": {"apps": True, "plugins": True}},
    ).as_cmd_args()
    assert not any(o.startswith("features") for o in _overrides(args))


@pytest.mark.parametrize(
    "key",
    ["features.apps", 'features."apps"', "features . apps", 'features."\\u0061pps"'],
)
def test_containment_reads_the_key_as_toml_does(key):
    args = CodexCodeRequest(
        prompt="hi", contain_account_integrations=True, config_overrides={key: True}
    ).as_cmd_args()
    assert _overrides(args) == []
    assert ("--disable", "apps") in _features_switched(args)


def test_toml_decodes_a_quoted_dotted_key():
    import toml

    assert toml.loads('features."apps" = 1') == {"features": {"apps": 1}}
    assert toml.loads("features . apps = 1") != {"features": {"apps": 1}}


def test_containment_keeps_a_nested_key_under_another_table():
    args = CodexCodeRequest(
        prompt="hi",
        contain_account_integrations=True,
        config_overrides={"profiles.work.features.apps": True},
    ).as_cmd_args()
    assert "profiles.work.features.apps=true" in _overrides(args)


def test_containment_drops_a_nested_account_feature_override():
    args = CodexCodeRequest(
        prompt="hi",
        contain_account_integrations=True,
        config_overrides={"features.apps.enabled": True},
    ).as_cmd_args()
    assert _overrides(args) == []


@pytest.mark.parametrize(
    "key,refusal",
    [("a b", "entry 0 is not a TOML key"), ("x = 1\nfeatures.apps", "entry 0 holds '='")],
    ids=["unreadable", "multiline-with-equals"],
)
def test_containment_refuses_an_override_key_it_cannot_read(key, refusal):
    with pytest.raises(CLIInputRefusedError, match=refusal):
        CodexCodeRequest(prompt="hi", contain_account_integrations=True, config_overrides={key: 1})
    copied = CodexCodeRequest(prompt="hi", contain_account_integrations=True).model_copy(
        update={"config_overrides": {key: True}}
    )
    with pytest.raises(ValueError, match=refusal):
        copied.as_cmd_args()


# ── naming a connector lifts the containment ────────────────────────────


def test_named_connectors_leave_the_argv_as_uncontained():
    uncontained = CodexCodeRequest(**_BUSY).as_cmd_args()
    granted = CodexCodeRequest(
        **_BUSY, contain_account_integrations=True, account_connectors=["github"]
    ).as_cmd_args()
    assert granted == uncontained


def test_connector_names_are_stripped_and_deduplicated():
    req = CodexCodeRequest(prompt="hi", account_connectors=[" github", "github ", "gmail"])
    assert req.account_connectors == ["github", "gmail"]


@pytest.mark.parametrize("bad", ["github", [""], ["  "], [1], ["github", None], {"github": 1}])
def test_a_value_that_does_not_read_as_names_is_refused(bad):
    with pytest.raises(ValueError, match="account_connectors must be a list"):
        CodexCodeRequest(prompt="hi", account_connectors=bad)


# ── the grant is never written down ─────────────────────────────────────


def test_grant_reaches_the_live_config_and_no_dump():
    model = iModel(provider="codex", model="gpt-5.5", account_connectors=["github"])
    cfg = model.endpoint.config
    assert cfg.kwargs["account_connectors"] == ["github"]
    assert "account_connectors" not in cfg.model_dump()["kwargs"]
    assert "account_connectors" not in dict(cfg)["kwargs"]
    assert "account_connectors" not in model.to_dict()["endpoint"]["config"]["kwargs"]


def test_a_stored_row_carrying_the_grant_restores_without_it():
    model = iModel(
        provider="codex", model="gpt-5.5", contain_account_integrations=True, verbose_output=True
    )
    data = model.to_dict()
    data["endpoint"]["config"]["kwargs"]["account_connectors"] = ["github"]
    restored = Endpoint.from_dict(data["endpoint"])
    assert "account_connectors" not in restored.config.kwargs
    # The persisted settings around it survive.
    assert restored.config.kwargs["contain_account_integrations"] is True
    assert restored.config.kwargs["verbose_output"] is True
    assert "account_connectors" not in iModel.from_dict(data).endpoint.config.kwargs


# ── role profile ────────────────────────────────────────────────────────


def _profile(frontmatter: str) -> AgentProfile:
    return _parse_profile("role", f"---\nmodel: codex/gpt-5.5\n{frontmatter}---\n\nBody.\n")


def test_profile_without_either_field_reports_what_it_did_before():
    profile = _profile("")
    assert profile.contain_account_integrations is False
    assert profile.account_connectors == ()
    config = profile_config(profile)
    assert "contain_account_integrations" not in config
    assert "account_connectors" not in config


def test_profile_fields_parse_and_stay_out_of_extra():
    profile = _profile(
        "contain_account_integrations: true\naccount_connectors: [github, ' github', gmail]\n"
    )
    assert profile.contain_account_integrations is True
    assert profile.account_connectors == ("github", "gmail")
    assert "contain_account_integrations" not in profile.extra
    assert "account_connectors" not in profile.extra
    config = profile_config(profile)
    assert config["contain_account_integrations"] is True
    assert config["account_connectors"] == ["github", "gmail"]


def test_unreadable_containment_flag_warns_and_contains(caplog):
    with caplog.at_level(logging.WARNING):
        profile = _profile("contain_account_integrations: maybe\n")
    assert profile.contain_account_integrations is True
    assert "contain_account_integrations 'maybe'" in caplog.text


@pytest.mark.parametrize("raw", ["github", "[github, '']", "[1]", "{github: 1}"])
def test_unreadable_connector_list_warns_and_grants_none(raw, caplog):
    with caplog.at_level(logging.WARNING):
        profile = _profile(f"account_connectors: {raw}\n")
    assert profile.account_connectors == ()
    assert "ignoring account_connectors" in caplog.text


# ── the CLI builders hand the fields to codex only ──────────────────────


@pytest.fixture
def captor(monkeypatch):
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)
    return captor


def test_build_imodel_from_spec_hands_both_fields_to_codex(captor):
    build_imodel_from_spec(
        "codex/gpt-5.5",
        account_connectors=("github",),
        contain_account_integrations=True,
    )
    kw = captor.captures[-1]
    assert kw["account_connectors"] == ["github"]
    assert kw["contain_account_integrations"] is True


def test_build_imodel_from_spec_leaves_both_out_when_unset(captor):
    build_imodel_from_spec("codex/gpt-5.5")
    kw = captor.captures[-1]
    assert "account_connectors" not in kw
    assert "contain_account_integrations" not in kw


def test_build_imodel_from_spec_hands_neither_to_another_provider(captor):
    build_imodel_from_spec(
        "claude_code/sonnet",
        account_connectors=("github",),
        contain_account_integrations=True,
    )
    kw = captor.captures[-1]
    assert "account_connectors" not in kw
    assert "contain_account_integrations" not in kw


def test_build_chat_model_hands_both_fields_to_codex(captor):
    build_chat_model(
        "codex",
        "gpt-5.5",
        False,
        False,
        None,
        account_connectors=("github",),
        contain_account_integrations=True,
    )
    kw = captor.captures[-1]
    assert kw["account_connectors"] == ["github"]
    assert kw["contain_account_integrations"] is True
