# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A ``light`` CLI agent is held to what its engine can enforce: codex in its
workspace-write sandbox with no network and no extra writable roots, agy under
``--sandbox``, Claude Code without its file-writing tools. A setting that would undo
that is refused, and Pi, which has no sandbox to pin, refuses ``light`` outright. A
``heavy`` codex agent keeps the network setting its user config gives and its temporary
directories, but its extra writable roots are the child sandbox's ``allow_write`` list, not
whatever the user config lists."""

from __future__ import annotations

import os
import subprocess

import pytest

from lionagi._errors import CLIInputRefusedError, ConfigurationError
from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest
from lionhub.providers.google.gemini_code import GeminiCodeRequest
from lionhub.providers.openai.codex import CodexCodeRequest


def _codex(tmp_path, **kw) -> list[str]:
    return CodexCodeRequest(prompt="hi", repo=tmp_path, **kw).as_cmd_args()


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


@pytest.mark.parametrize(
    "kw", [{}, {"full_auto": True}, {"sandbox": "workspace-write"}, {"ask_for_approval": "never"}]
)
def test_a_light_codex_agent_is_sandboxed_to_its_cwd_with_no_network(tmp_path, kw):
    args = _codex(tmp_path, workload="light", **kw)
    assert _pairs(args, "--sandbox") == ["workspace-write"]
    assert "-s" not in args
    overrides = _pairs(args, "-c")
    assert "sandbox_workspace_write.writable_roots=[]" in overrides
    assert "sandbox_workspace_write.network_access=false" in overrides
    # Neither /tmp nor $TMPDIR stays writable: the working directory is all it writes.
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in overrides
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in overrides
    assert "--add-dir" not in args


@pytest.mark.parametrize("kw", [{}, {"full_auto": True}, {"bypass_approvals": True}])
def test_a_heavy_codex_agent_is_given_neither_temp_directory_pin(tmp_path, kw):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for workload in ({}, {"workload": "heavy"}):
            overrides = _pairs(_codex(tmp_path, **workload, **kw), "-c")
            sandbox = [o for o in overrides if o.startswith("sandbox_workspace_write.")]
            assert not [o for o in sandbox if "writable_roots" not in o]


def _heavy_roots_pin(roots) -> str:
    from lionhub.providers.openai.codex import toml_override_value

    return f"sandbox_workspace_write.writable_roots={toml_override_value(list(roots))}"


@pytest.fixture
def sandbox_on(monkeypatch):
    """The child sandbox on with the default policy (the suite switches it off by default)."""
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


@pytest.fixture(autouse=True)
def _no_host_probe_record(monkeypatch):
    """These tests cover argv, roots and config refusals, not the probe record.

    The launch gate runs on every render under the permission profile, and an unpatched record
    lookup would run the host's codex binary to read its version.
    """
    import lionhub.providers.openai.codex as codex_module

    monkeypatch.setattr(
        codex_module,
        "require_passing_probe_record",
        lambda binary, env=None, cwd=None, table=None, scratch=False: "probe-record",
    )


def _default_roots():
    import os

    from lionhub.agent.child_sandbox import default_allow_write

    return [os.path.expanduser(path) for path in default_allow_write()]


def _profile_table(args: list[str]) -> dict:
    """The permission profile table a codex command line carries, parsed back from its override."""
    import tomllib

    rows = [o for o in _pairs(args, "-c") if o.startswith("permissions.lionagi_child=")]
    assert len(rows) == 1
    return tomllib.loads("table = " + rows[0].split("=", 1)[1])["table"]


def _expected_profile(env, *, write, light=False, read_only=False, network=False) -> dict:
    """The table a launch builds for these inputs, rendered and parsed as a command line carries it."""
    import tomllib

    from lionhub.agent.child_sandbox import resolve_child_sandbox
    from lionhub.providers.openai._codex_permissions import (
        deny_entries,
        permission_profile_table,
    )
    from lionhub.providers.openai._codex_profile import codex_home_for_env
    from lionhub.providers.openai.codex import toml_override_value

    policy = resolve_child_sandbox(None)
    table = permission_profile_table(
        deny=deny_entries(policy.deny_read, codex_home_for_env(env), env),
        write=write,
        light=light,
        read_only=read_only,
        network=network,
    )
    return tomllib.loads("table = " + toml_override_value(table))["table"]


@pytest.mark.parametrize(
    "kw",
    [
        {"full_auto": True},
        {"sandbox": "workspace-write"},
        {"workload": "heavy", "sandbox": "workspace-write"},
        {"workload": "heavy", "full_auto": True},
    ],
)
def test_a_heavy_codex_agent_writes_only_the_toolchain_caches_beyond_its_cwd(
    tmp_path, kw, sandbox_on
):
    env = _home_with_config(tmp_path, "")
    args = _codex(tmp_path, env=env, **kw)
    # Under the permission profile there is no sandbox mode and no legacy pin: the toolchain
    # caches are the profile's write entries, and the profile is the last override.
    assert "--sandbox" not in args
    assert "-s" not in args
    overrides = _pairs(args, "-c")
    assert not [o for o in overrides if o.startswith("sandbox_workspace_write.")]
    assert overrides[-1].startswith("permissions.lionagi_child=")
    assert overrides[-2].startswith("default_permissions=")
    assert _profile_table(args) == _expected_profile(env, write=_default_roots())
    assert all(root.startswith("/") for root in _default_roots())


def test_a_heavy_codex_agent_that_bypasses_the_sandbox_gets_no_pin(tmp_path):
    # Only reachable with the child sandbox off; with it on the request is refused (below).
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        overrides = _pairs(_codex(tmp_path, bypass_approvals=True), "-c")
    assert not [o for o in overrides if o.startswith("sandbox_workspace_write.")]


def test_the_heavy_profile_beats_the_user_configs_writable_roots(tmp_path, sandbox_on):
    env = _home_with_config(
        tmp_path,
        "[sandbox_workspace_write]\nnetwork_access = true\n"
        f"writable_roots = [{str(tmp_path / 'everything')!r}]\n",
    )
    args = _codex(tmp_path, full_auto=True, ignore_user_config=True, env=env)
    # Codex ignores `sandbox_workspace_write.*` under a profile, so the profile's write entries
    # are the child sandbox's roots and never the roots the user config lists.
    table = _profile_table(args)
    # The ignored config's `network_access = true` carries into the profile's network key.
    assert table == _expected_profile(env, write=_default_roots(), network=True)
    assert "everything" not in str(table)
    assert _pairs(args, "-c")[-1].startswith("permissions.lionagi_child=")


@pytest.mark.parametrize(
    "overrides",
    [
        {"sandbox_workspace_write.writable_roots": ["/opt/build"]},
        {"sandbox_workspace_write": {"writable_roots": ["/opt/build"]}},
    ],
)
def test_writable_roots_the_caller_sets_are_in_the_profile(tmp_path, overrides, sandbox_on):
    env = _home_with_config(tmp_path, "")
    args = _codex(tmp_path, env=env, full_auto=True, config_overrides=overrides)
    # Codex ignores `sandbox_workspace_write.*` under a profile, so the roots are not emitted that
    # way: the profile's write entries carry them, in place of the child sandbox's own list.
    assert not [o for o in _pairs(args, "-c") if o.startswith("sandbox_workspace_write")]
    assert _profile_table(args) == _expected_profile(env, write=["/opt/build"])


def test_the_heavy_roots_follow_the_child_sandbox_settings(tmp_path, monkeypatch):
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"allow_write": ["/opt/c"]}}
    )
    env = _home_with_config(tmp_path, "")
    args = _codex(tmp_path, env=env, full_auto=True)
    assert _profile_table(args) == _expected_profile(env, write=[*_default_roots(), "/opt/c"])
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )
    args = _codex(tmp_path, env=env, full_auto=True)
    # With the child sandbox off the command line is the sandbox-mode one: no profile, no pin.
    overrides = _pairs(args, "-c")
    assert not [o for o in overrides if o.startswith(("sandbox_workspace_write.", "permissions."))]
    assert _pairs(args, "--sandbox") == ["workspace-write"]


@pytest.mark.parametrize("kw", [{}, {"enable_features": ["apps", "plugins"]}])
def test_a_light_codex_agent_runs_with_the_accounts_apps_and_plugins_off(tmp_path, kw):
    args = _codex(tmp_path, workload="light", **kw)
    assert {"apps", "plugins"} <= set(_pairs(args, "--disable"))
    assert {"apps", "plugins"}.isdisjoint(_pairs(args, "--enable"))
    heavy = _codex(tmp_path, **kw)
    assert {"apps", "plugins"}.isdisjoint(_pairs(heavy, "--disable"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"plugins.x.enabled": True},
        {'"plugins"."x@y".enabled': True},
        {"apps.x.enabled": True},
        {"plugins": {"x": {"enabled": True}}},
    ],
)
def test_a_light_codex_agent_drops_a_setting_under_the_apps_or_plugins_table(tmp_path, overrides):
    light = _pairs(_codex(tmp_path, workload="light", config_overrides=overrides), "-c")
    assert not any("enabled" in o for o in light)
    heavy = _pairs(_codex(tmp_path, config_overrides=overrides), "-c")
    assert any("enabled" in o for o in heavy)


def test_a_light_codex_agent_asked_for_read_only_stays_read_only(tmp_path):
    args = _codex(tmp_path, workload="light", sandbox="read-only")
    assert _pairs(args, "--sandbox") == ["read-only"]


def test_a_light_codex_agent_in_a_linked_worktree_gets_no_writable_git_dir(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *cmd], cwd=main, check=True
        )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    heavy = CodexCodeRequest(prompt="hi", repo=wt, full_auto=True).as_cmd_args()
    light = CodexCodeRequest(prompt="hi", repo=wt, workload="light").as_cmd_args()
    assert "--add-dir" in heavy
    assert "--add-dir" not in light


@pytest.mark.parametrize(
    ("kw", "named"),
    [
        ({"bypass_approvals": True}, "bypass_approvals"),
        ({"sandbox": "danger-full-access"}, "danger-full-access"),
        ({"add_dir": ["/tmp/elsewhere"]}, "add_dir"),
        ({"config_overrides": {"sandbox_workspace_write.network_access": True}}, "network_access"),
        ({"config_overrides": {"sandbox_workspace_write": {"writable_roots": ["/"]}}}, "sandbox_"),
        (
            {"config_overrides": {"sandbox_workspace_write.exclude_slash_tmp": False}},
            "exclude_slash_tmp",
        ),
        (
            {"config_overrides": {"sandbox_workspace_write": {"exclude_tmpdir_env_var": False}}},
            "sandbox_workspace_write",
        ),
        ({"config_overrides": {"sandbox_mode": "danger-full-access"}}, "sandbox_mode"),
        ({"account_connectors": ["calendar"]}, "account_connectors"),
    ],
)
def test_a_light_codex_agent_that_could_write_elsewhere_is_refused_at_construction(
    tmp_path, kw, named
):
    with pytest.raises(CLIInputRefusedError, match=named) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light", **kw)
    assert "Declare the agent's workload 'heavy'" in str(refused.value)


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({'"sandbox_mode"': "danger-full-access"}, "sandbox_mode"),
        ({"sandbox_workspace_write . writable_roots": ["/tmp"]}, "sandbox_workspace_write"),
        ({'"sandbox_workspace_write"."network_access"': True}, "sandbox_workspace_write"),
        ({'"sandbox_workspace_write"."exclude_slash_tmp"': False}, "sandbox_workspace_write"),
        ({"sandbox_workspace_write . exclude_tmpdir_env_var": False}, "sandbox_workspace_write"),
        ({"profile": "unconfined"}, "profile"),
        ({"profiles.unconfined.sandbox_mode": "danger-full-access"}, "profiles"),
        ({"default_permissions": "wide"}, "set default_permissions"),
        ({"permissions.wide.network.enabled": True}, "set permissions.wide.network.enabled"),
    ],
)
def test_a_light_codex_override_is_judged_by_the_key_codex_reads(tmp_path, overrides, named):
    with pytest.raises(CLIInputRefusedError, match=named):
        CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light", config_overrides=overrides)


def test_a_light_codex_override_key_toml_cannot_read_is_refused(tmp_path):
    with pytest.raises(CLIInputRefusedError, match="entry 0 is not a TOML key"):
        CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light", config_overrides={"a b": 1})


def test_a_light_codex_agent_refuses_a_profile_lionagi_did_not_write(tmp_path):
    with pytest.raises(CLIInputRefusedError, match="profile='unconfined'"):
        CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light", profile="unconfined")
    generated = "lionagi-mcp-" + "0" * 32
    args = _codex(tmp_path, workload="light", profile=generated, env={"CODEX_HOME": str(tmp_path)})
    assert _pairs(args, "--profile") + _pairs(args, "-p") == [generated]


def _home_with_config(tmp_path, text: str) -> dict[str, str]:
    # Beside the working directory, not inside it: a write root over CODEX_HOME is refused.
    home = tmp_path.parent / f"{tmp_path.name}-codex-home"
    home.mkdir()
    (home / "config.toml").write_text(text)
    return {"CODEX_HOME": str(home)}


# The project config sits at the git root; the agent runs there or in a directory below it.
_IN_THE_ROOT_OR_BELOW = pytest.mark.parametrize("below", ["", "sub/dir"])


@_IN_THE_ROOT_OR_BELOW
def test_a_light_codex_agent_refuses_a_root_profile_in_the_configs_codex_reads(tmp_path, below):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cwd = repo / below
    cwd.mkdir(parents=True, exist_ok=True)
    env = _home_with_config(tmp_path, 'profile = "wide"\n')
    with pytest.raises(CLIInputRefusedError, match="root profile key"):
        _codex(cwd, workload="light", env=env)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('profile = "wide"\n')
    with pytest.raises(CLIInputRefusedError, match="project codex config"):
        _codex(cwd, workload="light", env={"CODEX_HOME": str(tmp_path / "empty")})
    # The same configs leave a heavy agent's command line alone.
    assert "--sandbox" not in _codex(cwd, workload="heavy", env=env)


@_IN_THE_ROOT_OR_BELOW
def test_a_light_codex_agent_refuses_default_permissions_in_the_configs_codex_reads(
    tmp_path, below
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cwd = repo / below
    cwd.mkdir(parents=True, exist_ok=True)
    env = _home_with_config(tmp_path, 'default_permissions = "wide"\n')
    with pytest.raises(CLIInputRefusedError, match="sets `default_permissions`") as refused:
        _codex(cwd, workload="light", env=env)
    assert "codex-home" in str(refused.value)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('default_permissions = "wide"\n')
    empty = {"CODEX_HOME": str(tmp_path / "empty")}
    with pytest.raises(CLIInputRefusedError, match="sets `default_permissions`") as refused:
        _codex(cwd, workload="light", env=empty)
    assert str(repo / ".codex" / "config.toml") in str(refused.value)
    # The same configs leave a heavy agent's command line alone.
    assert "--sandbox" not in _codex(cwd, workload="heavy", env=env)


def _walk_layout(tmp_path, layout):
    """The agent's cwd and the directory holding the project config, for each walk the
    config search makes."""
    if layout == "no git root":
        cwd = tmp_path / "top" / "a" / "b"
        cwd.mkdir(parents=True)
        return cwd, tmp_path / "top"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    if layout == "between the cwd and the git root":
        cwd = repo / "sub" / "dir"
        cwd.mkdir(parents=True)
        return cwd, repo / "sub"
    if layout == "cwd is a symlink into the repo":
        real = repo / "sub" / "dir"
        real.mkdir(parents=True)
        cwd = tmp_path / "link"
        cwd.symlink_to(real)
        return cwd, repo
    # The path as spelled is in the repo; the resolved path is outside it.
    outside = tmp_path / "outside" / "dir"
    outside.mkdir(parents=True)
    cwd = repo / "link"
    cwd.symlink_to(outside)
    return cwd, repo


@pytest.mark.parametrize(
    "layout",
    [
        "between the cwd and the git root",
        "cwd is a symlink into the repo",
        "cwd is a symlink in the repo to outside it",
        "no git root",
    ],
)
def test_a_light_codex_agent_reads_each_project_config_codex_can_merge(tmp_path, layout):
    cwd, holder = _walk_layout(tmp_path, layout)
    config = holder / ".codex" / "config.toml"
    config.parent.mkdir()
    empty = {"CODEX_HOME": str(tmp_path / "empty")}
    config.write_text('profile = "wide"\n')
    with pytest.raises(CLIInputRefusedError, match="project codex config"):
        _codex(cwd, workload="light", env=empty)
    config.write_text('default_permissions = "wide"\n')
    with pytest.raises(CLIInputRefusedError, match="sets `default_permissions`") as refused:
        _codex(cwd, workload="light", env=empty)
    assert str(config) in str(refused.value) or str(config.resolve()) in str(refused.value)


# A linked worktree's root holds a `.git` file naming the main checkout's git directory.
@pytest.mark.parametrize("git_marker", ["directory", "file"])
def test_a_light_codex_agent_leaves_a_project_config_above_the_git_root_unread(
    tmp_path, git_marker
):
    repo = tmp_path / "repo"
    repo.mkdir()
    if git_marker == "directory":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n")
    cwd = repo / "sub"
    cwd.mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        'profile = "wide"\ndefault_permissions = "wide"\n'
    )
    args = _codex(cwd, workload="light", env={"CODEX_HOME": str(tmp_path / "empty")})
    assert _pairs(args, "--sandbox") == ["workspace-write"]


def test_a_light_codex_agent_refuses_a_config_defining_its_generated_profile(tmp_path):
    generated = "lionagi-mcp-" + "1" * 32
    env = _home_with_config(
        tmp_path, f'[profiles."{generated}"]\nsandbox_mode = "danger-full-access"\n'
    )
    with pytest.raises(CLIInputRefusedError, match="defines a profile named"):
        _codex(tmp_path, workload="light", profile=generated, env=env)


def test_a_light_codex_agent_refuses_a_codex_config_it_cannot_read(tmp_path):
    env = _home_with_config(tmp_path, "profile = \n")
    with pytest.raises(CLIInputRefusedError, match="Declare the agent's workload 'heavy'"):
        _codex(tmp_path, workload="light", env=env)


_LIGHT_PINS = [
    "sandbox_workspace_write.writable_roots=[]",
    "sandbox_workspace_write.network_access=false",
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
]


def test_the_light_pins_come_after_every_caller_override(tmp_path):
    args = _codex(tmp_path, workload="light", config_overrides={"model_verbosity": "low"})
    overrides = _pairs(args, "-c")
    assert overrides[-4:] == _LIGHT_PINS
    assert overrides.index('model_verbosity="low"') < len(overrides) - 4


def test_the_light_pins_come_after_the_settings_carried_beside_ignore_user_config(tmp_path):
    env = _home_with_config(
        tmp_path,
        "[sandbox_workspace_write]\nnetwork_access = true\n"
        f"writable_roots = [{str(tmp_path / 'elsewhere')!r}]\n"
        "exclude_slash_tmp = true\nexclude_tmpdir_env_var = true\n",
    )
    args = _codex(tmp_path, workload="light", ignore_user_config=True, env=env)
    assert "--ignore-user-config" in args
    sandbox = [o for o in _pairs(args, "-c") if o.startswith("sandbox_workspace_write.")]
    # The user config's settings are carried, and the pins still have the last word.
    assert "sandbox_workspace_write.network_access=true" in sandbox[:-4]
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in sandbox[:-4]
    assert sandbox[-4:] == _LIGHT_PINS


def test_a_copied_request_cannot_slip_a_light_agent_past_the_refusal(tmp_path):
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light")
    copied = request.model_copy(update={"bypass_approvals": True})
    with pytest.raises(CLIInputRefusedError, match="bypass_approvals"):
        copied.as_cmd_args()


@pytest.mark.parametrize("kw", [{}, {"full_auto": True}, {"bypass_approvals": True}])
def test_a_heavy_codex_agent_keeps_its_command_line(tmp_path, kw):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _codex(tmp_path, workload="heavy", **kw) == _codex(tmp_path, **kw)


def test_a_light_claude_agent_runs_without_its_file_writing_tools(tmp_path):
    args = ClaudeCodeRequest(
        prompt="hi", repo=tmp_path, workload="light", disallowed_tools=["WebFetch", "Write"]
    ).as_cmd_args()
    assert args.count("--disallowedTools") == 1
    start = args.index("--disallowedTools") + 1
    denied = []
    for token in args[start:]:
        if token.startswith("-"):
            break
        denied.append(token)
    assert denied == ["WebFetch", "Write", "Edit", "NotebookEdit"]
    assert "Bash" not in denied


def test_a_heavy_claude_agent_keeps_its_command_line(tmp_path):
    kw = {"prompt": "hi", "repo": tmp_path, "disallowed_tools": ["WebFetch"]}
    assert (
        ClaudeCodeRequest(**kw, workload="heavy").as_cmd_args()
        == ClaudeCodeRequest(**kw).as_cmd_args()
    )


def test_a_light_gemini_agent_always_runs_sandboxed(tmp_path):
    light = GeminiCodeRequest(prompt="hi", repo=tmp_path, workload="light").as_cmd_args()
    heavy = GeminiCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
    assert light.count("--sandbox") == 1
    assert "--sandbox" not in heavy


@pytest.mark.asyncio
async def test_a_light_pi_agent_is_refused_before_anything_starts(tmp_path, monkeypatch):
    import lionhub.providers.pi.cli as pi_cli

    monkeypatch.setattr(pi_cli, "PI_CLI", str(tmp_path / "pi-must-not-run"))
    request = pi_cli.PiCodeRequest(prompt="hi", repo=tmp_path, workload="light")
    with pytest.raises(CLIInputRefusedError, match="Pi has none"):
        async for _ in pi_cli._ndjson_from_cli(request):
            pass


# --------------------------------------------------------------------------- sandbox off, heavy


_SANDBOX_OFF = pytest.mark.parametrize(
    "kw",
    [
        {"bypass_approvals": True},
        {"sandbox": "danger-full-access"},
        {"config_overrides": {"sandbox_mode": "danger-full-access"}},
        {"config_overrides": {"profiles.p.sandbox_mode": "danger-full-access"}},
        {"config_overrides": {"default_permissions": "open"}},
    ],
)


@_SANDBOX_OFF
def test_a_heavy_codex_agent_may_not_turn_its_sandbox_off_while_confined(tmp_path, kw, sandbox_on):
    with pytest.raises(CLIInputRefusedError, match="child_sandbox: \\{enabled: false\\}"):
        _codex(tmp_path, workload="heavy", **kw)


@_SANDBOX_OFF
def test_with_the_child_sandbox_off_the_request_runs_as_before(tmp_path, kw):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _codex(tmp_path, workload="heavy", **kw)[:2] == ["exec", "--json"]


@pytest.mark.parametrize(
    "config",
    [
        'sandbox_mode = "danger-full-access"\n',
        'profile = "p"\n[profiles.p]\nsandbox_mode = "danger-full-access"\n',
        'default_permissions = "open"\n',
        "sandbox_mode = \n",
    ],
)
def test_a_codex_config_that_turns_the_sandbox_off_is_refused(tmp_path, config, sandbox_on):
    env = _home_with_config(tmp_path, config)
    with pytest.raises(CLIInputRefusedError, match="sandbox"):
        _codex(tmp_path, env=env)


def test_a_named_profile_that_turns_the_sandbox_off_is_refused(tmp_path, sandbox_on):
    env = _home_with_config(tmp_path, '[profiles.p]\nsandbox_mode = "danger-full-access"\n')
    with pytest.raises(CLIInputRefusedError, match="profile 'p'"):
        _codex(tmp_path, env=env, profile="p")
    assert _codex(tmp_path, env=env, profile="q")[:2] == ["exec", "--json"]


@pytest.mark.parametrize("request_kw", [{"full_auto": True}, {"sandbox": "workspace-write"}])
def test_a_request_mode_does_not_shield_a_config_that_turns_the_sandbox_off(
    tmp_path, sandbox_on, request_kw
):
    # Under the permission profile no `--sandbox` flag is emitted, so the request's own mode
    # displaces nothing: the config still decides, and a config that turns the sandbox off is refused.
    env = _home_with_config(tmp_path, 'sandbox_mode = "danger-full-access"\n')
    with pytest.raises(CLIInputRefusedError, match="danger-full-access"):
        _codex(tmp_path, env=env, **request_kw)


def test_a_request_mode_with_a_harmless_config_mode_renders_the_profile_only(tmp_path, sandbox_on):
    env = _home_with_config(tmp_path, 'sandbox_mode = "workspace-write"\n')
    args = _codex(tmp_path, env=env, full_auto=True)
    # The profile governs, so the request's own mode is not rendered as a sandbox flag at all.
    assert "--sandbox" not in args
    assert "-s" not in args
    assert _profile_table(args) == _expected_profile(env, write=_default_roots())


def test_a_project_codex_config_that_turns_the_sandbox_off_is_refused(tmp_path, sandbox_on):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    env = _home_with_config(tmp_path, "")
    with pytest.raises(CLIInputRefusedError, match="danger-full-access"):
        _codex(tmp_path, env=env)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory two levels under tmp_path, set as this process's HOME."""
    home = tmp_path / "users" / "u"
    (home / "inside").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


@pytest.mark.parametrize(
    "spell",
    [
        lambda home: str(home),
        lambda home: str(home.parent),
        lambda home: "/",
        lambda home: "~",
        lambda home: str(home) + "/",
    ],
    ids=["home", "parent", "root", "tilde", "trailing-slash"],
)
@pytest.mark.parametrize("where", ["add_dir", "override", "nested", "profile"])
def test_a_root_reaching_the_home_directory_is_refused_while_confined(
    tmp_path, fake_home, sandbox_on, spell, where
):
    root = spell(fake_home)
    kw = {
        "add_dir": {"add_dir": [root]},
        "override": {"config_overrides": {"sandbox_workspace_write.writable_roots": [root]}},
        "nested": {"config_overrides": {"sandbox_workspace_write": {"writable_roots": root}}},
        "profile": {
            "config_overrides": {"profiles.p.sandbox_workspace_write.writable_roots": [root]}
        },
    }[where]
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, **kw)


def test_a_link_to_the_home_directory_is_refused_while_confined(tmp_path, fake_home, sandbox_on):
    link = tmp_path / "link"
    link.symlink_to(fake_home)
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, add_dir=[str(link)])


def test_the_childs_own_home_is_checked_too(tmp_path, fake_home, sandbox_on):
    other = tmp_path / "other-home"
    other.mkdir()
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, env={"HOME": str(other)}, add_dir=[str(other)])


@pytest.mark.parametrize("which", ["home", "parent"])
def test_the_account_home_is_refused_under_a_scratch_home(
    tmp_path, fake_home, sandbox_on, monkeypatch, which
):
    """HOME points at a scratch directory; the account's home from the password
    database, and its parent, are still refused as writable roots."""
    pwd = pytest.importorskip("pwd")
    account = tmp_path / "accounts" / "me"
    (account / "project").mkdir(parents=True)
    fields = list(pwd.getpwuid(os.getuid()))
    fields[5] = str(account)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: pwd.struct_passwd(fields))
    root = account if which == "home" else account.parent
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, add_dir=[str(root)])
    inside = str(account / "project")
    assert inside in _pairs(_codex(_repo(tmp_path), full_auto=True, add_dir=[inside]), "--add-dir")


def test_the_real_account_home_is_refused_under_a_scratch_home(tmp_path, fake_home, sandbox_on):
    """The same with nothing faked but HOME. Builds the argv only; no child starts."""
    pwd = pytest.importorskip("pwd")
    account = pwd.getpwuid(os.getuid()).pw_dir
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, add_dir=[account])


def test_a_root_inside_the_home_directory_is_kept(tmp_path, fake_home, sandbox_on):
    inside = str(fake_home / "inside")
    args = _codex(_repo(tmp_path), full_auto=True, add_dir=[inside])
    assert inside in _pairs(args, "--add-dir")
    args = _codex(
        _repo(tmp_path),
        full_auto=True,
        config_overrides={"sandbox_workspace_write.writable_roots": [inside]},
    )
    assert [o for o in _pairs(args, "-c") if inside in o]


def test_a_writable_roots_value_that_is_not_a_list_of_paths_is_refused(
    tmp_path, fake_home, sandbox_on
):
    with pytest.raises(CLIInputRefusedError, match="other than a list of paths"):
        _codex(
            _repo(tmp_path),
            full_auto=True,
            config_overrides={"sandbox_workspace_write.writable_roots": 7},
        )


def test_with_the_child_sandbox_off_a_home_root_runs_as_before(tmp_path, fake_home):
    args = _codex(_repo(tmp_path), full_auto=True, add_dir=[str(fake_home)])
    assert str(fake_home) in _pairs(args, "--add-dir")


@pytest.mark.parametrize("which", ["home", "parent"])
def test_a_case_changed_spelling_of_a_home_root_is_refused_while_confined(
    tmp_path, fake_home, sandbox_on, which
):
    import os

    target = fake_home if which == "home" else fake_home.parent
    respelled = str(target.parent / target.name.upper())
    if not os.path.exists(respelled) or not os.path.samefile(respelled, target):
        pytest.skip("the volume under tmp_path is case-sensitive")
    with pytest.raises(CLIInputRefusedError, match="home directory or an ancestor"):
        _codex(_repo(tmp_path), full_auto=True, add_dir=[respelled])


async def _drain_pi(pi_cli, request):
    async for _ in pi_cli._ndjson_from_cli(request):
        pass


@pytest.fixture
def pi_launches(tmp_path, monkeypatch):
    """Pi's launch point replaced by a recorder; the list holds each command it was handed."""
    import lionhub.providers.pi.cli as pi_cli

    launched: list[list[str]] = []

    async def fake_ndjson(cmd, **_kw):
        launched.append(list(cmd))
        if False:
            yield {}

    monkeypatch.setattr(pi_cli, "PI_CLI", str(tmp_path / "pi-must-not-run"))
    monkeypatch.setattr(pi_cli, "ndjson_from_cli", fake_ndjson)
    return launched


@pytest.mark.asyncio
async def test_a_heavy_pi_agent_is_refused_while_the_child_sandbox_is_enabled(
    tmp_path, monkeypatch, pi_launches
):
    import lionhub.agent.child_sandbox as child_sandbox
    import lionhub.providers.pi.cli as pi_cli

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    request = pi_cli.PiCodeRequest(prompt="hi", repo=tmp_path)
    with pytest.raises(CLIInputRefusedError, match="while the child sandbox is enabled"):
        await _drain_pi(pi_cli, request)
    assert pi_launches == []


@pytest.mark.asyncio
async def test_a_heavy_pi_agent_runs_as_before_with_the_child_sandbox_disabled(
    tmp_path, pi_launches
):
    import lionhub.providers.pi.cli as pi_cli

    request = pi_cli.PiCodeRequest(prompt="hi", repo=tmp_path)
    await _drain_pi(pi_cli, request)
    assert len(pi_launches) == 1
    assert pi_launches[0][0] == str(tmp_path / "pi-must-not-run")


@pytest.mark.asyncio
async def test_a_pi_agent_is_not_started_when_the_sandbox_settings_cannot_be_read(
    tmp_path, monkeypatch, pi_launches
):
    import lionhub.agent.child_sandbox as child_sandbox
    import lionhub.providers.pi.cli as pi_cli

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": "on"})
    request = pi_cli.PiCodeRequest(prompt="hi", repo=tmp_path)
    with pytest.raises(ConfigurationError):
        await _drain_pi(pi_cli, request)
    assert pi_launches == []
