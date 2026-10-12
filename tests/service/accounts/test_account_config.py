# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Routed account declarations in AppSettings."""

import json
import os
from decimal import Decimal

import pytest

from lionagi.config import AccountConfigError, AppSettings, ProviderAccount

CANARY = "sk-ant-api03-CANARY0123456789"

_ONE = [{"name": "primary", "key_env": "ANTHROPIC_PRIMARY_KEY", "budget_usd": "50.00"}]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_ACCOUNTS", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _settings(**values):
    return AppSettings(_env_file=None, **values)


def test_no_declaration_is_the_empty_default(clean_env):
    assert _settings().ANTHROPIC_ACCOUNTS == ()


def test_the_environment_declares_accounts_as_a_json_array(clean_env, monkeypatch):
    two = [
        *_ONE,
        {"name": "secondary", "key_env": "anthropic_secondary_key", "budget_usd": "25.000000001"},
    ]
    monkeypatch.setenv("ANTHROPIC_ACCOUNTS", json.dumps(two))
    accounts = _settings().ANTHROPIC_ACCOUNTS
    assert [a.name for a in accounts] == ["primary", "secondary"]
    # A lookup item named in lower case is referenced as itself.
    assert accounts[1].key_env == "anthropic_secondary_key"
    assert accounts[0].budget_usd == Decimal("50.00")
    assert (accounts[0].budget_nusd, accounts[1].budget_nusd) == (50 * 10**9, 25 * 10**9 + 1)


def test_the_process_environment_overrides_dotenv_and_later_files_override_earlier(
    clean_env, monkeypatch
):
    first, second = clean_env / "a.env", clean_env / "b.env"
    first.write_text("ANTHROPIC_ACCOUNTS='" + json.dumps(_ONE) + "'\n")
    renamed = [{**_ONE[0], "name": "later"}]
    second.write_text("ANTHROPIC_ACCOUNTS='" + json.dumps(renamed) + "'\n")
    assert AppSettings(_env_file=(first, second)).ANTHROPIC_ACCOUNTS[0].name == "later"
    monkeypatch.setenv("ANTHROPIC_ACCOUNTS", json.dumps([{**_ONE[0], "name": "process"}]))
    assert AppSettings(_env_file=(first, second)).ANTHROPIC_ACCOUNTS[0].name == "process"


def test_the_dotenv_files_are_captured_as_absolute_paths_at_construction(clean_env):
    settings = AppSettings(_env_file=("rel.env",))
    os.chdir("/")
    assert settings._dotenv_files == (clean_env / "rel.env",)
    assert AppSettings(_env_file=None)._dotenv_files == ()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty-string"),
        pytest.param("null", id="null"),
        pytest.param("{}", id="object"),
        pytest.param("[" + CANARY, id="malformed-json"),
        pytest.param(json.dumps([{**_ONE[0], "extra": CANARY}]), id="unknown-field"),
        pytest.param(json.dumps([{"name": "a", "key_env": "A_KEY"}]), id="missing-field"),
        pytest.param(json.dumps([{**_ONE[0], "name": CANARY}]), id="key-as-name"),
        pytest.param(
            json.dumps([{**_ONE[0], "name": "sk-ant-api03-abc"}]), id="lower-case-key-as-name"
        ),
        pytest.param(json.dumps([{**_ONE[0], "name": "Primary"}]), id="upper-case-name"),
        pytest.param(json.dumps([{**_ONE[0], "name": "a" * 33}]), id="long-name"),
        pytest.param(json.dumps([{**_ONE[0], "key_env": CANARY}]), id="key-as-reference"),
        pytest.param(json.dumps([{**_ONE[0], "key_env": "A" * 65}]), id="long-reference"),
        pytest.param(json.dumps([{**_ONE[0], "key_env": "1KEY"}]), id="reference-shape"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": 50}]), id="number-budget"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": True}]), id="bool-budget"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "0"}]), id="zero-budget"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "-1"}]), id="negative-budget"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "1.0000000001"}]), id="ten-digits"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "1e3"}]), id="exponent"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "NaN"}]), id="nan"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "1_000"}]), id="underscore"),
        pytest.param(json.dumps([{**_ONE[0], "budget_usd": "9223372037"}]), id="over-int64"),
        pytest.param(json.dumps([_ONE[0], {**_ONE[0], "key_env": "OTHER"}]), id="dup-name"),
        pytest.param(json.dumps([_ONE[0], {**_ONE[0], "name": "other"}]), id="dup-reference"),
        pytest.param(
            '[{"name":"a","name":"b","key_env":"K_EY","budget_usd":"1"}]', id="repeated-field"
        ),
    ],
)
def test_invalid_declarations_refuse_without_repeating_the_input(clean_env, monkeypatch, raw):
    monkeypatch.setenv("ANTHROPIC_ACCOUNTS", raw)
    with pytest.raises(AccountConfigError) as caught:
        _settings()
    message = str(caught.value)
    assert message.startswith("ANTHROPIC_ACCOUNTS")
    assert "CANARY" not in message and "sk-ant" not in message
    # Nothing chained: a parser or validator message could repeat the input.
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_the_largest_representable_budget_is_accepted(clean_env, monkeypatch):
    budget = "9223372036.854775807"
    monkeypatch.setenv("ANTHROPIC_ACCOUNTS", json.dumps([{**_ONE[0], "budget_usd": budget}]))
    assert _settings().ANTHROPIC_ACCOUNTS[0].budget_nusd == 2**63 - 1


def test_accounts_passed_directly_are_validated_the_same_way(clean_env):
    assert _settings(ANTHROPIC_ACCOUNTS=_ONE).ANTHROPIC_ACCOUNTS[0].name == "primary"
    with pytest.raises(AccountConfigError):
        _settings(ANTHROPIC_ACCOUNTS=None)
    with pytest.raises(AccountConfigError):
        _settings(ANTHROPIC_ACCOUNTS=[{**_ONE[0], "budget_usd": 1.5}])
    account = ProviderAccount.from_declaration(_ONE[0])
    assert _settings(ANTHROPIC_ACCOUNTS=[account]).ANTHROPIC_ACCOUNTS == (account,)


def test_an_account_carries_no_credential_value(clean_env):
    account = ProviderAccount.from_declaration(_ONE[0])
    assert set(account.model_dump()) == {"name", "key_env", "budget_usd"}
