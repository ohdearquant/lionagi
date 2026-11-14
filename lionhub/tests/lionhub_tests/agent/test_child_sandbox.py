# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The child sandbox policy: defaults, additions, exemptions, refusals and Claude settings.

The policy is resolved from the global settings layer only. Every test points HOME at a
temporary directory, so nothing here reads or writes the real home.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import lionhub.agent.child_sandbox as child_sandbox
from lionagi._errors import ConfigurationError
from lionhub.agent.child_sandbox import (
    CHILD_SANDBOX_OPT_OUT,
    CLAUDE_CLI_FLOOR,
    DEFAULT_ALLOW_WRITE,
    DEFAULT_DENY_EDIT,
    DEFAULT_DENY_READ,
    PROJECT_CONFIG_NAMES,
    ChildSandboxPolicy,
    check_claude_cli_floor,
    check_sandbox_backend,
    claude_settings_argument,
    claude_settings_for,
    effective_policy,
    policy_from_dict,
    resolve_child_sandbox,
    run_state_paths,
    warn_project_child_sandbox,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the policy names POSIX paths")

# The real settings source, captured before the suite-wide fixture replaces it for each test.
_REAL_GLOBAL_SETTINGS = child_sandbox._global_settings

POLICY = ChildSandboxPolicy(True, ("~/.ssh", "/opt/keys"), ("~/.cache/uv",))

#: What a resolved policy denies reading when no layer adds anything: the built-in list, then
#: lionagi's own credential file under ``LIONAGI_HOME`` (pointed at the test home below).
RESOLVED_DENY_READ = (*DEFAULT_DENY_READ, "~/.lionagi/.env")

#: What the file tools are denied when no layer adds anything: the configuration list, then the
#: read list.
DEFAULT_EDIT = tuple(dict.fromkeys([*DEFAULT_DENY_EDIT, *RESOLVED_DENY_READ]))

DEFAULT_POLICY = ChildSandboxPolicy(True, RESOLVED_DENY_READ, DEFAULT_ALLOW_WRITE, DEFAULT_EDIT)

#: Profiles the tests exempt; each has a file in the test home's global agents directory.
GLOBAL_PROFILES = ("builder", "keeper")


def _fixed_edit_rules():
    """The Edit rules every enabled policy ends with: the run files, then project configuration."""
    return [
        *[rule for path in run_state_paths() for rule in (f"Edit(/{path})", f"Edit(/{path}/**)")],
        *[
            rule
            for name in PROJECT_CONFIG_NAMES
            for rule in (f"Edit(//**/{name})", f"Edit(//**/{name}/**)")
        ],
    ]


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """A throwaway HOME, and no inherited uv cache setting."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    for name in GLOBAL_PROFILES:
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return home


def _settings(**block):
    return {"child_sandbox": block}


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- defaults


def test_the_default_policy_is_on_with_the_generic_lists():
    policy = resolve_child_sandbox(None, {})
    assert policy == DEFAULT_POLICY


def test_the_default_lists_are_valid_policy_input():
    policy = resolve_child_sandbox(None, {})
    assert policy_from_dict(policy.to_dict()) == policy


_EXPECTED_DENY_READ = (
    "~/.ssh",
    "~/.gnupg",
    "~/.netrc",
    "~/.git-credentials",
    "~/.config/gh",
    "~/.config/hub",
    "~/.aws",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube/config",
    "~/.docker/config.json",
    "~/.npmrc",
    "~/.pypirc",
    "~/.config/pip/pip.conf",
    "~/.cargo/credentials.toml",
    "~/.cargo/credentials",
    "~/.gem/credentials",
    "~/.m2/settings.xml",
    "~/.gradle/gradle.properties",
    "~/.terraform.d/credentials.tfrc.json",
    "~/.fly",
    "~/.config/stripe",
    "~/.config/rclone",
    "~/.pgpass",
    "~/.my.cnf",
    "~/.codex/auth.json",
    "~/.claude/.credentials.json",
    "~/.claude.json",
    "~/.claude/settings.json",
    "~/.claude/settings.local.json",
    "~/.gemini/oauth_creds.json",
    "~/.config/github-copilot",
    "~/.copilot",
    "~/.zsh_history",
    "~/.bash_history",
    "~/.python_history",
)


def test_the_built_in_deny_list_is_the_generic_credential_list():
    assert DEFAULT_DENY_READ == _EXPECTED_DENY_READ
    assert len(set(DEFAULT_DENY_READ)) == len(DEFAULT_DENY_READ)


def test_the_gh_config_is_denied_as_a_whole_directory():
    assert "~/.config/gh" in DEFAULT_DENY_READ
    assert not any(path.startswith("~/.config/gh/") for path in DEFAULT_DENY_READ)


def test_a_uv_cache_directory_from_the_environment_is_writable(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/var/cache/my-uv/")
    policy = resolve_child_sandbox(None, {})
    assert policy.allow_write == ("/var/cache/my-uv", *DEFAULT_ALLOW_WRITE)


def test_a_relative_uv_cache_directory_is_not_added(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "cache/uv")
    assert resolve_child_sandbox(None, {}).allow_write == DEFAULT_ALLOW_WRITE


def test_the_public_names_are_a_tuple_of_existing_attributes():
    assert isinstance(child_sandbox.__all__, tuple)
    assert all(hasattr(child_sandbox, name) for name in child_sandbox.__all__)


# --------------------------------------------------------------------------- additions


def test_extra_paths_are_added_to_the_defaults():
    policy = resolve_child_sandbox(
        None,
        _settings(deny_read=["~/vault/", "/opt/keys"], allow_write=["~/.cache/ruff"]),
    )
    assert policy.deny_read == (*RESOLVED_DENY_READ, "~/vault", "/opt/keys")
    assert policy.allow_write == (*DEFAULT_ALLOW_WRITE, "~/.cache/ruff")


def test_a_path_already_listed_is_not_repeated():
    policy = resolve_child_sandbox(None, _settings(deny_read=["~/.ssh"], allow_write=["~/.npm"]))
    assert policy.deny_read == RESOLVED_DENY_READ
    assert policy.allow_write == DEFAULT_ALLOW_WRITE


def test_repeated_slashes_collapse_into_one_path_and_one_rule():
    policy = resolve_child_sandbox(None, _settings(deny_read=["//opt//keys/", "/opt/keys"]))
    assert policy.deny_read == (*RESOLVED_DENY_READ, "/opt/keys")
    rules = claude_settings_for(policy)["permissions"]["deny"]
    assert "Read(//opt/keys)" in rules
    assert not any(rule.startswith("Read(///") for rule in rules)


@pytest.mark.parametrize("path", ["~/.cache/uv/../../.ssh", "/opt/./keys", "~/.."])
def test_a_path_with_a_dot_or_dot_dot_segment_is_refused(path):
    with pytest.raises(ConfigurationError, match="must not contain"):
        resolve_child_sandbox(None, _settings(allow_write=[path]))


def test_the_sandbox_can_be_turned_off():
    assert resolve_child_sandbox(None, _settings(enabled=False)).enabled is False


# --------------------------------------------------------------------------- exemptions

_EXEMPT = ["~/.npmrc", "~/.cargo/credentials"]
_WITH_EXEMPTION = _settings(profiles={"builder": {"allow_read": _EXEMPT}})


def test_a_profile_exemption_removes_exactly_the_named_entries():
    policy = resolve_child_sandbox("builder", _WITH_EXEMPTION)
    assert policy.deny_read == tuple(p for p in RESOLVED_DENY_READ if p not in _EXEMPT)
    assert set(RESOLVED_DENY_READ) - set(policy.deny_read) == set(_EXEMPT)
    assert policy.allow_write == DEFAULT_ALLOW_WRITE
    assert policy.enabled is True


@pytest.mark.parametrize("profile", ["reviewer", None])
def test_an_exemption_does_not_apply_to_other_profiles_or_bare_launches(profile):
    assert resolve_child_sandbox(profile, _WITH_EXEMPTION).deny_read == RESOLVED_DENY_READ


def test_a_project_profile_of_the_same_name_does_not_inherit_the_exemption(
    tmp_path, monkeypatch, caplog
):
    # The exemption was written for the profile in the global agents directory. A project
    # directory is searched first, so its file of the same name is the profile -a loads, and
    # it gets the full list.
    project = tmp_path / "repo"
    (project / ".lionagi" / "agents").mkdir(parents=True)
    (project / ".lionagi" / "agents" / "builder.md").write_text("---\nname: builder\n---\n")
    monkeypatch.chdir(project)
    with caplog.at_level("WARNING", logger="lionhub.agent.child_sandbox"):
        policy = resolve_child_sandbox("builder", _WITH_EXEMPTION)
    assert policy.deny_read == RESOLVED_DENY_READ
    assert "does not resolve to a file in ~/.lionagi/agents" in caplog.text


def test_an_exemption_for_a_profile_with_no_global_file_is_not_applied():
    settings = _settings(profiles={"nobody": {"allow_read": _EXEMPT}})
    assert resolve_child_sandbox("nobody", settings).deny_read == RESOLVED_DENY_READ


def test_lionagis_own_credential_file_is_denied_where_lionagi_home_points(tmp_path, monkeypatch):
    assert "~/.lionagi/.env" in resolve_child_sandbox(None, {}).deny_read
    elsewhere = tmp_path / "relocated"
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", elsewhere)
    policy = resolve_child_sandbox(None, {})
    assert str(elsewhere / ".env") in policy.deny_read
    rules = claude_settings_for(policy)["permissions"]["deny"]
    assert f"Read(/{elsewhere}/.env)" in rules


# User-scope MCP server definitions and the user settings' ``env`` block can carry tokens.
_CLAUDE_USER_FILES = ("~/.claude.json", "~/.claude/settings.json", "~/.claude/settings.local.json")


@pytest.mark.parametrize("path", _CLAUDE_USER_FILES)
def test_claude_codes_user_files_are_denied_to_the_file_tools(path):
    settings = claude_settings_for(resolve_child_sandbox(None, {}))
    rules = settings["permissions"]["deny"]
    assert f"Read({path})" in rules
    assert path in settings["sandbox"]["filesystem"]["denyRead"]
    assert f"Edit({path})" in rules


@pytest.mark.parametrize("path", _CLAUDE_USER_FILES)
def test_a_read_exemption_for_a_claude_user_file_lifts_only_the_read_denial(path):
    settings = _settings(profiles={"builder": {"allow_read": [path]}})
    policy = resolve_child_sandbox("builder", settings)
    assert path not in policy.deny_read
    rules = claude_settings_for(policy)["permissions"]["deny"]
    assert f"Read({path})" not in rules
    assert f"Edit({path})" in rules


def test_an_exemption_may_name_a_path_added_to_the_list():
    settings = _settings(deny_read=["~/vault"], profiles={"builder": {"allow_read": ["~/vault"]}})
    assert "~/vault" in resolve_child_sandbox(None, settings).deny_read
    assert "~/vault" not in resolve_child_sandbox("builder", settings).deny_read


@pytest.mark.parametrize("profile", ["builder", "reviewer", None])
def test_an_exemption_naming_a_path_not_on_the_list_is_refused(profile):
    settings = _settings(profiles={"builder": {"allow_read": ["~/.nothing"]}})
    with pytest.raises(ConfigurationError, match="not on the deny list"):
        resolve_child_sandbox(profile, settings)


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"child_sandbox": "on"}, "must be a mapping"),
        ({"child_sandbox": ["~/x"]}, "must be a mapping"),
        (_settings(enabled="false"), "enabled must be true or false"),
        (_settings(enabled=None), "enabled must be true or false"),
        (_settings(deny_read="~/x"), "must be a list of non-empty strings"),
        (_settings(deny_read=[""]), "must be a list of non-empty strings"),
        (_settings(deny_read=["~/x", 3]), "must be a list of non-empty strings"),
        (_settings(deny_read=None), "must be a list of non-empty strings"),
        (_settings(allow_write="~/x"), "must be a list of non-empty strings"),
        (_settings(deny_read=["secrets"]), "absolute path or start with"),
        (_settings(allow_write=["./cache"]), "absolute path or start with"),
        (_settings(surprise=1), "unknown keys"),
        (_settings(profiles=["p"]), "must be a mapping"),
        (_settings(profiles={"p": "x"}), "must be a mapping"),
        (
            _settings(profiles={"p": {"allow_read": "~/.ssh"}}),
            "must be a list of non-empty strings",
        ),
        (_settings(profiles={"p": {"allow_read": ["~/.ssh"], "extra": 1}}), "unknown keys"),
        (_settings(profiles={1: {"allow_read": []}}), "non-string name"),
        (_settings(extends="policy.yaml"), "extends must be a list of non-empty strings"),
        (_settings(extends=["policy.yaml"]), "absolute path or start with"),
    ],
)
def test_a_malformed_value_refuses(settings, message):
    with pytest.raises(ConfigurationError, match=message):
        resolve_child_sandbox(None, settings)


# --------------------------------------------------------------------------- extends files


def test_extends_files_are_merged_in_order(tmp_path):
    first = _write(tmp_path / "a.yaml", 'deny_read: ["~/a-secret"]\nallow_write: ["~/a-cache"]\n')
    second = _write(
        tmp_path / "b.yaml",
        'deny_read: ["/b/secret"]\nprofiles:\n  builder:\n    allow_read: ["~/a-secret"]\n',
    )
    settings = _settings(deny_read=["~/own"], extends=[first, second])
    bare = resolve_child_sandbox(None, settings)
    assert bare.deny_read == (*RESOLVED_DENY_READ, "~/own", "~/a-secret", "/b/secret")
    assert bare.allow_write == (*DEFAULT_ALLOW_WRITE, "~/a-cache")
    builder = resolve_child_sandbox("builder", settings)
    assert builder.deny_read == (*RESOLVED_DENY_READ, "~/own", "/b/secret")


def test_an_extends_entry_may_start_with_the_home_marker(isolated_home):
    _write(isolated_home / "policy.yaml", 'deny_read: ["~/vault"]\n')
    policy = resolve_child_sandbox(None, _settings(extends=["~/policy.yaml"]))
    assert policy.deny_read == (*RESOLVED_DENY_READ, "~/vault")


def test_an_empty_extends_file_adds_only_itself_to_the_edit_denial(tmp_path):
    empty = _write(tmp_path / "empty.yaml", "")
    policy = resolve_child_sandbox(None, _settings(extends=[empty]))
    base = resolve_child_sandbox(None, {})
    assert (policy.deny_read, policy.allow_write) == (base.deny_read, base.allow_write)
    assert set(policy.deny_edit) - set(base.deny_edit) == {empty, str(Path(empty).resolve())}


def test_a_missing_extends_file_refuses(tmp_path):
    with pytest.raises(ConfigurationError, match="cannot read extends file"):
        resolve_child_sandbox(None, _settings(extends=[str(tmp_path / "absent.yaml")]))


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions and a non-root user",
)
def test_an_unreadable_extends_file_refuses(tmp_path):
    locked = tmp_path / "locked.yaml"
    locked.write_text("deny_read: [~/x]\n", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(ConfigurationError, match="cannot read extends file") as caught:
            resolve_child_sandbox(None, _settings(extends=[str(locked)]))
    finally:
        locked.chmod(0o600)
    assert "child_sandbox: {enabled: false}" in str(caught.value)


def test_a_malformed_extends_file_refuses(tmp_path):
    broken = _write(tmp_path / "broken.yaml", "deny_read: [unclosed\n")
    with pytest.raises(ConfigurationError, match="cannot read extends file"):
        resolve_child_sandbox(None, _settings(extends=[broken]))


def test_an_extends_file_that_is_not_text_refuses(tmp_path):
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xff\xff")
    with pytest.raises(ConfigurationError, match="cannot read extends file"):
        resolve_child_sandbox(None, _settings(extends=[str(binary)]))


def test_an_extends_file_that_is_not_a_mapping_refuses(tmp_path):
    listing = _write(tmp_path / "list.yaml", "- a\n- b\n")
    with pytest.raises(ConfigurationError, match="must be a mapping"):
        resolve_child_sandbox(None, _settings(extends=[listing]))


def test_an_extends_file_cannot_turn_the_sandbox_off(tmp_path):
    off = _write(tmp_path / "off.yaml", "enabled: false\n")
    with pytest.raises(ConfigurationError, match="unknown keys"):
        resolve_child_sandbox(None, _settings(extends=[off]))


# --------------------------------------------------------------------------- settings layers


def test_without_a_settings_file_the_default_policy_is_on(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", _REAL_GLOBAL_SETTINGS)
    assert resolve_child_sandbox(None) == DEFAULT_POLICY


def test_the_policy_is_read_from_the_global_settings_file(isolated_home, monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", _REAL_GLOBAL_SETTINGS)
    directory = isolated_home / ".lionagi"
    directory.mkdir(exist_ok=True)
    _write(
        directory / "settings.yaml",
        "child_sandbox:\n"
        '  deny_read: ["~/vault"]\n'
        "  profiles:\n"
        "    builder:\n"
        '      allow_read: ["~/.npmrc"]\n',
    )
    assert resolve_child_sandbox(None).deny_read == (*RESOLVED_DENY_READ, "~/vault")
    assert "~/.npmrc" not in resolve_child_sandbox("builder").deny_read


def test_a_project_layer_key_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", _REAL_GLOBAL_SETTINGS)
    project = tmp_path / "project"
    (project / ".lionagi").mkdir(parents=True)
    _write(
        project / ".lionagi" / "settings.yaml",
        'child_sandbox:\n  enabled: false\n  deny_read: ["~/project-only"]\n',
    )
    monkeypatch.chdir(project)
    policy = resolve_child_sandbox(None)
    assert policy.enabled is True
    assert "~/project-only" not in policy.deny_read


def test_the_policy_asks_the_loader_for_the_global_layer_only(monkeypatch):
    import lionagi._settings as settings_module

    asked = []

    def loader(*, include_project=True, **kwargs):
        asked.append(include_project)
        if include_project:
            return {"child_sandbox": {"enabled": False, "deny_read": ["~/project-only"]}}
        return {"child_sandbox": {"deny_read": ["~/global-only"]}}

    monkeypatch.setattr(child_sandbox, "_global_settings", _REAL_GLOBAL_SETTINGS)
    monkeypatch.setattr(settings_module, "load_settings", loader)
    policy = resolve_child_sandbox(None)
    assert asked == [False]
    assert policy.enabled is True
    assert "~/global-only" in policy.deny_read
    assert "~/project-only" not in policy.deny_read


# --------------------------------------------------------------------------- Claude settings


def test_claude_settings_for_a_home_path_and_an_absolute_path():
    assert claude_settings_for(POLICY) == {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": ["~/.ssh", "/opt/keys"],
                "allowWrite": ["~/.cache/uv"],
            },
        },
        "permissions": {
            "deny": [
                "Read(~/.ssh)",
                "Read(~/.ssh/**)",
                "Read(//opt/keys)",
                "Read(//opt/keys/**)",
                *[
                    rule
                    for path in DEFAULT_DENY_EDIT
                    for rule in (f"Edit({path})", f"Edit({path}/**)")
                ],
                "Edit(~/.ssh)",
                "Edit(~/.ssh/**)",
                "Edit(//opt/keys)",
                "Edit(//opt/keys/**)",
                *_fixed_edit_rules(),
            ]
        },
    }


def test_a_disabled_policy_renders_no_settings():
    assert claude_settings_for(ChildSandboxPolicy(False, DEFAULT_DENY_READ, ())) == {}


def test_every_default_path_gets_two_permission_rules():
    rules = claude_settings_for(resolve_child_sandbox(None, {}))["permissions"]["deny"]
    reads = [rule for rule in rules if rule.startswith("Read(")]
    edits = [rule for rule in rules if rule.startswith("Edit(")]
    fixed = _fixed_edit_rules()
    assert len(reads) == 2 * len(RESOLVED_DENY_READ)
    assert len(edits) == 2 * len(DEFAULT_EDIT) + len(fixed)
    assert len(reads) + len(edits) == len(rules)
    assert rules[-len(fixed) :] == fixed
    assert all(rule.startswith(("Read(~/", "Edit(~/")) for rule in rules[: -len(fixed)])


# --------------------------------------------------------------------------- edit denial


def test_the_file_tools_are_denied_the_configuration_that_launches_the_next_child():
    rules = claude_settings_for(resolve_child_sandbox(None, {}))["permissions"]["deny"]
    for path in (
        "~/.lionagi/settings.yaml",
        "~/.lionagi/agents",
        "~/.claude/settings.json",
        "~/.claude/hooks",
        "~/.claude/plugins",
        "~/.claude/projects/*/memory",
    ):
        assert f"Edit({path})" in rules
        assert f"Edit({path}/**)" in rules


def test_the_files_a_resume_reads_back_are_denied_and_the_artifacts_directory_is_not(
    monkeypatch, tmp_path
):
    import lionagi._paths

    home = tmp_path / "lionagi-home"
    monkeypatch.setattr(lionagi._paths, "LIONAGI_HOME", home)
    rules = claude_settings_for(POLICY)["permissions"]["deny"]
    for path in (
        "runs/*/run.json",
        "runs/*/checkpoint.json",
        "runs/*/branches",
        "runs/*/stream",
        "logs/agents",
        "last_branch.json",
    ):
        assert f"Edit(/{home}/{path})" in rules
        assert f"Edit(/{home}/{path}/**)" in rules
    # Flow and fanout workers run in a run's artifacts directory and must keep writing there.
    assert not [rule for rule in rules if "artifacts" in rule or rule.endswith("/runs/*/**)")]


def test_project_configuration_is_denied_at_any_depth():
    rules = claude_settings_for(POLICY)["permissions"]["deny"]
    for name in (
        ".claude",
        ".mcp.json",
        "CLAUDE.md",
        "CLAUDE.local.md",
        ".lionagi/settings.yaml",
        ".lionagi/agents",
        ".lionagi/playbooks",
        ".env",
        ".env.local",
        ".secrets.env",
    ):
        assert f"Edit(//**/{name})" in rules
        assert f"Edit(//**/{name}/**)" in rules
    # The rest of a project .lionagi directory stays writable.
    assert "Edit(//**/.lionagi)" not in rules
    assert "Edit(//**/.lionagi/**)" not in rules


def test_every_read_denial_is_also_an_edit_denial():
    rules = claude_settings_for(POLICY)["permissions"]["deny"]
    for path in ("~/.ssh", "//opt/keys"):
        assert f"Read({path})" in rules
        assert f"Edit({path})" in rules


def test_deny_edit_in_the_block_and_in_an_extends_file_renders_edit_rules(tmp_path):
    extra = _write(tmp_path / "extra.yaml", "deny_edit:\n  - /srv/prod/config\n")
    policy = resolve_child_sandbox(None, _settings(deny_edit=["~/.tool/config"], extends=[extra]))
    rules = claude_settings_for(policy)["permissions"]["deny"]
    assert "Edit(~/.tool/config/**)" in rules
    assert "Edit(//srv/prod/config/**)" in rules
    # An edit denial does not become a read denial.
    assert "Read(//srv/prod/config)" not in rules
    assert "/srv/prod/config" not in policy.deny_read


def test_an_extends_file_is_denied_by_its_written_path_and_by_the_file_it_resolves_to(tmp_path):
    target = _write(tmp_path / "real.yaml", "deny_read: [/opt/shared]\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    rules = claude_settings_for(resolve_child_sandbox(None, _settings(extends=[str(link)])))[
        "permissions"
    ]["deny"]
    assert f"Edit(/{link})" in rules
    assert f"Edit(/{Path(target).resolve()})" in rules


def test_a_read_exemption_keeps_the_edit_denial():
    policy = resolve_child_sandbox(
        "keeper",
        _settings(deny_read=["/opt/vault"], profiles={"keeper": {"allow_read": ["/opt/vault"]}}),
    )
    rules = claude_settings_for(policy)["permissions"]["deny"]
    assert "Read(//opt/vault)" not in rules
    assert "Edit(//opt/vault)" in rules


def test_a_policy_dict_without_deny_edit_still_renders_the_default_edit_rules():
    data = POLICY.to_dict()
    del data["deny_edit"]
    rules = claude_settings_for(policy_from_dict(data))["permissions"]["deny"]
    assert "Edit(~/.lionagi/settings.yaml)" in rules
    assert "Edit(~/.ssh)" in rules


@pytest.mark.parametrize("value", ["~/.x", [""], ["relative/path"], ["/a/../b"]])
def test_a_malformed_deny_edit_refuses(value):
    with pytest.raises(ConfigurationError, match="deny_edit"):
        resolve_child_sandbox(None, _settings(deny_edit=value))


# --------------------------------------------------------------------------- policy dicts


# --------------------------------------------------------------------------- stored policies


def _stored(deny_read, *, profile=None, allow_write=("~/.cache/uv",), enabled=True):
    return ChildSandboxPolicy(enabled, tuple(deny_read), tuple(allow_write), (), profile)


def test_a_stored_policy_follows_the_global_switch_off(monkeypatch):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )
    assert effective_policy(_stored(["~/.ssh"])).enabled is False


def test_a_stored_policy_regains_a_denial_whose_exemption_was_removed(monkeypatch):
    # Launched as builder while ~/.npmrc was exempt; the exemption is gone when it resumes.
    launched = _stored([p for p in RESOLVED_DENY_READ if p != "~/.npmrc"], profile="builder")
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    assert "~/.npmrc" in effective_policy(launched).deny_read


def test_a_stored_exemption_holds_while_it_is_still_configured(monkeypatch):
    launched = _stored([p for p in RESOLVED_DENY_READ if p != "~/.npmrc"], profile="builder")
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: _settings(profiles={"builder": {"allow_read": ["~/.npmrc"]}}),
    )
    assert "~/.npmrc" not in effective_policy(launched).deny_read


def test_a_stored_policy_without_a_profile_gets_no_exemption(monkeypatch):
    # A policy stored before the profile was recorded resolves as a bare launch.
    old = {"enabled": True, "deny_read": ["~/.ssh"], "allow_write": [], "deny_edit": []}
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: _settings(profiles={"builder": {"allow_read": ["~/.npmrc"]}}),
    )
    assert "~/.npmrc" in effective_policy(policy_from_dict(old)).deny_read


def test_a_stored_policy_keeps_its_own_extra_denials_and_only_allowed_writes(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    policy = effective_policy(_stored(["/opt/keys"], allow_write=["~/.cache/uv", "/tmp/anything"]))
    assert policy.deny_read == ("/opt/keys", *RESOLVED_DENY_READ)
    assert policy.allow_write == ("~/.cache/uv",)


def test_a_policy_survives_a_round_trip_through_json():
    policy = resolve_child_sandbox("builder", _settings(deny_read=["~/vault"]))
    assert policy_from_dict(json.loads(json.dumps(policy.to_dict()))) == policy


def test_a_disabled_policy_round_trips():
    policy = ChildSandboxPolicy(False, (), ())
    assert policy_from_dict(policy.to_dict()) == policy


def test_a_policy_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLICY.enabled = False


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("on", "must be a mapping"),
        ({"enabled": 1}, "enabled must be true or false"),
        ({"surprise": 1}, "unknown keys"),
        ({"deny_read": "~/x"}, "must be a list of non-empty strings"),
        ({"deny_read": ["relative"]}, "absolute path or start with"),
        ({"allow_write": [3]}, "must be a list of non-empty strings"),
    ],
)
def test_a_malformed_policy_dict_refuses(data, message):
    with pytest.raises(ConfigurationError, match=message):
        policy_from_dict(data)


# --------------------------------------------------------------------------- merging settings


def test_no_existing_settings_gives_exactly_the_policy_block():
    assert json.loads(claude_settings_argument(POLICY)) == claude_settings_for(POLICY)


def test_inline_settings_are_merged_and_nothing_is_dropped():
    existing = json.dumps(
        {
            "model": "haiku",
            "permissions": {"deny": ["Bash(rm:*)"], "allow": ["Read(docs)"]},
        }
    )
    merged = json.loads(claude_settings_argument(POLICY, existing))
    ours = claude_settings_for(POLICY)
    assert merged["model"] == "haiku"
    assert merged["sandbox"]["enabled"] is True
    assert merged["sandbox"]["failIfUnavailable"] is True
    assert merged["sandbox"]["allowUnsandboxedCommands"] is False
    assert merged["sandbox"] == ours["sandbox"]
    assert merged["permissions"]["deny"] == ["Bash(rm:*)", *ours["permissions"]["deny"]]
    assert merged["permissions"]["allow"] == ["Read(docs)"]


def test_an_entry_the_caller_already_lists_is_not_repeated():
    existing = json.dumps({"permissions": {"deny": ["Read(~/.ssh)"]}})
    merged = json.loads(claude_settings_argument(POLICY, existing))
    assert merged["permissions"]["deny"] == claude_settings_for(POLICY)["permissions"]["deny"]


@pytest.mark.parametrize(
    "block",
    [
        {"excludedCommands": ["bash"]},
        {"filesystem": {"allowWrite": ["/"]}},
        {"filesystem": {"allowRead": ["~/.ssh"]}},
        {"enabled": False},
        {},
    ],
)
def test_settings_that_carry_a_sandbox_block_of_their_own_are_refused(block):
    """Their keys would sit beside the policy's at the same level, and some loosen it."""
    with pytest.raises(ConfigurationError, match="carry a sandbox block"):
        claude_settings_argument(POLICY, json.dumps({"model": "haiku", "sandbox": block}))


def test_a_settings_file_is_read_against_the_base_directory(tmp_path):
    _write(tmp_path / "base.json", json.dumps({"model": "haiku"}))
    merged = json.loads(claude_settings_argument(POLICY, "base.json", tmp_path))
    assert merged["model"] == "haiku"
    assert merged["sandbox"] == claude_settings_for(POLICY)["sandbox"]


def test_an_absolute_settings_file_ignores_the_base_directory(tmp_path):
    path = _write(tmp_path / "abs.json", json.dumps({"model": "haiku"}))
    merged = json.loads(claude_settings_argument(POLICY, path, tmp_path / "elsewhere"))
    assert merged["model"] == "haiku"


def test_a_missing_settings_file_refuses(tmp_path):
    with pytest.raises(ConfigurationError, match="cannot read settings file"):
        claude_settings_argument(POLICY, "absent.json", tmp_path)


def test_a_settings_file_that_is_not_an_object_refuses(tmp_path):
    _write(tmp_path / "list.json", "[1, 2]")
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        claude_settings_argument(POLICY, "list.json", tmp_path)


def test_a_settings_file_that_is_not_json_refuses(tmp_path):
    _write(tmp_path / "text.json", "not json")
    with pytest.raises(ConfigurationError, match="is not valid JSON"):
        claude_settings_argument(POLICY, "text.json", tmp_path)


def test_inline_settings_that_are_not_json_refuse():
    with pytest.raises(ConfigurationError, match="is not valid JSON"):
        claude_settings_argument(POLICY, '{"model":')


def test_an_existing_key_of_an_unexpected_type_refuses():
    existing = json.dumps({"permissions": {"deny": "Bash(rm:*)"}})
    with pytest.raises(ConfigurationError, match="unexpected type"):
        claude_settings_argument(POLICY, existing)


# --------------------------------------------------------------------------- CLI floor


@pytest.mark.parametrize("version", [CLAUDE_CLI_FLOOR, (2, 1, 400), (2, 2, 0), (3, 0, 0)])
def test_a_cli_at_or_above_the_floor_passes(monkeypatch, version):
    monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: version)
    check_claude_cli_floor("claude")


@pytest.mark.parametrize("version", [(2, 1, 322), (2, 0, 999), (1, 9, 9)])
def test_a_cli_below_the_floor_refuses(monkeypatch, version):
    monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: version)
    with pytest.raises(ConfigurationError, match="older than"):
        check_claude_cli_floor("claude")


def test_an_unreadable_version_refuses(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: None)
    with pytest.raises(ConfigurationError, match="cannot read the version"):
        check_claude_cli_floor("claude")


def _script(tmp_path, body):
    path = tmp_path / "claude"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_the_probe_reads_the_version_the_binary_prints(tmp_path):
    binary = _script(tmp_path, 'echo "2.1.323 (Claude Code)"')
    assert child_sandbox._cli_version(binary) == (2, 1, 323)
    check_claude_cli_floor(binary)


def test_the_probe_refuses_an_older_binary(tmp_path):
    binary = _script(tmp_path, 'echo "2.0.1 (Claude Code)"')
    with pytest.raises(ConfigurationError, match="older than"):
        check_claude_cli_floor(binary)


def test_the_probe_refuses_a_binary_that_prints_no_version(tmp_path):
    binary = _script(tmp_path, "echo hello")
    with pytest.raises(ConfigurationError, match="cannot read the version"):
        check_claude_cli_floor(binary)


def test_the_probe_refuses_a_binary_that_cannot_run(tmp_path):
    with pytest.raises(ConfigurationError, match="cannot read the version"):
        check_claude_cli_floor(str(tmp_path / "absent"))


def test_a_failed_probe_is_not_remembered(tmp_path):
    binary = str(tmp_path / "claude")
    assert child_sandbox._cli_version(binary) is None
    _script(tmp_path, 'echo "2.1.323 (Claude Code)"')
    assert child_sandbox._cli_version(binary) == (2, 1, 323)


def test_the_probe_refuses_a_binary_that_exits_non_zero(tmp_path):
    binary = _script(tmp_path, 'echo "2.1.323 (Claude Code)"; exit 3')
    with pytest.raises(ConfigurationError, match="cannot read the version"):
        check_claude_cli_floor(binary)


def test_the_probe_reads_only_a_version_at_the_start_of_a_line(tmp_path):
    binary = _script(
        tmp_path, 'echo "wrapper for node v22.11.0"; echo "built at /opt/claude-3.0.0"'
    )
    with pytest.raises(ConfigurationError, match="cannot read the version"):
        check_claude_cli_floor(binary)


def test_the_probe_finds_the_version_line_after_other_output(tmp_path):
    binary = _script(tmp_path, 'echo "update available"; echo "2.1.400 (Claude Code)"')
    assert child_sandbox._cli_version(binary) == (2, 1, 400)


def test_the_probe_survives_output_that_is_not_utf8(tmp_path):
    binary = _script(tmp_path, "printf '\\377\\376\\n2.1.323 (Claude Code)\\n'")
    assert child_sandbox._cli_version(binary) == (2, 1, 323)


# --------------------------------------------------------------------------- the sandbox backend


def _which_without(*absent):
    return lambda name: None if name in absent else f"/usr/bin/{name}"


def test_native_windows_refuses_whatever_is_installed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(child_sandbox.shutil, "which", _which_without())
    with pytest.raises(ConfigurationError, match="no backend on native Windows"):
        check_sandbox_backend()


@pytest.mark.parametrize("absent", [("bwrap",), ("socat",), ("bwrap", "socat")])
def test_linux_refuses_and_names_each_missing_program(monkeypatch, absent):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(child_sandbox.shutil, "which", _which_without(*absent))
    with pytest.raises(ConfigurationError) as caught:
        check_sandbox_backend()
    assert f"missing: {', '.join(absent)}. " in str(caught.value)


def test_linux_with_both_programs_passes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(child_sandbox.shutil, "which", _which_without())
    check_sandbox_backend()


@pytest.mark.parametrize("platform", ["cygwin", "msys", "freebsd14", "openbsd7", "aix"])
def test_any_other_platform_refuses(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(child_sandbox.shutil, "which", _which_without())
    with pytest.raises(ConfigurationError, match=f"no backend on {platform}"):
        check_sandbox_backend()


def test_macos_needs_no_program_and_looks_none_up(monkeypatch):
    def which(name):
        raise AssertionError(f"looked up {name} on macOS")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(child_sandbox.shutil, "which", which)
    check_sandbox_backend()


# --------------------------------------------------------------------------- the opt-out sentence


def _floor_check(version):
    def run(tmp_path, monkeypatch):
        monkeypatch.setattr(child_sandbox, "_cli_version", lambda binary: version)
        check_claude_cli_floor("claude")

    return run


def _unreadable_global(tmp_path, monkeypatch):
    import lionagi._settings as settings_mod

    def broken(include_project=True):
        raise settings_mod.yaml.YAMLError("child_sandbox: [unclosed")

    monkeypatch.setattr(settings_mod, "load_settings", broken)
    monkeypatch.setattr(child_sandbox, "_global_settings", _REAL_GLOBAL_SETTINGS)
    resolve_child_sandbox(None)


def _backend_check(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(child_sandbox.shutil, "which", _which_without("bwrap"))
    check_sandbox_backend()


_REFUSALS = {
    "malformed value": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, _settings(enabled="false")
    ),
    "block that is not a mapping": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, {"child_sandbox": "on"}
    ),
    "unknown key": lambda tmp_path, monkeypatch: resolve_child_sandbox(None, _settings(surprise=1)),
    "allow_read not on the list": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        "builder", _settings(profiles={"builder": {"allow_read": ["~/.nothing"]}})
    ),
    "missing extends file": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, _settings(extends=[str(tmp_path / "absent.yaml")])
    ),
    "malformed extends file": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None,
        _settings(extends=[_write(tmp_path / "broken.yaml", "deny_read: [unclosed\n")]),
    ),
    "extends file that is not a mapping": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, _settings(extends=[_write(tmp_path / "list.yaml", "- a\n")])
    ),
    "extends file with an unknown key": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, _settings(extends=[_write(tmp_path / "off.yaml", "enabled: false\n")])
    ),
    "malformed policy dict": lambda tmp_path, monkeypatch: policy_from_dict({"surprise": 1}),
    "unreadable existing settings": lambda tmp_path, monkeypatch: claude_settings_argument(
        POLICY, "absent.json", tmp_path
    ),
    "existing settings not JSON": lambda tmp_path, monkeypatch: claude_settings_argument(
        POLICY, '{"model":'
    ),
    "existing settings not an object": lambda tmp_path, monkeypatch: claude_settings_argument(
        POLICY, _write(tmp_path / "list.json", "[1, 2]"), tmp_path
    ),
    "existing settings of an odd type": lambda tmp_path, monkeypatch: claude_settings_argument(
        POLICY, json.dumps({"permissions": {"deny": "Bash(rm:*)"}})
    ),
    "existing settings with a sandbox block": lambda tmp_path, monkeypatch: (
        claude_settings_argument(POLICY, json.dumps({"sandbox": {"excludedCommands": ["bash"]}}))
    ),
    "path with a dot-dot segment": lambda tmp_path, monkeypatch: resolve_child_sandbox(
        None, _settings(allow_write=["~/.cache/uv/../../.ssh"])
    ),
    "unreadable global settings file": _unreadable_global,
    "CLI below the floor": _floor_check((2, 1, 322)),
    "unreadable CLI version": _floor_check(None),
    "no sandbox backend": _backend_check,
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_every_refusal_ends_with_the_opt_out_sentence(case, tmp_path, monkeypatch):
    with pytest.raises(ConfigurationError) as caught:
        _REFUSALS[case](tmp_path, monkeypatch)
    text = str(caught.value)
    assert text.endswith(CHILD_SANDBOX_OPT_OUT)
    assert text.count(CHILD_SANDBOX_OPT_OUT) == 1
    assert text != CHILD_SANDBOX_OPT_OUT


def test_the_opt_out_sentence_names_the_key_and_the_file():
    assert "`child_sandbox: {enabled: false}`" in CHILD_SANDBOX_OPT_OUT
    assert "~/.lionagi/settings.yaml" in CHILD_SANDBOX_OPT_OUT
    assert CHILD_SANDBOX_OPT_OUT.endswith(".")


# --------------------------------------------------------------------------- the project layer


class _RecordingLogger:
    """Stands in for the module logger and keeps each formatted warning."""

    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


@pytest.fixture
def project_warnings(monkeypatch):
    """The warnings logged for project settings files, with the once-per-file memory reset."""
    recorder = _RecordingLogger()
    monkeypatch.setattr(child_sandbox, "logger", recorder)
    monkeypatch.setattr(child_sandbox, "_warned_project_files", set())
    return recorder.warnings


def _project(tmp_path, name, text):
    """A project directory whose ``.lionagi/settings.yaml`` holds ``text``; returns both paths."""
    project = tmp_path / name
    (project / ".lionagi").mkdir(parents=True)
    settings_file = project / ".lionagi" / "settings.yaml"
    settings_file.write_text(text, encoding="utf-8")
    return project, settings_file


def test_a_project_child_sandbox_block_is_reported_as_ignored(tmp_path, project_warnings):
    project, settings_file = _project(tmp_path, "proj", "child_sandbox:\n  enabled: false\n")
    warn_project_child_sandbox(project)
    assert len(project_warnings) == 1
    message = project_warnings[0]
    assert str(settings_file) in message
    assert "ignored" in message
    assert "only the global settings file" in message


def test_a_project_file_is_reported_once_per_process(tmp_path, project_warnings):
    project, _ = _project(tmp_path, "proj", "child_sandbox: {enabled: false}\n")
    (project / "sub").mkdir()
    warn_project_child_sandbox(project)
    warn_project_child_sandbox(project)
    warn_project_child_sandbox(project / "sub")
    assert len(project_warnings) == 1


def test_each_project_file_is_reported_on_its_own(tmp_path, project_warnings):
    first, first_file = _project(tmp_path, "one", "child_sandbox: {}\n")
    second, second_file = _project(tmp_path, "two", "child_sandbox: {}\n")
    warn_project_child_sandbox(first)
    warn_project_child_sandbox(second)
    assert len(project_warnings) == 2
    assert str(first_file) in project_warnings[0]
    assert str(second_file) in project_warnings[1]


def test_a_project_file_without_the_block_is_not_reported(tmp_path, project_warnings):
    project, _ = _project(tmp_path, "proj", "other_setting: 1\n")
    warn_project_child_sandbox(project)
    assert project_warnings == []


def test_a_project_with_no_settings_file_is_not_reported(tmp_path, project_warnings):
    project = tmp_path / "bare"
    project.mkdir()
    warn_project_child_sandbox(project)
    assert project_warnings == []


@pytest.mark.parametrize("text", ["child_sandbox: [unclosed\n", "- a\n- b\n"])
def test_an_unusable_project_file_is_left_to_the_settings_loader(tmp_path, project_warnings, text):
    project, _ = _project(tmp_path, "proj", text)
    warn_project_child_sandbox(project)
    assert project_warnings == []


@pytest.fixture
def project_checks(monkeypatch):
    """The calls ``resolve_child_sandbox`` makes to the project-layer warning."""
    calls = []
    monkeypatch.setattr(
        child_sandbox, "warn_project_child_sandbox", lambda *args: calls.append(args)
    )
    return calls


def test_resolving_without_settings_checks_the_project_layer(monkeypatch, project_checks):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    resolve_child_sandbox(None)
    assert project_checks == [()]


def test_resolving_with_explicit_settings_skips_the_project_layer(project_checks):
    resolve_child_sandbox(None, {})
    assert project_checks == []


def test_resolving_warns_about_a_project_block_in_the_working_directory(
    tmp_path, monkeypatch, project_warnings
):
    project, settings_file = _project(tmp_path, "proj", "child_sandbox: {enabled: false}\n")
    monkeypatch.chdir(project)
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    # The project block is reported and not applied: the policy stays on.
    assert resolve_child_sandbox(None) == DEFAULT_POLICY
    assert len(project_warnings) == 1
    assert str(settings_file.resolve()) in project_warnings[0]
