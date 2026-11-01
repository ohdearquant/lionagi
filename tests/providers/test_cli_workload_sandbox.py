# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A ``light`` CLI agent is held to what its engine can enforce: codex in its
workspace-write sandbox with no network and no extra writable roots, agy under
``--sandbox``, Claude Code without its file-writing tools. A setting that would undo
that is refused, and Pi, which has no sandbox to pin, refuses ``light`` outright. A
``heavy`` agent's command line is the one it had before workloads existed."""

from __future__ import annotations

import subprocess

import pytest

from lionagi._errors import CLIInputRefusedError
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
    assert "--add-dir" not in args


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
    home = tmp_path / "codex-home"
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


def test_the_light_pins_come_after_every_caller_override(tmp_path):
    args = _codex(tmp_path, workload="light", config_overrides={"model_verbosity": "low"})
    overrides = _pairs(args, "-c")
    assert overrides[-2:] == [
        "sandbox_workspace_write.writable_roots=[]",
        "sandbox_workspace_write.network_access=false",
    ]
    assert overrides.index('model_verbosity="low"') < len(overrides) - 2


def test_the_light_pins_come_after_the_settings_carried_beside_ignore_user_config(tmp_path):
    env = _home_with_config(
        tmp_path,
        "[sandbox_workspace_write]\nnetwork_access = true\n"
        f"writable_roots = [{str(tmp_path / 'elsewhere')!r}]\n",
    )
    args = _codex(tmp_path, workload="light", ignore_user_config=True, env=env)
    assert "--ignore-user-config" in args
    sandbox = [o for o in _pairs(args, "-c") if o.startswith("sandbox_workspace_write.")]
    # The user config's grants are carried, and the pins still have the last word.
    assert "sandbox_workspace_write.network_access=true" in sandbox[:-2]
    assert sandbox[-2:] == [
        "sandbox_workspace_write.writable_roots=[]",
        "sandbox_workspace_write.network_access=false",
    ]


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
