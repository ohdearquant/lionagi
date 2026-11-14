# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile account integration declarations and model builders."

from __future__ import annotations

import logging

import pytest

from lionagi._errors import ConfigurationError
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


def test_a_keyless_bare_codex_profile_reports_containment():
    profile = _parse_profile("role", "---\nmodel: codex\n---\n\nBody.\n")
    assert profile_config(profile)["contain_account_integrations"] is True


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


@pytest.mark.parametrize(
    "block",
    [
        "contain_account_integrations: true\nDeadline: 2026-02-30\n",
        "contain_account_integrations: true\nnotes: [unfinished\n",
        "# contain_account_integrations: false\nnotes: [unfinished\n",
    ],
    ids=["invalid-date", "syntax", "unreadable-comment"],
)
def test_unreadable_frontmatter_retains_account_containment(block):
    profile = _parse_profile("role", f"---\n{block}---\nBody.\n")
    assert profile.contain_account_integrations is True
    assert profile_config(profile)["contain_account_integrations"] is True
    assert profile.raw_body == "Body."


def test_unclosed_mapping_with_account_containment_retains_it():
    profile = _parse_profile("role", "---\ncontain_account_integrations: true\n")
    assert profile.contain_account_integrations is True


def test_unclosed_readable_mapping_only_counts_account_containment_as_a_key():
    profile = _parse_profile("role", "---\nmodel: codex\n# contain_account_integrations\n")
    assert profile.contain_account_integrations is None


def test_hidden_frontmatter_retains_both_containment_keys():
    profile = _parse_profile(
        "role",
        "Notes ---\ncontain_user_settings: true\ncontain_account_integrations: true\n---\nBody.\n",
    )
    assert profile.contain_user_settings is True
    assert profile.contain_account_integrations is True


def test_misplaced_account_containment_setting_is_refused():
    with pytest.raises(ConfigurationError, match="contain_account_integrations declaration"):
        _parse_profile("role", "Notes.\n---\ncontain_account_integrations: true\n---\nBody.\n")


def test_readable_false_preserves_account_integration_access():
    profile = _profile("contain_account_integrations: false\n")
    assert profile.contain_account_integrations is False
    assert profile_config(profile)["contain_account_integrations"] is False


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
