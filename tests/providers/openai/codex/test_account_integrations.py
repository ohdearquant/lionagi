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
import re

import pytest

from lionagi._errors import ContainmentRefusedError
from lionagi.cli._providers import (
    AgentProfile,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from lionagi.providers.openai._codex_profile import CODEX_MCP_PROFILE_PREFIX
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


def test_containment_refuses_an_override_key_toml_cannot_read():
    req = CodexCodeRequest(
        prompt="hi", contain_account_integrations=True, config_overrides={"a b": 1}
    )
    with pytest.raises(ValueError, match="is not a TOML key"):
        req.as_cmd_args()


def test_containment_refuses_a_multiline_override_key_by_name():
    req = CodexCodeRequest(
        prompt="hi",
        contain_account_integrations=True,
        config_overrides={"x = 1\nfeatures.apps": True},
    )
    with pytest.raises(ValueError, match="is not a TOML key"):
        req.as_cmd_args()


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
    with pytest.raises(ValueError, match=re.escape(f"override {key!r}")):
        req.as_cmd_args()


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
    with pytest.raises(ValueError, match=re.escape("override 'profile'")):
        req.as_cmd_args()


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
        (None, {"config_overrides": {"a b": 1}}),
        ('profile = "work"\n', {}),
        ("profile = \n", {}),
    ],
    ids=[
        "profile",
        "override-selects-a-profile",
        "override-key-unreadable",
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
    with pytest.raises(ValueError, match=r"the project codex config .*'s profile = 'w'"):
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
    [
        {},
        {"contain_account_integrations": True, "account_connectors": ["github"]},
        {
            "contain_account_integrations": True,
            "profile": CODEX_MCP_PROFILE_PREFIX + "0123456789abcdef" * 2,
        },
    ],
    ids=["uncontained", "granted", "generated-profile"],
)
def test_a_project_codex_config_is_read_only_under_containment_with_no_profile(fields, tmp_path):
    repo = _repo(tmp_path)
    _project_config(repo, "profile = \n")
    env = _codex_home(tmp_path, 'model = "gpt-5.5"\n')
    CodexCodeRequest(prompt="hi", repo=repo, env=env, **fields).as_cmd_args()


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
