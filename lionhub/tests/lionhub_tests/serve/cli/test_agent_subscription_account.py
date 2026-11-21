# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A leg's subscription account comes from ``--account``, else its role's ``account`` or
``codex_home``. It reaches the CLI child's environment on a new run, a resume and every
orchestration worker; the run manifest and the saved branch name it and never hold a token;
and a resume runs on the account the branch last ran on, which only a Claude Code resume may
change."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import lionhub.agent.subscription_accounts as accounts
from lionagi._errors import ConfigurationError
from lionhub.agent.subscription_accounts import SubscriptionAccountError
from lionhub.orchestration.spawn import _parse_profile, build_imodel_from_spec, profile_config
from lionhub.providers._cli_subprocess import reveal_redacted

from .orchestrate.test_worker_artifact_directive import _build_worker
from .test_agent_resume_model_override import _wire_agent_stubs

TOKEN = "tok-not-a-real-token-0123456789"


@pytest.fixture
def logins(tmp_path, monkeypatch):
    directory = tmp_path / "logins"
    directory.mkdir()
    monkeypatch.setenv(accounts.LOGINS_DIR_ENV, str(directory))
    (directory / "claude-alpha").write_text(TOKEN)
    return directory


@pytest.fixture
def codex_homes(tmp_path):
    homes = {}
    for name in ("one", "two"):
        home = tmp_path / f"codex-{name}"
        home.mkdir()
        (home / "config.toml").write_text("")
        homes[name] = home.resolve()
    return homes


def _role(**keys: str) -> str:
    lines = "".join(f"{k}: {v}\n" for k, v in keys.items())
    return f"---\n{lines}---\nYou are a role.\n"


# ---- the profile keys -------------------------------------------------------------------


def test_a_profile_names_an_account_or_a_codex_home():
    with_account = _parse_profile("r", _role(account="alpha"))
    with_home = _parse_profile("r", _role(codex_home="/opt/codex-two"))
    assert (with_account.account, with_account.codex_home) == ("alpha", None)
    assert (with_home.account, with_home.codex_home) == (None, "/opt/codex-two")
    assert profile_config(with_account)["account"] == "alpha"
    assert profile_config(with_home)["codex_home"] == "/opt/codex-two"
    plain = profile_config(_parse_profile("r", _role(model="codex/gpt-5.5")))
    assert "account" not in plain and "codex_home" not in plain
    assert "account" not in with_account.extra and "codex_home" not in with_home.extra


@pytest.mark.parametrize(
    ("keys", "match"),
    [
        ({"account": "alpha", "codex_home": "/opt/x"}, "set one"),
        ({"account": "Not-Valid"}, "not an account name"),
        ({"account": "7"}, "not an account name"),
        ({"codex_home": "~/codex"}, "not an absolute path"),
        ({"codex_home": "relative/codex"}, "not an absolute path"),
    ],
)
def test_a_malformed_profile_account_is_refused_not_ignored(keys, match):
    """Read as unset, the leg would run on this machine's own login: the wrong account."""
    with pytest.raises(SubscriptionAccountError, match=match):
        _parse_profile("r", _role(**keys))


# ---- the flag ---------------------------------------------------------------------------

_CAPTURED: dict[str, Any] = {}


async def _fake_run_agent(model_str, prompt, **kwargs):
    _CAPTURED.update(kwargs)
    return "output", "provider", "branch-id", "completed", "sess-001"


@pytest.mark.parametrize(("argv", "expected"), [([], None), (["--account", "alpha"], "alpha")])
def test_the_flag_reaches_the_run(argv, expected):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.main import main

    _CAPTURED.clear()
    with patch.object(agent_mod, "_run_agent", _fake_run_agent):
        assert main(["agent", "codex", *argv, "do it"]) == 0
    assert _CAPTURED["account"] == expected


@pytest.fixture
def profile_dirs(tmp_path, monkeypatch):
    """A global profile directory under a stand-in home, and a checkout with its own."""
    home = tmp_path / "home"
    (home / ".lionagi" / "agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".lionagi" / "agents").mkdir(parents=True)
    return SimpleNamespace(
        global_agents=home / ".lionagi" / "agents",
        checkout=checkout,
        checkout_agents=checkout / ".lionagi" / "agents",
    )


@pytest.mark.parametrize(
    "keys", [{"account": "alpha"}, {"codex_home": "/opt/codex-two"}], ids=["account", "codex_home"]
)
def test_a_checkout_profile_cannot_choose_the_account(profile_dirs, keys):
    """A profile the checkout carries is refused, naming the file and the key, rather than read
    without the key: read without it, the leg would run on this machine's own login."""
    from lionhub.orchestration.spawn import load_agent_profile

    (key,) = keys
    path = profile_dirs.checkout_agents / "r.md"
    path.write_text(_role(model="codex/gpt-5.5", **keys))
    with pytest.raises(SubscriptionAccountError, match=f"{key}") as caught:
        load_agent_profile("r", cwd=profile_dirs.checkout)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize(
    "keys", [{"account": "alpha"}, {"codex_home": "/opt/codex-two"}], ids=["account", "codex_home"]
)
def test_the_global_profile_chooses_the_account(profile_dirs, keys):
    from lionhub.orchestration.spawn import load_agent_profile

    (key,) = keys
    (profile_dirs.global_agents / "r.md").write_text(_role(model="codex/gpt-5.5", **keys))
    profile = load_agent_profile("r", cwd=profile_dirs.checkout)
    assert getattr(profile, key) == keys[key]


def test_a_checkout_profile_without_the_keys_still_loads(profile_dirs):
    from lionhub.orchestration.spawn import load_agent_profile

    (profile_dirs.checkout_agents / "r.md").write_text(_role(model="codex/gpt-5.5"))
    profile = load_agent_profile("r", cwd=profile_dirs.checkout)
    assert profile.account is None and profile.codex_home is None


# ---- a new run and a resume -------------------------------------------------------------


def _use_role(monkeypatch, text: str) -> None:
    import lionhub.serve.cli.agent as agent_mod

    profile = _parse_profile("role", text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)


def _capture(monkeypatch, tmp_path) -> dict[str, list]:
    """The child environment each turn ran with, the branch metadata, and the manifests."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch

    seen: dict[str, list] = {"env": [], "skip": [], "metadata": [], "manifest": []}

    async def fake_operate(self, instruction=None, **kw):
        state = self.chat_model.endpoint._runtime_state
        seen["env"].append(reveal_redacted(state.get("env")))
        seen["skip"].append(state.get("secret_fill_skip"))
        seen["metadata"].append(dict(self.metadata))
        return "ok"

    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda manifest: seen["manifest"].append(json.dumps(manifest)),
        ),
    )
    return seen


@pytest.mark.parametrize(
    "keys", [{"account": "alpha"}, {"codex_home": "/opt/codex-two"}], ids=["account", "codex_home"]
)
def test_a_plugin_profile_cannot_choose_the_account(tmp_path, monkeypatch, keys):
    import lionhub.orchestration.spawn as spawn

    (key,) = keys
    path = tmp_path / "plugin" / "r.md"
    path.parent.mkdir()
    path.write_text(_role(model="codex/gpt-5.5", **keys))
    monkeypatch.setattr(spawn, "_resolve_plugin_profile_path", lambda name: path)
    with pytest.raises(SubscriptionAccountError, match=key) as caught:
        spawn.load_agent_profile("plug/r", cwd=tmp_path)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize(
    "keys", [{"account": "alpha"}, {"codex_home": "/opt/codex-two"}], ids=["account", "codex_home"]
)
def test_a_checkout_profile_cannot_hide_a_global_one_that_names_an_account(profile_dirs, keys):
    """Found first, a keyless checkout profile of the role's name would move the role off the
    account its global profile names and onto this machine's own login."""
    from lionhub.orchestration.spawn import load_agent_profile

    (key,) = keys
    (profile_dirs.global_agents / "r.md").write_text(_role(model="codex/gpt-5.5", **keys))
    local = profile_dirs.checkout_agents / "r.md"
    local.write_text(_role(model="codex/gpt-5.5"))
    with pytest.raises(SubscriptionAccountError, match=f"hides .*sets {key}") as caught:
        load_agent_profile("r", cwd=profile_dirs.checkout)
    assert str(local) in str(caught.value)


def test_a_checkout_profile_may_hide_a_global_one_that_names_no_account(profile_dirs):
    from lionhub.orchestration.spawn import load_agent_profile

    (profile_dirs.global_agents / "r.md").write_text(_role(model="claude_code/sonnet"))
    (profile_dirs.checkout_agents / "r.md").write_text(_role(model="codex/gpt-5.5"))
    assert load_agent_profile("r", cwd=profile_dirs.checkout).model == "codex/gpt-5.5"


def test_a_refused_profile_is_left_out_of_the_catalog_not_every_role(profile_dirs, monkeypatch):
    """One checkout file choosing an account must not hide every other role from the list."""
    from lionhub.orchestration import spawn
    from lionhub.serve.mcp import roster

    monkeypatch.setattr(spawn, "_plugin_agent_profiles", lambda: {})
    monkeypatch.chdir(profile_dirs.checkout)
    bad = profile_dirs.checkout_agents / "bad.md"
    bad.write_text(_role(model="codex/gpt-5.5", account="alpha"))
    (profile_dirs.checkout_agents / "good.md").write_text(_role(model="codex/gpt-5.5"))
    refused: dict[str, str] = {}
    catalog = spawn.build_agent_profile_catalog(refused)
    assert "good" in catalog and "bad" not in catalog
    assert str(bad) in refused["bad"]
    listed = roster.profile_list(cwd=str(profile_dirs.checkout))
    assert [p["name"] for p in listed["profiles"]] == ["good"]
    assert listed["refused"][0]["name"] == "bad" and str(bad) in listed["refused"][0]["reason"]


def test_the_refusal_says_how_to_fix_it(profile_dirs):
    from lionhub.orchestration.spawn import load_agent_profile

    (profile_dirs.checkout_agents / "r.md").write_text(_role(account="alpha"))
    with pytest.raises(SubscriptionAccountError, match="Move it to a profile there") as caught:
        load_agent_profile("r", cwd=profile_dirs.checkout)
    assert "--account" not in str(caught.value)


def test_a_global_profiles_codex_home_is_denied_to_a_claude_child(profile_dirs, logins, tmp_path):
    home = tmp_path / "codex-for-role"
    home.mkdir()
    nested = profile_dirs.global_agents / "nested"
    nested.mkdir()
    (nested / "nested.md").write_text(_role(model="codex/gpt-5.5", codex_home=str(home)))
    (profile_dirs.global_agents / "plain.md").write_text(_role(model="codex/gpt-5.5"))
    assert str(home) in accounts.credential_paths()


def test_a_checkout_profiles_codex_home_is_not_read_for_the_deny_list(profile_dirs, logins):
    """Only global profiles are honoured, so only they are read; a checkout profile with the key
    is refused when it is loaded instead."""
    (profile_dirs.checkout_agents / "r.md").write_text(
        _role(model="codex/gpt-5.5", codex_home="/opt/codex-local")
    )
    assert "/opt/codex-local" not in accounts.credential_paths()


def test_a_malformed_global_codex_home_refuses_the_deny_list(profile_dirs, logins):
    """Its home could not be named, so a child would be free to read it."""
    (profile_dirs.global_agents / "r.md").write_text(_role(codex_home="relative/codex"))
    with pytest.raises(SubscriptionAccountError, match="not an absolute path"):
        accounts.credential_paths()


@pytest.mark.asyncio
async def test_a_leg_whose_checkout_profile_names_an_account_does_not_start(
    monkeypatch, tmp_path, logins, profile_dirs
):
    from lionhub.serve.cli.agent import _run_agent

    seen = _capture(monkeypatch, tmp_path)
    (profile_dirs.checkout_agents / "role.md").write_text(
        _role(model="claude_code/sonnet", account="alpha")
    )
    monkeypatch.chdir(profile_dirs.checkout)
    with pytest.raises(SubscriptionAccountError, match="account"):
        await _run_agent(None, "do it", agent_name="role")
    assert seen["env"] == [] and seen["manifest"] == []


@pytest.mark.asyncio
async def test_a_claude_account_runs_on_its_token_and_the_record_names_it(
    monkeypatch, tmp_path, logins
):
    from lionhub.serve.cli.agent import _run_agent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-parent-key")
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent("claude_code/sonnet", "do it", account="alpha")

    env = seen["env"][-1]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" in seen["skip"][-1]
    record = {"account": "alpha", "account_provider": "claude_code"}
    assert seen["metadata"][-1]["subscription_account"] == record
    manifest = json.loads(seen["manifest"][0])
    assert {k: manifest[k] for k in record} == record
    assert all(TOKEN not in written for written in seen["manifest"])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
async def test_a_roles_codex_home_runs_the_child_there(
    monkeypatch, tmp_path, codex_homes, provider
):
    from lionhub.serve.cli.agent import _run_agent

    _use_role(monkeypatch, _role(codex_home=str(codex_homes["one"])))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(f"{provider}/gpt-5.5", "do it", agent_name="role")

    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["one"])
    assert json.loads(seen["manifest"][0])["codex_home"] == str(codex_homes["one"])
    assert {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"} <= seen["skip"][-1]


@pytest.mark.asyncio
async def test_a_build_that_drops_the_skip_set_is_refused(monkeypatch, tmp_path, codex_homes):
    """An endpoint that kept the environment but not the skip set would let the secret lookup
    fill a removed credential back into the child."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    build_chat_model = agent_mod.build_chat_model

    def build_without_the_skip_set(*args, **kwargs):
        model = build_chat_model(*args, **kwargs)
        model.endpoint._runtime_state.pop("secret_fill_skip", None)
        return model

    _use_role(monkeypatch, _role(codex_home=str(codex_homes["one"])))
    seen = _capture(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "build_chat_model", build_without_the_skip_set)
    with pytest.raises(ConfigurationError, match="skip set"):
        await _run_agent("codex/gpt-5.5", "do it", agent_name="role")
    assert seen["env"] == []


@pytest.mark.asyncio
async def test_the_flag_wins_over_the_roles_codex_home(monkeypatch, tmp_path, logins, codex_homes):
    from lionhub.serve.cli.agent import _run_agent

    (logins / accounts.ACCOUNTS_FILE).write_text(f'[codex.two]\nhome = "{codex_homes["two"]}"\n')
    _use_role(monkeypatch, _role(codex_home=str(codex_homes["one"])))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent("codex/gpt-5.5", "do it", agent_name="role", account="two")

    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["two"])
    assert seen["metadata"][-1]["subscription_account"] == {
        "account": "two",
        "account_provider": "codex",
        "codex_home": str(codex_homes["two"]),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "account", "match"),
    [
        ("codex/gpt-5.5", "nobody", "does not fall back"),
        ("codex/gpt-5.5", "alpha", "no codex entry"),
        ("gemini-cli/gemini-4-argon", "alpha", "takes no subscription account"),
    ],
)
async def test_an_account_that_does_not_resolve_refuses_before_any_turn(
    monkeypatch, tmp_path, logins, model, account, match
):
    from lionhub.serve.cli.agent import _run_agent

    seen = _capture(monkeypatch, tmp_path)
    with pytest.raises(ConfigurationError, match=match):
        await _run_agent(model, "do it", account=account)
    assert seen["env"] == []
    assert seen["manifest"] == []


def _stored_branch(
    tmp_path: Path,
    record: dict | None,
    *,
    provider: str = "codex",
    model: str = "gpt-5.5",
    switches: list[dict] | None = None,
) -> tuple[str, Path]:
    from lionagi import Branch, iModel

    branch = Branch(chat_model=iModel(provider=provider, model=model))
    if record is not None:
        branch.metadata["subscription_account"] = record
    if switches is not None:
        branch.metadata["subscription_account_switches"] = switches
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(branch.to_dict()))
    return str(branch.id), path


@pytest.mark.asyncio
async def test_a_resume_runs_on_the_account_the_branch_ran_on(monkeypatch, tmp_path, codex_homes):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    branch_id, path = _stored_branch(
        tmp_path, {"codex_home": str(codex_homes["one"]), "account_provider": "codex"}
    )
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id)

    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["one"])
    assert json.loads(seen["manifest"][0])["codex_home"] == str(codex_homes["one"])


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [None, "one"])
async def test_a_resume_on_another_account_is_refused(
    monkeypatch, tmp_path, logins, codex_homes, recorded
):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / accounts.ACCOUNTS_FILE).write_text(f'[codex.two]\nhome = "{codex_homes["two"]}"\n')
    record = (
        None
        if recorded is None
        else {"codex_home": str(codex_homes[recorded]), "account_provider": "codex"}
    )
    branch_id, path = _stored_branch(tmp_path, record)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    with pytest.raises(ConfigurationError, match="cannot be resumed on"):
        await _run_agent(None, "continue", resume=branch_id, account="two")
    assert seen["env"] == []


@pytest.mark.asyncio
async def test_a_resume_with_the_same_account_runs(monkeypatch, tmp_path, logins, codex_homes):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / accounts.ACCOUNTS_FILE).write_text(f'[codex.two]\nhome = "{codex_homes["two"]}"\n')
    branch_id, path = _stored_branch(tmp_path, {"account": "two", "account_provider": "codex"})
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id, account="two")

    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["two"])


def _claude(name: str) -> dict:
    return {"account": name, "account_provider": "claude_code"}


TOKEN_TWO = "tok-not-a-real-token-two-987654"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started_on", "history"), [("alpha", ["alpha", "two"]), (None, [None, "two"])]
)
async def test_a_claude_resume_moves_to_another_account_and_records_both(
    monkeypatch, tmp_path, logins, caplog, started_on, history
):
    """A Claude Code transcript is kept on this machine, so a session moves off an account
    that has run out; the branch keeps the account it started on and appends the new one."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi._log_channels import WARN_LOGGER_NAME
    from lionhub.serve.cli.agent import _run_agent

    (logins / "claude-two").write_text(TOKEN_TWO)
    record = None if started_on is None else _claude(started_on)
    branch_id, path = _stored_branch(tmp_path, record, provider="claude_code", model="sonnet")
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    with caplog.at_level("WARNING", logger=WARN_LOGGER_NAME):
        await _run_agent(None, "continue", resume=branch_id, account="two")

    assert seen["env"][-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_TWO
    metadata = seen["metadata"][-1]
    assert metadata.get("subscription_account") == record
    assert metadata["subscription_account_switches"] == [_claude("two")]
    manifest = json.loads(seen["manifest"][0])
    assert (manifest["account"], manifest["account_history"]) == ("two", history)
    assert all(TOKEN_TWO not in written for written in seen["manifest"])
    changed = [r.getMessage() for r in caplog.records if "account changed" in r.getMessage()]
    assert changed == [f"account changed on resume: {started_on or 'no named account'} -> two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account", "token", "switches", "history"),
    [
        (None, TOKEN_TWO, ["two"], ["alpha", "two"]),
        ("alpha", TOKEN, ["two", "alpha"], ["alpha", "two", "alpha"]),
    ],
    ids=["stays-on-the-latest", "moves-back"],
)
async def test_a_switched_claude_branch_resumes_on_its_latest_account(
    monkeypatch, tmp_path, logins, account, token, switches, history
):
    """With no --account the branch stays on the account it last moved to; moving back
    appends again and rewrites nothing."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / "claude-two").write_text(TOKEN_TWO)
    branch_id, path = _stored_branch(
        tmp_path,
        _claude("alpha"),
        provider="claude_code",
        model="sonnet",
        switches=[_claude("two")],
    )
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id, account=account)

    assert seen["env"][-1]["CLAUDE_CODE_OAUTH_TOKEN"] == token
    metadata = seen["metadata"][-1]
    assert metadata["subscription_account"] == _claude("alpha")
    assert metadata["subscription_account_switches"] == [_claude(name) for name in switches]
    assert json.loads(seen["manifest"][0])["account_history"] == history


@pytest.mark.asyncio
async def test_a_roles_account_does_not_move_a_resumed_branch(monkeypatch, tmp_path, logins):
    """A role's account picks the account of a new run; only --account moves a resumed one."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / "claude-two").write_text(TOKEN_TWO)
    _use_role(monkeypatch, _role(model="claude_code/sonnet", account="two"))
    branch_id, path = _stored_branch(
        tmp_path, _claude("alpha"), provider="claude_code", model="sonnet"
    )
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id, agent_name="role")

    assert seen["env"][-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "subscription_account_switches" not in seen["metadata"][-1]


@pytest.mark.asyncio
async def test_a_claude_alias_branch_moves_to_another_account(monkeypatch, tmp_path, logins):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / "claude-two").write_text(TOKEN_TWO)
    branch_id, path = _stored_branch(tmp_path, _claude("alpha"), provider="claude", model="sonnet")
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id, account="two")

    assert seen["env"][-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_TWO
    assert seen["metadata"][-1]["subscription_account_switches"] == [_claude("two")]


@pytest.mark.asyncio
async def test_a_codex_account_pointed_at_another_home_does_not_move_its_branch(
    monkeypatch, tmp_path, logins, codex_homes
):
    """A codex session is kept under the home it ran in. Repointing the account it ran on
    refuses the resume; an account on the recorded home resumes it there."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    (logins / accounts.ACCOUNTS_FILE).write_text(
        f'[codex.two]\nhome = "{codex_homes["two"]}"\n[codex.one]\nhome = "{codex_homes["one"]}"\n'
    )
    record = {"account": "two", "account_provider": "codex", "codex_home": str(codex_homes["one"])}
    branch_id, path = _stored_branch(tmp_path, record)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    with pytest.raises(ConfigurationError, match="now names the Codex home"):
        await _run_agent(None, "continue", resume=branch_id)
    with pytest.raises(ConfigurationError, match="now names the Codex home"):
        await _run_agent(None, "continue", resume=branch_id, account="two")
    assert seen["env"] == []

    await _run_agent(None, "continue", resume=branch_id, account="one")
    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["one"])
    moved = seen["metadata"][-1]
    one = {"account": "one", "account_provider": "codex", "codex_home": str(codex_homes["one"])}
    assert moved["subscription_account"] == record
    assert moved["subscription_account_switches"] == [one]
    assert json.loads(seen["manifest"][-1])["account_history"] == ["two", "one"]

    # The next resume names no account and stays on the one it moved to.
    branch_id, path = _stored_branch(tmp_path, record, switches=[one])
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    await _run_agent(None, "continue", resume=branch_id)
    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["one"])


@pytest.mark.asyncio
async def test_a_resume_naming_an_account_on_a_provider_that_takes_none_is_refused(
    monkeypatch, tmp_path, logins
):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    branch_id, path = _stored_branch(tmp_path, None, provider="gemini-cli", model="gemini-4-argon")
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    with pytest.raises(ConfigurationError, match="'gemini-cli' takes no subscription account"):
        await _run_agent(None, "continue", resume=branch_id, account="alpha")
    assert seen["env"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parse_provider", ["claude_code", "claude"])
async def test_a_resumed_parse_model_runs_on_the_branchs_account(
    monkeypatch, tmp_path, logins, parse_provider
):
    """A parse model of the chat model's account provider, under either of its names, spawns the
    same child, on the same account."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionhub.serve.cli.agent import _run_agent

    branch = Branch(
        chat_model=iModel(provider="claude_code", model="sonnet"),
        parse_model=iModel(provider=parse_provider, model="haiku"),
    )
    branch.metadata["subscription_account"] = _claude("alpha")
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(branch.to_dict()))
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)
    parse_env = []

    from lionagi import Branch as _Branch

    original = _Branch.operate

    async def operate(self, instruction=None, **kw):
        assert self.parse_model is not self.chat_model
        parse_env.append(reveal_redacted(self.parse_model.endpoint._runtime_state.get("env")))
        return await original(self, instruction, **kw)

    monkeypatch.setattr(_Branch, "operate", operate)

    await _run_agent(None, "continue", resume=str(branch.id))

    assert seen["env"][-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert parse_env[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


# ---- the model builders and orchestration workers ---------------------------------------


def test_a_codex_spec_reads_its_default_effort_from_the_childs_home(
    tmp_path, monkeypatch, codex_homes
):
    parent = tmp_path / "parent-home"
    parent.mkdir()
    (parent / "config.toml").write_text('model_reasoning_effort = "high"\n')
    (codex_homes["one"] / "config.toml").write_text('model_reasoning_effort = "low"\n')
    monkeypatch.setenv("CODEX_HOME", str(parent))

    model = build_imodel_from_spec(
        "codex/gpt-5.5", env={"PATH": "/usr/bin", "CODEX_HOME": str(codex_homes["one"])}
    )

    assert model.endpoint.config.kwargs["reasoning_effort"] == "low"
    assert reveal_redacted(model.endpoint._runtime_state["env"])["CODEX_HOME"] == str(
        codex_homes["one"]
    )


def test_a_codex_profile_name_resolves_in_the_childs_home(tmp_path, monkeypatch, codex_homes):
    parent = tmp_path / "parent-home"
    parent.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(parent))
    (codex_homes["one"] / "fast.config.toml").write_text('model = "gpt-5.5-mini"\n')

    model = build_imodel_from_spec(
        "codex/fast", env={"PATH": "/usr/bin", "CODEX_HOME": str(codex_homes["one"])}
    )

    assert model.endpoint.config.kwargs["model"] == "gpt-5.5-mini"


@pytest.mark.parametrize("named_by", ["codex_home", "account"])
def test_the_worker_check_reads_the_workers_own_codex_home(
    monkeypatch, tmp_path, logins, codex_homes, named_by
):
    """Whether a worker is a CLI one is decided on its model built as the worker's is: in its
    role's Codex home, with no user config. A profile name this process's own home cannot read
    does not stop a run whose worker never reads that home."""
    import lionhub.serve.cli.orchestrate._orchestration as orch

    from .orchestrate.test_messenger_wiring import _make_env

    parent = tmp_path / "parent-home"
    parent.mkdir()
    (parent / "leg.config.toml").symlink_to(tmp_path / "missing")
    (codex_homes["one"] / "leg.config.toml").write_text('model = "gpt-5.5"\n')
    (logins / accounts.ACCOUNTS_FILE).write_text(f'[codex.one]\nhome = "{codex_homes["one"]}"\n')
    monkeypatch.setenv("CODEX_HOME", str(parent))
    profile = SimpleNamespace(
        account="one" if named_by == "account" else None,
        codex_home=str(codex_homes["one"]) if named_by == "codex_home" else None,
    )
    monkeypatch.setattr(orch, "resolve_worker_spec", lambda role: ("codex/leg", profile))
    env = _make_env(tmp_path)
    env.bare = False

    assert orch.worker_is_cli(env, "reader") is True


@pytest.mark.parametrize(("bare", "expected"), [(False, TOKEN), (True, None)])
def test_a_worker_runs_on_its_roles_account_and_a_bare_worker_on_none(
    tmp_path, logins, bare, expected
):
    profile = SimpleNamespace(
        raw_body=True,
        system_prompt="You are a reader.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
        account="alpha",
        codex_home=None,
    )
    _env, _imodel, built = _build_worker(
        tmp_path, agent_id="reader", role="reader", bare=bare, profile=profile
    )
    env = built["imodel_kwargs"].get("env")
    assert (env or {}).get("CLAUDE_CODE_OAUTH_TOKEN") == expected
    # The saved worker branch names its account, never the token.
    record = built["branch"].metadata.get("subscription_account")
    if bare:
        assert record is None
    else:
        assert record["account"] == "alpha"
        assert TOKEN not in repr(record)


def test_mcp_secret_fields_go_to_the_childs_codex_home(codex_home, codex_homes):
    """The profile carrying the servers' env goes where the child's codex looks for `-p`."""
    from lionhub.orchestration.spawn import build_chat_model

    from .test_mcp_set_reaches_every_cli_provider import SECRET_SERVERS

    codex_home.write_config({})
    model = build_chat_model(
        "codex",
        "gpt-5.5",
        False,
        False,
        None,
        mcp_servers=SECRET_SERVERS,
        mcp_exclusive=True,
        env={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(codex_homes["one"])},
    )

    profile = model.endpoint.config.kwargs["profile"]
    assert (codex_homes["one"] / f"{profile}.config.toml").is_file()
    assert codex_home.profile_files() == []


def test_a_codex_requests_skip_set_reaches_the_spawn(tmp_path, monkeypatch):
    import asyncio

    import lionhub.providers.openai.codex as codex_mod
    from lionhub.providers.openai.codex import CodexCodeRequest

    class _Stopped(Exception):
        pass

    seen: dict = {}

    def spawned(cmd, **kwargs):
        seen.update(kwargs)
        raise _Stopped

    async def drain(request):
        async for _ in codex_mod._ndjson_from_cli(request):
            pass

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    skip = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
    request = CodexCodeRequest(
        repo=tmp_path, prompt="hi", env={"PATH": "/usr/bin"}, secret_fill_skip=skip
    )
    assert "secret_fill_skip" not in request.model_dump()
    with pytest.raises(_Stopped):
        asyncio.run(drain(request))
    assert seen["secret_fill_skip"] == skip


@pytest.mark.asyncio
async def test_a_resume_through_a_link_to_the_recorded_home_runs(
    monkeypatch, tmp_path, logins, codex_homes
):
    """The record holds the home's real path; an account naming it through a link is the same
    home."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.agent import _run_agent

    link = tmp_path / "codex-link"
    link.symlink_to(codex_homes["one"])
    (logins / accounts.ACCOUNTS_FILE).write_text(f'[codex.linked]\nhome = "{link}"\n')
    branch_id, path = _stored_branch(
        tmp_path, {"codex_home": str(codex_homes["one"]), "account_provider": "codex"}
    )
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    seen = _capture(monkeypatch, tmp_path)

    await _run_agent(None, "continue", resume=branch_id, account="linked")

    assert seen["env"][-1]["CODEX_HOME"] == str(codex_homes["one"])
    assert seen["metadata"][-1]["subscription_account_switches"][-1]["account"] == "linked"


# ---- the orchestrator -------------------------------------------------------------------


class _Built(Exception):
    pass


@pytest.mark.asyncio
async def test_the_orchestrator_runs_on_its_roles_account(monkeypatch, tmp_path, codex_homes):
    """The orchestrator's model is built with its role's account; a clone copies the model and
    its runtime environment with it."""
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod
    from lionhub.serve.cli.orchestrate._orchestration import setup_orchestration

    profile = _parse_profile(
        "orc", _role(model="codex/gpt-5.5", codex_home=str(codex_homes["one"]))
    )
    built: dict = {}

    def build(*a, **kw):
        built.update(kw)
        raise _Built

    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name, *a, **kw: profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", build)
    with pytest.raises(_Built):
        await setup_orchestration(
            pattern_name="Fanout",
            model_spec=None,
            agent_name="orc",
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            no_mcp_config=True,
        )
    assert built["env"]["CODEX_HOME"] == str(codex_homes["one"])
    assert "OPENAI_API_KEY" in built["secret_fill_skip"]


@pytest.mark.asyncio
async def test_the_orchestrators_branch_names_its_account(monkeypatch, tmp_path, codex_homes):
    """The saved orchestrator branch records the account it ran on, as an `li agent` branch
    does."""
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod
    from lionhub.serve.cli.orchestrate._orchestration import setup_orchestration

    profile = _parse_profile(
        "orc", _role(model="codex/gpt-5.5", codex_home=str(codex_homes["one"]))
    )
    seen: dict = {}

    def session(*a, default_branch, **kw):
        seen["branch"] = default_branch
        raise _Built

    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name, *a, **kw: profile)
    monkeypatch.setattr(orch_mod, "Session", session)
    with pytest.raises(_Built):
        await setup_orchestration(
            pattern_name="Fanout",
            model_spec=None,
            agent_name="orc",
            save_dir=tmp_path / "runs",
            cwd=str(tmp_path),
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            no_mcp_config=True,
        )
    record = seen["branch"].metadata["subscription_account"]
    assert record["codex_home"] == str(codex_homes["one"])


@pytest.mark.asyncio
async def test_the_flow_run_record_names_the_orchestrators_account(monkeypatch):
    """A flow's run.json carries its orchestrator's account the way an `li agent` run.json does."""
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod

    written: list[dict] = []
    record = {"account": "alpha", "account_provider": "claude_code"}
    env = SimpleNamespace(
        orc_branch=SimpleNamespace(id="orc", metadata={"subscription_account": record}),
        run=SimpleNamespace(write_manifest=lambda manifest: written.append(dict(manifest))),
        session=SimpleNamespace(branches=[]),
    )

    async def stop(*a, **kw):
        raise _Built

    monkeypatch.setattr(orch_mod, "setup_orchestration_persist", stop)
    with pytest.raises(_Built):
        await orch_mod.start_live_persist(env, agent_name="orc")
    assert written[0]["account"] == "alpha"
    assert written[0]["account_provider"] == "claude_code"


def test_the_orchestrator_clone_check_reads_the_orchestrators_home(tmp_path, codex_homes):
    """A codex profile named by the orchestrator's model is looked up in the home it runs in."""
    from lionhub.serve.cli.orchestrate._orchestration import _check_orchestrator_clone_profile

    (codex_homes["one"] / "fast.config.toml").write_text(
        'model = "gpt-5.5"\nsandbox_mode = "danger-full-access"\n'
    )
    endpoint = SimpleNamespace(
        config=SimpleNamespace(provider="codex"),
        _runtime_state={"env": {"CODEX_HOME": str(codex_homes["one"])}},
    )
    env = SimpleNamespace(
        orc_branch=SimpleNamespace(chat_model=SimpleNamespace(is_cli=True, endpoint=endpoint)),
        default_model_spec="codex/fast",
    )
    with pytest.raises(ConfigurationError, match="cloned from the codex orchestrator"):
        _check_orchestrator_clone_profile(env)
