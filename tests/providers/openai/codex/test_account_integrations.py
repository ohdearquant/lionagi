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
import os
import re
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from lionagi._errors import CLIInputRefusedError, ConfigurationError, ContainmentRefusedError
from lionagi.cli._providers import (
    AgentProfile,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.imodel import iModel
from lionhub.providers.openai import codex as codex_mod
from lionhub.providers.openai._codex_profile import CODEX_MCP_PROFILE_PREFIX
from lionhub.providers.openai.codex import CodexCodeRequest, _override_pairs


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
    """A profile's table is refused instead (below)."""
    args = CodexCodeRequest(
        prompt="hi",
        contain_account_integrations=True,
        config_overrides={"tui.features.apps": True},
    ).as_cmd_args()
    assert "tui.features.apps=true" in _overrides(args)


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


# ── a profile under containment ─────────────────────────────────────────
# `--disable` sets the features in the root config; codex before 0.135 applies
# the active profile's `[features]` after it, so a profile could turn either back
# on. The active profile is `-p`, else a root `profile` key in the user config or
# in a project `.codex/config.toml`.


@pytest.mark.parametrize(
    "profile",
    ["work", "", CODEX_MCP_PROFILE_PREFIX + "0" * 31, CODEX_MCP_PROFILE_PREFIX + "A" * 32],
    ids=["named", "empty", "short-generated-shape", "uppercase-generated-shape"],
)
def test_containment_refuses_a_profile_lionagi_did_not_write(profile):
    req = CodexCodeRequest(prompt="hi", contain_account_integrations=True, profile=profile)
    with pytest.raises(ValueError, match=re.escape(f"profile={profile!r}")):
        req.as_cmd_args()


def test_containment_keeps_the_profile_lionagi_writes_for_mcp_server_secrets(tmp_path):
    """That profile holds only mcp_servers."""
    name = CODEX_MCP_PROFILE_PREFIX + "0123456789abcdef" * 2
    args = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True, profile=name
    ).as_cmd_args()
    assert args[args.index("-p") + 1] == name
    assert _features_switched(args) == [("--disable", "apps"), ("--disable", "plugins")]


@pytest.mark.parametrize(
    "key",
    [
        "profile",
        '"profile"',
        "profiles",
        "profiles.work.features.apps",
        'profiles."work".features',
        '"\\u0070rofiles".work.features.plugins',
    ],
)
def test_containment_refuses_an_override_that_selects_or_changes_a_profile(key):
    req = CodexCodeRequest(
        prompt="hi", contain_account_integrations=True, config_overrides={key: True}
    )
    # Named by position: an override key is never printed, whatever it holds.
    with pytest.raises(ValueError, match=re.escape("override entry 0: it selects")) as exc:
        req.as_cmd_args()
    assert repr(key) not in str(exc.value)


def test_containment_keeps_an_override_whose_first_part_only_starts_like_profile(tmp_path):
    args = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides={"profile_note": 1, "tools.profiles": 2},
    ).as_cmd_args()
    assert {"profile_note=1", "tools.profiles=2"} <= set(_overrides(args))


@pytest.mark.parametrize(
    "fields",
    [{}, {"contain_account_integrations": True, "account_connectors": ["github"]}],
    ids=["uncontained", "granted"],
)
def test_a_profile_is_left_as_named_without_containment(fields, tmp_path):
    args = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        profile="work",
        config_overrides={"profiles.work.model_verbosity": "low"},
        **fields,
    ).as_cmd_args()
    assert args[args.index("-p") + 1] == "work"
    assert 'profiles.work.model_verbosity="low"' in _overrides(args)


def test_containment_refuses_a_profile_a_model_profile_file_selects(tmp_path):
    """A model naming a codex config profile file brings its scalar keys as overrides."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "leg.config.toml").write_text('model = "gpt-5.5"\nprofile = "work"\n')
    req = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        model="leg",
        env={"CODEX_HOME": str(home)},
        contain_account_integrations=True,
    )
    assert req.model == "gpt-5.5"
    assert req.config_overrides == {"profile": "work"}
    with pytest.raises(ValueError, match=re.escape("override entry 0: it selects")):
        req.as_cmd_args()


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex"])
def test_the_model_spec_reader_refuses_a_request_codex_home_that_is_not_absolute(tmp_path, value):
    """The model-spec lookup resolves the home from the request's env, so the same refusal
    applies before it reads a profile file there."""
    with pytest.raises(ValidationError, match="CODEX_HOME") as refusal:
        CodexCodeRequest(prompt="hi", repo=tmp_path, model="leg", env={"CODEX_HOME": value})
    assert "is not an absolute path" in str(refusal.value)


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex"])
def test_the_containment_reader_refuses_a_request_codex_home_that_is_not_absolute(tmp_path, value):
    """The containment read resolves the home from the request's env, so the same refusal
    applies before it reads the config there."""
    with pytest.raises(ValidationError, match="CODEX_HOME") as refusal:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            env={"CODEX_HOME": value},
            contain_account_integrations=True,
        )
    assert "is not an absolute path" in str(refusal.value)


def _codex_home(tmp_path, config: str) -> dict[str, str]:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config)
    return {"CODEX_HOME": str(home)}


def test_containment_refuses_a_profile_the_codex_config_selects(tmp_path):
    env = _codex_home(tmp_path, 'profile = "work"\n[profiles.work.features]\napps = true\n')
    req = CodexCodeRequest(prompt="hi", repo=tmp_path, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match=re.escape("profile = 'work'")):
        req.as_cmd_args()


@pytest.mark.parametrize(
    ("config", "fields"),
    [
        (None, {"profile": "work"}),
        (None, {"config_overrides": {"profile": "work"}}),
        ('profile = "work"\n', {}),
        ("profile = \n", {}),
    ],
    ids=[
        "profile",
        "override-selects-a-profile",
        "config-selects-a-profile",
        "config-unreadable",
    ],
)
def test_every_containment_refusal_carries_its_own_type(config, fields, tmp_path):
    """A caller tells a containment refusal from any other ValueError by its type."""
    env = _codex_home(tmp_path, config) if config is not None else None
    req = CodexCodeRequest(
        prompt="hi", repo=tmp_path, env=env, contain_account_integrations=True, **fields
    )
    with pytest.raises(ContainmentRefusedError):
        req.as_cmd_args()


def test_an_override_key_containment_cannot_read_is_refused_when_the_request_is_built(tmp_path):
    """Containment reads every override key, so one TOML cannot read is refused as the
    request is built, by position, as an input the CLI cannot take: a ValueError raised
    there would arrive wrapped with the whole input."""
    with pytest.raises(CLIInputRefusedError, match="config override entry 0 is not a TOML key"):
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            contain_account_integrations=True,
            config_overrides={"a b": 1},
        )


def test_containment_reads_the_codex_config_of_the_home_the_child_runs_under(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('profile = ""\n')
    req = CodexCodeRequest(
        prompt="hi", repo=tmp_path, env={"HOME": str(home)}, contain_account_integrations=True
    )
    with pytest.raises(ValueError, match=re.escape("profile = ''")):
        req.as_cmd_args()


def test_containment_keeps_a_codex_config_without_a_root_profile(tmp_path):
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n[profiles.work.features]\napps = true\n')
    args = CodexCodeRequest(
        prompt="hi", repo=tmp_path, env=env, contain_account_integrations=True
    ).as_cmd_args()
    assert "-p" not in args
    assert _features_switched(args) == [("--disable", "apps"), ("--disable", "plugins")]


def test_containment_with_the_profile_lionagi_writes_leaves_the_config_profile_to_codex(tmp_path):
    """`-p` names the active profile, so the config's root key is not the one applied."""
    env = _codex_home(tmp_path, 'profile = "work"\n')
    name = CODEX_MCP_PROFILE_PREFIX + "0123456789abcdef" * 2
    args = CodexCodeRequest(
        prompt="hi", repo=tmp_path, env=env, contain_account_integrations=True, profile=name
    ).as_cmd_args()
    assert args[args.index("-p") + 1] == name


def test_containment_refuses_a_codex_config_it_cannot_read(tmp_path):
    env = _codex_home(tmp_path, "profile = \n")
    req = CodexCodeRequest(prompt="hi", repo=tmp_path, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match="cannot read the codex config"):
        req.as_cmd_args()


@pytest.mark.parametrize(
    "fields",
    [{}, {"contain_account_integrations": True, "account_connectors": ["github"]}],
    ids=["uncontained", "granted"],
)
def test_the_codex_config_profile_is_read_only_under_containment(fields, tmp_path):
    """A config that cannot be parsed is refused only when containment reads it."""
    env = _codex_home(tmp_path, "profile = \n")
    args = CodexCodeRequest(prompt="hi", repo=tmp_path, env=env, **fields).as_cmd_args()
    assert "-p" not in args


def _repo(tmp_path, marker: str = "dir"):
    repo = tmp_path / "repo"
    repo.mkdir()
    if marker == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: elsewhere\n")
    return repo


def _project_config(directory, config: str) -> None:
    (directory / ".codex").mkdir(parents=True, exist_ok=True)
    (directory / ".codex" / "config.toml").write_text(config)


@pytest.mark.parametrize("at", ["", "a", "a/b"], ids=["git-root", "between", "cwd"])
def test_containment_refuses_a_profile_a_project_codex_config_selects(tmp_path, at):
    """A trusted project's config is merged too, so its root profile key selects the active
    profile; every file from the working directory up to the git root is read."""
    repo = _repo(tmp_path)
    cwd = repo / "a" / "b"
    cwd.mkdir(parents=True)
    _project_config(repo / at, 'profile = "w"\n[profiles.w.features]\napps = true\n')
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(prompt="hi", repo=cwd, env=env, contain_account_integrations=True)
    with pytest.raises(
        ValueError, match=r"profile = 'w' in the project codex config '.*config\.toml'"
    ):
        req.as_cmd_args()


@pytest.mark.parametrize("marker", ["dir", "file"])
def test_containment_reads_no_project_codex_config_above_the_git_root(tmp_path, marker):
    repo = _repo(tmp_path, marker)
    _project_config(tmp_path, 'profile = "w"\n')
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    args = CodexCodeRequest(
        prompt="hi", repo=repo, env=env, contain_account_integrations=True
    ).as_cmd_args()
    assert _features_switched(args) == [("--disable", "apps"), ("--disable", "plugins")]


def test_containment_without_a_git_root_reads_every_ancestors_project_codex_config(tmp_path):
    cwd = tmp_path / "plain" / "x"
    cwd.mkdir(parents=True)
    _project_config(tmp_path, 'profile = "w"\n')
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(prompt="hi", repo=cwd, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match="the project codex config"):
        req.as_cmd_args()


def test_containment_refuses_a_project_codex_config_it_cannot_read(tmp_path):
    repo = _repo(tmp_path)
    _project_config(repo, "profile = \n")
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(prompt="hi", repo=repo, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match="cannot read the codex config"):
        req.as_cmd_args()


@pytest.mark.parametrize(
    "fields",
    [{}, {"contain_account_integrations": True, "account_connectors": ["github"]}],
    ids=["uncontained", "granted"],
)
def test_a_project_codex_config_is_read_only_under_containment(fields, tmp_path):
    repo = _repo(tmp_path)
    _project_config(repo, "profile = \n")
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    CodexCodeRequest(prompt="hi", repo=repo, env=env, **fields).as_cmd_args()


_GENERATED = CODEX_MCP_PROFILE_PREFIX + "0123456789abcdef" * 2


@pytest.mark.parametrize("where", ["user", "project"])
def test_containment_refuses_a_config_defining_a_profile_under_the_generated_name(tmp_path, where):
    """Codex before 0.135 reads `-p` as a `[profiles.<name>]` table in the configs it merges."""
    table = f"[profiles.{_GENERATED}.features]\napps = true\n"
    repo = _repo(tmp_path)
    if where == "user":
        env = _codex_home(tmp_path, table)
    else:
        env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
        _project_config(repo, table)
    req = CodexCodeRequest(
        prompt="hi", repo=repo, env=env, contain_account_integrations=True, profile=_GENERATED
    )
    with pytest.raises(ValueError, match=re.escape(f"profile={_GENERATED!r} while")):
        req.as_cmd_args()


def test_containment_keeps_the_generated_profile_beside_other_profiles(tmp_path):
    repo = _repo(tmp_path)
    env = _codex_home(tmp_path, 'profile = "work"\n[profiles.work.features]\napps = true\n')
    _project_config(repo, 'profile = "w"\n[profiles.w.features]\nplugins = true\n')
    args = CodexCodeRequest(
        prompt="hi", repo=repo, env=env, contain_account_integrations=True, profile=_GENERATED
    ).as_cmd_args()
    assert args[args.index("-p") + 1] == _GENERATED
    assert _features_switched(args) == [("--disable", "apps"), ("--disable", "plugins")]


def test_containment_with_the_generated_profile_refuses_a_config_it_cannot_read(tmp_path):
    repo = _repo(tmp_path)
    _project_config(repo, "profile = \n")
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(
        prompt="hi", repo=repo, env=env, contain_account_integrations=True, profile=_GENERATED
    )
    with pytest.raises(ValueError, match="cannot read the codex config"):
        req.as_cmd_args()


def test_containment_walks_the_resolved_path_of_a_symlinked_working_directory(tmp_path):
    real = _repo(tmp_path)
    _project_config(real, 'profile = "w"\n')
    (real / "sub").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real / "sub", target_is_directory=True)
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(prompt="hi", repo=link, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match=re.escape("profile = 'w' in the project codex config")):
        req.as_cmd_args()


def test_containment_collapses_a_dot_dot_in_the_working_directory(tmp_path):
    """`a/b/../c` is `a/c`: the walk reads `a` and above, not the git root at `a/b`."""
    _project_config(tmp_path, 'profile = "w"\n')
    (tmp_path / "a" / "b" / ".git").mkdir(parents=True)
    (tmp_path / "a" / "c").mkdir()
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path / "a" / "b" / ".." / "c",
        env=env,
        contain_account_integrations=True,
    )
    with pytest.raises(ValueError, match=re.escape("profile = 'w' in the project codex config")):
        req.as_cmd_args()


def test_containment_walks_the_spelled_path_beside_the_resolved_one(tmp_path):
    """`x/link/../c`, with `link` pointing into a git root at `far`, is `x/c` as spelled and
    `far/c` as resolved; the two trees differ, and the spelled one holds the config."""
    _project_config(tmp_path / "x", 'profile = "w"\n')
    (tmp_path / "far" / ".git").mkdir(parents=True)
    (tmp_path / "far" / "deep").mkdir()
    (tmp_path / "far" / "c").mkdir()
    (tmp_path / "x" / "link").symlink_to(tmp_path / "far" / "deep", target_is_directory=True)
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path / "x" / "link" / ".." / "c",
        env=env,
        contain_account_integrations=True,
    )
    with pytest.raises(ValueError, match=re.escape("profile = 'w' in the project codex config")):
        req.as_cmd_args()


def test_containment_walks_the_resolved_path_beside_the_spelled_one(tmp_path):
    """The mirror of the case above: the config sits only on the resolved chain.

    `x/link/../c`, with `link` pointing into a git root at `far`, is `x/c` as spelled and
    `far/c` as resolved. The spelled tree holds no config here, so a walk that covers
    only the spelled path misses one codex will read for the child's own directory.
    """
    (tmp_path / "x").mkdir()
    (tmp_path / "far" / ".git").mkdir(parents=True)
    (tmp_path / "far" / "deep").mkdir()
    (tmp_path / "far" / "c").mkdir()
    _project_config(tmp_path / "far", 'profile = "w"\n')
    (tmp_path / "x" / "link").symlink_to(tmp_path / "far" / "deep", target_is_directory=True)
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path / "x" / "link" / ".." / "c",
        env=env,
        contain_account_integrations=True,
    )
    with pytest.raises(ValueError, match=re.escape("profile = 'w' in the project codex config")):
        req.as_cmd_args()


def test_containment_refuses_a_project_codex_config_nested_too_deep_to_parse(tmp_path):
    repo = _repo(tmp_path)
    _project_config(repo, "x = " + "[" * 3000 + "]" * 3000 + "\n")
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    req = CodexCodeRequest(prompt="hi", repo=repo, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match="cannot read the codex config"):
        req.as_cmd_args()


@pytest.mark.parametrize("where", ["user", "project"])
def test_a_root_profile_key_that_is_not_a_string_is_named_by_its_type(tmp_path, where):
    config = '[profile]\nname = "not-echoed"\n'
    repo = _repo(tmp_path)
    if where == "user":
        env = _codex_home(tmp_path, config)
    else:
        env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
        _project_config(repo, config)
    req = CodexCodeRequest(prompt="hi", repo=repo, env=env, contain_account_integrations=True)
    with pytest.raises(ValueError, match="a root profile key of type dict") as raised:
        req.as_cmd_args()
    assert "not-echoed" not in str(raised.value)


def test_containment_refuses_a_working_directory_that_is_a_symlink_to_itself(tmp_path):
    """The config walk cannot resolve such a directory, so it cannot read the configs
    codex would apply. The refusal names the directory and the reason it cannot be used."""
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    # Python before 3.13 cannot resolve the loop; 3.13 and later resolve it and find no
    # directory there. Either way the refusal names the directory.
    with pytest.raises(ContainmentRefusedError, match=re.escape(str(loop))):
        CodexCodeRequest(prompt="hi", repo=loop, contain_account_integrations=True)


def test_containment_refuses_a_working_directory_that_does_not_exist(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ContainmentRefusedError, match=re.escape(str(missing))) as raised:
        CodexCodeRequest(prompt="hi", repo=missing, contain_account_integrations=True)
    assert "does not exist or is not a directory" in str(raised.value)


def test_without_containment_an_unusable_working_directory_is_left_as_it_was(tmp_path):
    """Control: the refusal is containment's, not a new restriction on every request."""
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    missing = tmp_path / "missing"

    try:
        looped = CodexCodeRequest(prompt="hi", repo=loop)
    except RuntimeError as exc:
        # Python before 3.13 raises while resolving the loop, as it did before containment.
        assert "Symlink loop" in str(exc)
    else:
        with pytest.raises(ValueError, match="does not exist or is not a directory"):
            looped.as_cmd_args()
    req = CodexCodeRequest(prompt="hi", repo=missing)
    with pytest.raises(ValueError, match="does not exist or is not a directory"):
        req.as_cmd_args()


def _unusable_directory(tmp_path, kind):
    """A directory this process cannot list and enter, and the paths to restore after."""
    if kind == "no-permissions":
        target = tmp_path / "locked"
        target.mkdir()
        return target, [(target, 0o000)]
    if kind == "read-without-search":
        target = tmp_path / "unsearchable"
        target.mkdir()
        return target, [(target, 0o600)]
    parent = tmp_path / "locked-parent"
    target = parent / "child"
    target.mkdir(parents=True)
    return target, [(parent, 0o000)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
@pytest.mark.parametrize("kind", ["no-permissions", "read-without-search", "child-of-locked"])
def test_containment_refuses_a_working_directory_it_cannot_enter(tmp_path, kind):
    """Without permission to list and enter the directory the walk cannot read the
    configs codex would apply, so containment refuses it, naming the directory."""
    target, modes = _unusable_directory(tmp_path, kind)
    for path, mode in modes:
        path.chmod(mode)
    try:
        with pytest.raises(ContainmentRefusedError, match=re.escape(str(target))):
            CodexCodeRequest(prompt="hi", repo=target, contain_account_integrations=True)
    finally:
        for path, _ in modes:
            path.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_without_containment_a_directory_it_cannot_enter_is_not_refused_at_construction(
    tmp_path,
):
    """Control: the check is containment's; an ordinary request is built as before."""
    target, modes = _unusable_directory(tmp_path, "no-permissions")
    for path, mode in modes:
        path.chmod(mode)
    try:
        CodexCodeRequest(prompt="hi", repo=target)
    finally:
        for path, _ in modes:
            path.chmod(0o755)


def test_a_profile_file_that_is_not_utf8_is_refused_naming_it(tmp_path):
    """The profile read decodes the file; bytes that are not UTF-8 make it unreadable."""
    from lionhub.providers.openai._codex_profile import resolve_codex_config_profile

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "deep.config.toml").write_bytes(b'model = "\xff\xfe"\n')
    with pytest.raises(ValueError, match=re.escape("deep.config.toml") + ".*could not be read"):
        resolve_codex_config_profile("deep", env={"CODEX_HOME": str(home)})


def test_a_codex_home_holding_a_nul_reads_as_having_no_profile():
    """A path the operating system cannot name holds no file, as ``Path.is_file()`` says:
    a bare model name is sent as a model id, and the request is built."""
    from lionhub.providers.openai._codex_profile import _is_file, resolve_codex_config_profile

    env = {"CODEX_HOME": "/nonexistent/a\0b"}
    assert _is_file(Path("/nonexistent/a\0b/o3.config.toml")) is False
    assert resolve_codex_config_profile("o3", env=env) is None
    assert CodexCodeRequest(prompt="hi", model="o3", env=env).model == "o3"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_profile_that_cannot_be_checked_is_refused_naming_it(tmp_path):
    """A codex home this process cannot search leaves the profile unknown, not absent."""
    from lionhub.providers.openai._codex_profile import resolve_codex_config_profile

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "deep.config.toml").write_text('model = "gpt-5.5"\n')
    home.chmod(0o600)
    try:
        with pytest.raises(ValueError, match=re.escape("deep.config.toml")):
            resolve_codex_config_profile("deep", env={"CODEX_HOME": str(home)})
    finally:
        home.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_project_config_that_cannot_be_checked_is_refused_on_every_python(tmp_path):
    """From Python 3.14 Path.is_file() answers False for a permission error; the walk must
    still refuse a project config it cannot check rather than read it as absent."""
    from lionhub.providers.openai._codex_profile import project_codex_profile

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    codex_dir = repo / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('profile = "deep"\n')
    codex_dir.chmod(0o600)
    try:
        with pytest.raises(ValueError, match=re.escape(str(codex_dir / "config.toml"))):
            project_codex_profile(repo)
    finally:
        codex_dir.chmod(0o755)


# ── an override mapping whose views disagree ────────────────────────────
# The command line is rendered from the overrides' `items()`, and containment
# decides from the same pairs, so a mapping whose iteration and `items()` disagree
# cannot put a profile on the command line past the check.


class _OverridesToldByItems(Mapping):
    """A mapping whose iteration omits the override its ``items()`` yields."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return len(self._pairs)

    def __getitem__(self, key):
        raise KeyError(key)

    def items(self):
        return list(self._pairs)


class _OverridesAsADictToldByItems(dict):
    """A dict whose storage holds an ordinary key while ``items()`` names a profile."""

    def items(self):
        return [("profile", "work")]


class _OverridesAnsweringEachCall(Mapping):
    """A mapping whose ``items()`` answers a different list of pairs on each call."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.calls = 0

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __getitem__(self, key):
        raise KeyError(key)

    def items(self):
        answer = self._answers[min(self.calls, len(self._answers) - 1)]
        self.calls += 1
        return list(answer)


def _refuse_a_spawn(monkeypatch):
    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)


def _with_those_overrides(tmp_path, overrides, way):
    """The request with ``overrides`` stored the way a caller without validation leaves it."""
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, contain_account_integrations=True)
    if way == "copied":
        return request.model_copy(update={"config_overrides": overrides})
    request.config_overrides = overrides
    return request


@pytest.mark.parametrize(
    "overrides",
    [_OverridesToldByItems([("profile", "work")]), _OverridesAsADictToldByItems({"ok": 1})],
    ids=["iteration-omits-it", "dict-items-name-it"],
)
@pytest.mark.parametrize("way", ["copied", "assigned"], ids=["copied", "assigned"])
def test_an_override_that_names_a_profile_only_through_items_is_refused(tmp_path, overrides, way):
    """Containment reads the pairs the command line is rendered from, so an override that
    only ``items()`` names cannot reach the command line as ``-c profile=...``."""
    request = _with_those_overrides(tmp_path, overrides, way)

    with pytest.raises(ContainmentRefusedError) as refused:
        request.as_cmd_args()

    assert "override entry 0: it selects" in str(refused.value)
    assert "work" not in repr(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [_OverridesToldByItems([("profile", "work")]), _OverridesAsADictToldByItems({"ok": 1})],
    ids=["iteration-omits-it", "dict-items-name-it"],
)
@pytest.mark.parametrize("way", ["copied", "assigned"], ids=["copied", "assigned"])
async def test_an_override_that_names_a_profile_only_through_items_is_refused_before_the_cli(
    tmp_path, monkeypatch, overrides, way
):
    """The same pair is read where the process would start, before any process exists."""
    _refuse_a_spawn(monkeypatch)
    request = _with_those_overrides(tmp_path, overrides, way)

    with pytest.raises(ContainmentRefusedError) as refused:
        async for _ in codex_mod._ndjson_from_cli(request):
            pass

    assert "override entry 0: it selects" in str(refused.value)


@pytest.mark.parametrize(
    "answers,refused",
    [
        ([(), [("profile", "work")]], False),
        ([[("profile", "work")], ()], True),
    ],
    ids=["first-call-empty", "first-call-names-a-profile"],
)
def test_the_check_and_the_renderer_share_one_read_of_the_overrides(tmp_path, answers, refused):
    """``items()`` is read once, so a mapping that answers differently on each call cannot
    hide a profile from the check and hand it to the renderer, or the other way round."""
    overrides = _OverridesAnsweringEachCall(answers)
    request = _with_those_overrides(tmp_path, overrides, "copied")

    if refused:
        with pytest.raises(ContainmentRefusedError, match="override entry 0: it selects"):
            request.as_cmd_args()
    else:
        assert not any(o.startswith("profile=") for o in _overrides(request.as_cmd_args()))
    assert overrides.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers,refused",
    [
        ([(), [("profile", "work")]], False),
        ([[("profile", "work")], ()], True),
    ],
    ids=["first-call-empty", "first-call-names-a-profile"],
)
async def test_the_spawn_reads_the_overrides_once_for_its_check_and_its_render(
    tmp_path, monkeypatch, answers, refused
):
    """Where the process would start, one read serves containment and the render, so a mapping
    whose ``items()`` answers differently on each call cannot slip a profile past either."""
    seen = {}

    def spawned(*_args, **_kwargs):
        seen["argv"] = list(_args)
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    overrides = _OverridesAnsweringEachCall(answers)
    request = _with_those_overrides(tmp_path, overrides, "copied")

    if refused:
        with pytest.raises(ContainmentRefusedError, match="override entry 0: it selects"):
            async for _ in codex_mod._ndjson_from_cli(request):
                pass
    else:
        with pytest.raises(AssertionError, match="the Codex CLI was started"):
            async for _ in codex_mod._ndjson_from_cli(request):
                pass
        assert not any(o.startswith("profile=") for o in _overrides(seen["argv"]))
    assert overrides.calls == 1


def test_the_renderer_reads_the_pairs_it_is_handed_and_not_the_mapping_again(tmp_path):
    """The command line is rendered from the pairs handed to it, so a mapping that would name
    a profile on a later read cannot put one there under containment."""
    overrides = _OverridesAnsweringEachCall([(), [("profile", "work")]])
    request = _with_those_overrides(tmp_path, overrides, "copied")
    pairs = _override_pairs(request.config_overrides)

    assert overrides.calls == 1
    args = request.as_cmd_args(overrides=pairs)
    assert overrides.calls == 1
    assert not any(o.startswith("profile=") for o in _overrides(args))


def test_a_validated_override_mapping_is_checked_by_its_copied_keys(tmp_path):
    """A validated request stores the plain copy of the mapping, so a key only ``items()``
    yields is not copied and never reaches the command line."""
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides=_OverridesToldByItems([("profile", "work")]),
    )

    assert dict(request.config_overrides) == {}
    assert not any(o.startswith("profile=") for o in _overrides(request.as_cmd_args()))


def test_a_validated_override_dict_is_checked_by_its_storage_not_its_items(tmp_path):
    """A dict subclass is copied by its storage, so the profile ``items()`` names is not
    stored and the caller's own key reaches the command line."""
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides=_OverridesAsADictToldByItems({"ok": 1}),
    )

    assert dict(request.config_overrides) == {"ok": 1}
    args = request.as_cmd_args()
    assert "ok=1" in _overrides(args)
    assert not any(o.startswith("profile=") for o in _overrides(args))
