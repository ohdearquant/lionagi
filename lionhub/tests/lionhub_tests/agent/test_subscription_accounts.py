# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Named subscription accounts: one directory lists them, a spawn reads the credential into
the child's environment only, and anything that does not resolve refuses the spawn. The
macOS keychain is never read here: the ``security`` call is replaced in every test that
reaches it, and the replacement asserts the exact command it was given."""

from __future__ import annotations

import subprocess

import pytest

import lionhub.agent.subscription_accounts as accounts
from lionhub.agent.subscription_accounts import (
    SubscriptionAccountError,
    account_child_env,
    logins_dir,
    resolve_account,
    resolve_leg_account,
    validate_codex_home,
)

TOKEN = "tok-not-a-real-token-0123456789"


@pytest.fixture
def logins(tmp_path, monkeypatch):
    directory = tmp_path / "logins"
    directory.mkdir()
    monkeypatch.setenv(accounts.LOGINS_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def no_keychain(monkeypatch):
    """Any keychain read fails the test unless a test installs its own stand-in."""

    def refuse(*args, **kwargs):
        raise AssertionError("the keychain was read")

    monkeypatch.setattr(accounts.subprocess, "run", refuse)


def _toml(directory, text: str) -> None:
    (directory / accounts.ACCOUNTS_FILE).write_text(text)


# ---- the accounts directory -------------------------------------------------------------


def test_the_environment_names_the_directory(logins):
    assert logins_dir() == logins


def test_lionagi_homes_env_file_names_it_when_the_environment_does_not(tmp_path, monkeypatch):
    home = tmp_path / "lionagi-home"
    home.mkdir()
    (home / ".env").write_text(f"{accounts.LOGINS_DIR_ENV}={tmp_path / 'from-env-file'}\n")
    monkeypatch.delenv(accounts.LOGINS_DIR_ENV, raising=False)
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home)
    assert logins_dir() == tmp_path / "from-env-file"


def test_a_working_directory_env_file_does_not_name_it(tmp_path, monkeypatch):
    """A checkout under review cannot point a leg at another accounts directory."""
    home = tmp_path / "lionagi-home"
    home.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env").write_text(f"{accounts.LOGINS_DIR_ENV}={checkout / 'planted'}\n")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv(accounts.LOGINS_DIR_ENV, raising=False)
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home)
    assert logins_dir() == home / "logins"


def test_a_relative_directory_is_refused(monkeypatch):
    monkeypatch.setenv(accounts.LOGINS_DIR_ENV, "relative/logins")
    with pytest.raises(SubscriptionAccountError, match="not an absolute path"):
        logins_dir()


# ---- resolving a name -------------------------------------------------------------------


def test_a_claude_token_file_is_an_account(logins):
    (logins / "claude-alpha").write_text(TOKEN + "\n")
    account = resolve_account("alpha", "claude_code")
    assert (account.provider, account.source, account.location) == (
        "claude_code",
        "file",
        (str(logins / "claude-alpha"),),
    )


@pytest.mark.parametrize("provider", ["claude_code", "claude"])
def test_a_keychain_row_is_an_account(logins, provider):
    _toml(logins, '[claude_code.mail]\nkeychain = { service = "svc", account = "acct" }\n')
    account = resolve_account("mail", provider)
    assert (account.source, account.location) == ("keychain", ("svc", "acct"))


def test_a_dotenv_row_is_an_account(logins, tmp_path):
    _toml(logins, f'[claude_code.beta]\ndotenv = {{ file = "{tmp_path / "a.env"}", var = "T" }}\n')
    account = resolve_account("beta", "claude_code")
    assert (account.source, account.location) == ("dotenv", (str(tmp_path / "a.env"), "T"))


@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
def test_a_codex_row_is_an_account_at_the_homes_real_path(logins, tmp_path, provider):
    home = tmp_path / "codex-home"
    home.mkdir()
    link = tmp_path / "codex-link"
    link.symlink_to(home)
    _toml(logins, f'[codex.alt]\nhome = "{link}"\n')
    account = resolve_account("alt", provider)
    assert (account.provider, account.source, account.location) == (
        "codex",
        "home",
        (str(home.resolve()),),
    )


def test_one_name_may_serve_both_providers(logins, tmp_path):
    (logins / "claude-work").write_text(TOKEN)
    home = tmp_path / "home"
    home.mkdir()
    _toml(logins, f'[codex.work]\nhome = "{home}"\n')
    assert resolve_account("work", "claude_code").source == "file"
    assert resolve_account("work", "codex").source == "home"


def test_a_name_with_no_entry_for_the_provider_is_refused_with_no_fallback(logins):
    (logins / "claude-alpha").write_text(TOKEN)
    with pytest.raises(SubscriptionAccountError, match="does not fall back") as refused:
        resolve_account("alpha", "codex")
    assert "[codex.alpha]" in str(refused.value)


def test_a_name_defined_twice_for_claude_code_is_refused(logins):
    (logins / "claude-alpha").write_text(TOKEN)
    _toml(logins, '[claude_code.alpha]\nkeychain = { service = "s", account = "a" }\n')
    with pytest.raises(SubscriptionAccountError, match="defined twice"):
        resolve_account("alpha", "claude_code")


@pytest.mark.parametrize("provider", ["gemini-cli", "anthropic", "openai"])
def test_a_provider_that_takes_no_account_is_refused(logins, provider):
    (logins / "claude-alpha").write_text(TOKEN)
    with pytest.raises(SubscriptionAccountError, match="takes no subscription account"):
        resolve_account("alpha", provider)


@pytest.mark.parametrize("name", ["", "UPPER", "-x", "a/b", "../x", "a b", 7, None])
def test_a_malformed_name_is_refused(logins, name):
    with pytest.raises(SubscriptionAccountError, match="not an account name"):
        resolve_account(name, "claude_code")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[claude_code.x]\n", "exactly one token source"),
        (
            '[claude_code.x]\nkeychain = { service = "s", account = "a" }\n'
            'dotenv = { file = "/f", var = "V" }\n',
            "exactly one token source",
        ),
        ('[claude_code.x]\nkeychain = { service = "s" }\n', "non-empty strings"),
        ('[claude_code.x]\nkeychain = { service = "s", account = "" }\n', "non-empty strings"),
        ('[claude_code.x]\ndotenv = { file = "rel.env", var = "V" }\n', "not absolute"),
        ('[claude_code.x]\ndotenv = { file = "~/a.env", var = "V" }\n', "not absolute"),
        ('[claude_code.x]\ntoken = "inline"\n', "exactly one token source"),
        ('[codex.x]\nhome = "~/codex"\n', "not an absolute path"),
        ('[codex.x]\nhome = "relative"\n', "not an absolute path"),
        ('[codex.x]\nhome = "/does/not/exist/anywhere"\n', "not an existing directory"),
        ('[codex.x]\nhome = "/tmp"\nextra = 1\n', "exactly one key"),
        ("[other.x]\n", "unknown top-level"),
        ("not toml [", "cannot read the accounts file"),
    ],
)
def test_a_malformed_row_is_refused(logins, text, match):
    _toml(logins, text)
    provider = "codex" if "[codex" in text else "claude_code"
    with pytest.raises(SubscriptionAccountError, match=match):
        resolve_account("x", provider)


# ---- the child environment --------------------------------------------------------------


def test_a_claude_token_reaches_the_child_and_nothing_outranks_it(logins, no_keychain):
    (logins / "claude-alpha").write_text(f"  {TOKEN}\n")
    parent = {
        "PATH": "/usr/bin",
        "GH_TOKEN": "kept",
        "ANTHROPIC_API_KEY": "sk-x",
        "anthropic_auth_token": "bearer",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_USE_GATEWAY": "1",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "4",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "5",
        "CLAUDE_CODE_OAUTH_TOKEN": "the-parents-own",
    }
    env, skip = account_child_env(resolve_account("alpha", "claude_code"), parent)
    assert env == {"PATH": "/usr/bin", "GH_TOKEN": "kept", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_FOUNDRY"} <= skip
    assert parent["CLAUDE_CODE_OAUTH_TOKEN"] == "the-parents-own"


def test_an_empty_token_file_is_refused(logins):
    (logins / "claude-alpha").write_text("\n  \n")
    with pytest.raises(SubscriptionAccountError, match="is empty"):
        account_child_env(resolve_account("alpha", "claude_code"), {})


def test_a_codex_home_reaches_the_child(logins, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _toml(logins, f'[codex.alt]\nhome = "{home}"\n')
    parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x", "CODEX_API_KEY": "sk-y"}
    env, skip = account_child_env(resolve_account("alt", "codex"), parent)
    assert env == {"PATH": "/usr/bin", "CODEX_HOME": str(home.resolve())}
    assert skip == {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"}


def test_a_codex_home_removed_after_resolution_is_refused_at_spawn(logins, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _toml(logins, f'[codex.alt]\nhome = "{home}"\n')
    account = resolve_account("alt", "codex")
    home.rmdir()
    with pytest.raises(SubscriptionAccountError, match="not an existing directory"):
        account_child_env(account, {})


def test_a_dotenv_variable_is_read_literally(logins, tmp_path):
    env_file = tmp_path / "tokens.env"
    env_file.write_text("OTHER=x\nT=${HOME}-literal\n")
    _toml(logins, f'[claude_code.beta]\ndotenv = {{ file = "{env_file}", var = "T" }}\n')
    env, _ = account_child_env(resolve_account("beta", "claude_code"), {})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${HOME}-literal"
    assert "OTHER" not in env


@pytest.mark.parametrize(
    ("content", "match"),
    [(None, "does not exist"), ("OTHER=x\n", "does not set it"), ("T=\n", "is empty")],
)
def test_a_dotenv_source_that_cannot_give_a_token_is_refused(logins, tmp_path, content, match):
    env_file = tmp_path / "tokens.env"
    if content is not None:
        env_file.write_text(content)
    _toml(logins, f'[claude_code.beta]\ndotenv = {{ file = "{env_file}", var = "T" }}\n')
    with pytest.raises(SubscriptionAccountError, match=match) as refused:
        account_child_env(resolve_account("beta", "claude_code"), {})
    assert str(env_file) in str(refused.value)
    assert "'T'" in str(refused.value)


# ---- the keychain -----------------------------------------------------------------------


def _keychain_row(logins) -> None:
    _toml(logins, '[claude_code.mail]\nkeychain = { service = "svc-x", account = "acct-y" }\n')


def _stand_in(monkeypatch, result):
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(argv, result[0], result[1], "tool chatter")

    monkeypatch.setattr(accounts.subprocess, "run", run)
    monkeypatch.setattr(accounts.sys, "platform", "darwin")
    return calls


def test_one_named_keychain_item_is_read_with_a_bound_wait(logins, monkeypatch):
    _keychain_row(logins)
    calls = _stand_in(monkeypatch, (0, TOKEN + "\n"))
    env, _ = account_child_env(resolve_account("mail", "claude_code"), {})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    [(argv, kwargs)] = calls
    assert argv == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "svc-x",
        "-a",
        "acct-y",
        "-w",
    ]
    assert kwargs["timeout"] == accounts.KEYCHAIN_TIMEOUT_S
    assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("result", "match"),
    [
        ((44, ""), "is not in the keychain"),
        ((51, ""), "keychain may be locked"),
        ((0, "\n"), "is empty"),
        ((0, " \t\r\n"), "is empty"),
        (subprocess.TimeoutExpired(["security"], 10), "locked or waiting on an access prompt"),
        (FileNotFoundError(), "cannot run /usr/bin/security"),
        (UnicodeDecodeError("utf-8", TOKEN.encode() + b"\xff", 0, 1, "bad"), "is not text"),
    ],
)
def test_a_keychain_item_that_cannot_be_read_refuses_naming_the_item(
    logins, monkeypatch, result, match
):
    _keychain_row(logins)
    _stand_in(monkeypatch, result)
    with pytest.raises(SubscriptionAccountError, match=match) as refused:
        account_child_env(resolve_account("mail", "claude_code"), {})
    message = str(refused.value)
    assert "mail" in message
    assert "tool chatter" not in message
    assert "svc-x" in message and "acct-y" in message
    assert TOKEN not in message
    assert refused.value.__context__ is None or refused.value.__suppress_context__


def test_the_keychain_is_refused_off_macos(logins, monkeypatch, no_keychain):
    _keychain_row(logins)
    monkeypatch.setattr(accounts.sys, "platform", "linux")
    with pytest.raises(SubscriptionAccountError, match="needs the macOS keychain"):
        account_child_env(resolve_account("mail", "claude_code"), {})


# ---- a leg's account --------------------------------------------------------------------


def test_a_leg_with_neither_runs_as_before():
    assert resolve_leg_account("codex") is None


def test_a_leg_account_records_names_only(logins, no_keychain):
    (logins / "claude-alpha").write_text(TOKEN)
    leg = resolve_leg_account("claude_code", account="alpha", parent_env={"PATH": "/usr/bin"})
    assert leg.record == {"account": "alpha", "account_provider": "claude_code"}
    assert TOKEN not in repr(leg.record)
    assert leg.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


def test_a_leg_codex_home_needs_a_codex_model(tmp_path):
    with pytest.raises(SubscriptionAccountError, match="applies to a codex agent"):
        resolve_leg_account("claude_code", codex_home=str(tmp_path))


def test_a_leg_codex_home_records_its_real_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    leg = resolve_leg_account("codex", codex_home=str(home), parent_env={})
    assert leg.env == {"CODEX_HOME": str(home.resolve())}
    assert leg.record == {"codex_home": str(home.resolve()), "account_provider": "codex"}


def test_a_leg_may_not_name_both(tmp_path):
    with pytest.raises(SubscriptionAccountError, match="set one"):
        resolve_leg_account("codex", account="x", codex_home=str(tmp_path))


@pytest.mark.parametrize("raw", ["~/x", "x", "", None, 3])
def test_a_codex_home_must_be_absolute_as_written(raw):
    with pytest.raises(SubscriptionAccountError):
        validate_codex_home(raw)


# ---- what an error or a repr can show ---------------------------------------------------


def test_a_leg_accounts_repr_holds_no_token(logins, no_keychain):
    (logins / "claude-alpha").write_text(TOKEN)
    leg = resolve_leg_account("claude_code", account="alpha", parent_env={})
    assert leg.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in repr(leg) and TOKEN not in str(leg)


def test_a_long_invalid_name_is_not_repeated(logins):
    with pytest.raises(SubscriptionAccountError, match="of 80 characters") as refused:
        resolve_account("X" * 80, "claude_code")
    assert "X" * 80 not in str(refused.value)


def test_a_dangling_token_file_is_an_entry_not_a_gap(logins, tmp_path):
    (logins / "claude-alpha").symlink_to(tmp_path / "gone")
    with pytest.raises(SubscriptionAccountError, match="cannot read"):
        account_child_env(resolve_account("alpha", "claude_code"), {})
    _toml(logins, '[claude_code.alpha]\nkeychain = { service = "s", account = "a" }\n')
    with pytest.raises(SubscriptionAccountError, match="defined twice"):
        resolve_account("alpha", "claude_code")


def test_an_undecodable_lionagi_home_env_file_refuses(tmp_path, monkeypatch):
    import lionagi._paths

    monkeypatch.delenv(accounts.LOGINS_DIR_ENV, raising=False)
    monkeypatch.setattr(lionagi._paths, "LIONAGI_HOME", tmp_path)
    (tmp_path / ".env").write_bytes(b"SECRET=" + TOKEN.encode() + b"\xff\xfe\n")
    with pytest.raises(SubscriptionAccountError, match="cannot read") as refused:
        logins_dir()
    assert TOKEN not in str(refused.value)
    assert refused.value.__suppress_context__
