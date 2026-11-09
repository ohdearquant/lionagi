# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile account integration declarations and model builders."

from __future__ import annotations

import logging

import pytest

from lionhub.orchestration.spawn import (
    AgentProfile,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from tests._spawn_account_integrations_test_support import captor as captor


def _profile(frontmatter: str) -> AgentProfile:
    return _parse_profile("role", f"---\nmodel: codex/gpt-5.5\n{frontmatter}---\n\nBody.\n")


def test_a_codex_profile_without_either_field_reports_the_containment_it_gets():
    profile = _profile("")
    # The profile leaves the key unset; its launches are contained, and discovery says so.
    assert profile.contain_account_integrations is None
    assert profile.account_connectors == ()
    config = profile_config(profile)
    assert config["contain_account_integrations"] is True
    assert "account_connectors" not in config


def test_a_codex_profile_naming_connectors_reports_it_is_not_contained():
    config = profile_config(_profile("account_connectors: [gmail]\n"))
    assert config["contain_account_integrations"] is False
    assert config["account_connectors"] == ["gmail"]


@pytest.mark.parametrize("model", ["claude_code/haiku", "gpt-5"])
def test_another_profile_reports_containment_only_when_it_sets_it(model):
    profile = _parse_profile("role", f"---\nmodel: {model}\n---\n\nBody.\n")
    assert "contain_account_integrations" not in profile_config(profile)
    profile = _parse_profile(
        "role", f"---\nmodel: {model}\ncontain_account_integrations: false\n---\n\nBody.\n"
    )
    assert profile_config(profile)["contain_account_integrations"] is False


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


def test_build_imodel_from_spec_hands_both_fields_to_codex():
    model = build_imodel_from_spec(
        "codex/gpt-5.5",
        account_connectors=("github",),
        contain_account_integrations=True,
    )
    kw = model.endpoint.config.kwargs
    assert kw["account_connectors"] == ["github"]
    assert kw["contain_account_integrations"] is True


def test_build_imodel_from_spec_leaves_both_out_when_unset(captor):
    build_imodel_from_spec("codex/gpt-5.5")
    kw = captor.captures[-1]
    assert "account_connectors" not in kw
    assert "contain_account_integrations" not in kw


def test_build_imodel_from_spec_hands_neither_to_another_provider():
    model = build_imodel_from_spec(
        "claude_code/sonnet",
        account_connectors=("github",),
        contain_account_integrations=True,
    )
    kw = model.endpoint.config.kwargs
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
