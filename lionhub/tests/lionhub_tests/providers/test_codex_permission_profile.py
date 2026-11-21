# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""While the child sandbox is on, a codex child runs under a command-line permission profile.

This file pins four things: the command line with the child sandbox off is exactly the one a
codex child always had; with it on there is no sandbox mode on the command line and the profile
grants the roots the sandbox mode granted; a launch refuses, before it spawns, anything that
could set the sandbox a second time or a codex binary not shown to enforce the profile; and the
profile denies the CODEX_HOME of the child's final environment."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.agent.child_sandbox import (
    DEFAULT_DENY_EDIT,
    _probe_record_dir_entry,
    default_allow_write,
    resolve_child_sandbox,
)
from lionhub.providers.openai import codex as codex_module
from lionhub.providers.openai import codex_sandbox_probe as probe_module
from lionhub.providers.openai._codex_permissions import (
    PERMISSION_PROFILE,
    PROBE_COMMAND,
    REQUIRED_ARMS,
    deny_entries,
    permission_profile_table,
)
from lionhub.providers.openai._codex_profile import codex_home_for_env
from lionhub.providers.openai.codex import CodexCodeRequest, toml_override_value


def _toml_loads(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the declared `tomli` reads the same TOML
        import tomli as tomllib

    return tomllib.loads(text)


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def _home(tmp_path: Path, text: str = "") -> dict[str, str]:
    """An environment whose CODEX_HOME holds a user config with this text."""
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text)
    return {"CODEX_HOME": str(home)}


def _codex(cwd: Path, env: dict[str, str], **kw) -> list[str]:
    return CodexCodeRequest(prompt="hi", repo=cwd, env=env, **kw).as_cmd_args()


def _default_roots() -> list[str]:
    return [os.path.expanduser(path) for path in default_allow_write()]


def _profile_table(args: list[str]) -> dict:
    """The permission profile table a command line carries, parsed back from its override."""
    rows = [o for o in _pairs(args, "-c") if o.startswith(f"permissions.{PERMISSION_PROFILE}=")]
    assert len(rows) == 1
    return _toml_loads("table = " + rows[0].split("=", 1)[1])["table"]


def _expected_profile(env, *, write, light=False, read_only=False, network=False) -> dict:
    """The table a launch builds for these inputs, rendered and parsed as a command line carries it."""
    policy = resolve_child_sandbox(None)
    table = permission_profile_table(
        deny=deny_entries(policy.deny_read, codex_home_for_env(env), env),
        write=write,
        light=light,
        read_only=read_only,
        network=network,
    )
    return _toml_loads("table = " + toml_override_value(table))["table"]


@pytest.fixture
def sandbox_on(monkeypatch):
    """The child sandbox on with the default policy (the suite switches it off by default)."""
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


@pytest.fixture
def sandbox_off(monkeypatch):
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )


@pytest.fixture
def record_dir(tmp_path, monkeypatch):
    """Probe records written under the test's own directory, never the real one."""
    import lionagi._paths as paths

    home = tmp_path / "lionagi-home"
    monkeypatch.setattr(paths, "LIONAGI_HOME", home)
    return home / "codex-sandbox-probes"


def _fake_binary(directory: Path, version: str = "0.162.1") -> Path:
    """An executable that only answers ``--version``, like a codex binary does for the lookup."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "codex"
    path.write_text(f'#!/bin/sh\necho "codex-cli {version}"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _passing_arms() -> dict[str, dict]:
    return {name: {"passed": True, "evidence": "rc=0 ok"} for name in REQUIRED_ARMS}


# --------------------------------------------------------------------------- sandbox off: pinned

_PREFIX = ["exec", "--json", "-m", "gpt-5.3-codex", "--skip-git-repo-check"]
_NO_APPS = ["--disable", "apps", "--disable", "plugins"]
_NO_SEARCH = ["--disable", "tool_search"]
# The test's CODEX_HOME holds an empty user config, so no carried user setting is on the line.
_LIGHT_PINS = [
    "-c",
    "sandbox_workspace_write.writable_roots=[]",
    "-c",
    "sandbox_workspace_write.network_access=false",
    "-c",
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "-c",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
]
_CWD = ["-C", "{cwd}", "--", "-"]

# name -> (request fields, the command line with the child sandbox off; {cwd} is the repo)
_SANDBOX_OFF_ARGV = {
    "light": (
        {"workload": "light"},
        [*_PREFIX, *_NO_APPS, "--sandbox", "workspace-write", *_NO_SEARCH] + [*_LIGHT_PINS, *_CWD],
    ),
    "light_read_only": (
        {"workload": "light", "sandbox": "read-only"},
        [*_PREFIX, *_NO_APPS, "--sandbox", "read-only", *_NO_SEARCH] + [*_LIGHT_PINS, *_CWD],
    ),
    "heavy": ({}, [*_PREFIX, *_NO_SEARCH, *_CWD]),
    "heavy_workspace_write": (
        {"sandbox": "workspace-write"},
        [*_PREFIX, "-s", "workspace-write", *_NO_SEARCH, *_CWD],
    ),
    "full_auto": (
        {"full_auto": True},
        [*_PREFIX, "--sandbox", "workspace-write", *_NO_SEARCH, *_CWD],
    ),
    "read_only": (
        {"sandbox": "read-only"},
        [*_PREFIX, "-s", "read-only", *_NO_SEARCH, *_CWD],
    ),
    "ask_never_read_only": (
        {"sandbox": "read-only", "ask_for_approval": "never"},
        [*_PREFIX, "-c", 'approval_policy="never"', "-s", "read-only"] + [*_NO_SEARCH, *_CWD],
    ),
}


@pytest.mark.parametrize("name", sorted(_SANDBOX_OFF_ARGV))
def test_with_the_child_sandbox_off_the_command_line_is_the_one_it_always_was(
    tmp_path, sandbox_off, name
):
    kw, expected = _SANDBOX_OFF_ARGV[name]
    actual = _codex(tmp_path, _home(tmp_path), **kw)
    cwd = actual[actual.index("-C") + 1]
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path)
    assert actual == [cwd if arg == "{cwd}" else arg for arg in expected]


# ----------------------------------------------------------------- sandbox on: the profile form

_PROFILED = {
    "light": {"workload": "light"},
    "light_read_only": {"workload": "light", "sandbox": "read-only"},
    "heavy": {},
    "heavy_workspace_write": {"sandbox": "workspace-write"},
    "full_auto": {"full_auto": True},
    "read_only": {"sandbox": "read-only"},
    "ask_never_read_only": {"sandbox": "read-only", "ask_for_approval": "never"},
}


@pytest.mark.parametrize("name", sorted(_PROFILED))
def test_with_the_child_sandbox_on_there_is_no_sandbox_mode_on_the_command_line(
    tmp_path, sandbox_on, passing_binary, name
):
    kw = _PROFILED[name]
    env = _home(tmp_path)
    args = _codex(tmp_path, env, **kw)
    assert "--sandbox" not in args
    assert "-s" not in args
    overrides = _pairs(args, "-c")
    # The legacy pins are not emitted: under a profile codex ignores them.
    assert not [o for o in overrides if o.startswith("sandbox_workspace_write.")]
    # Codex keeps the last `-c` of a key, so the profile comes after everything else.
    assert overrides[-2] == f'default_permissions="{PERMISSION_PROFILE}"'
    assert overrides[-1].startswith(f"permissions.{PERMISSION_PROFILE}=")
    if kw.get("ask_for_approval"):
        assert 'approval_policy="never"' in overrides[:-2]

    light = kw.get("workload") == "light"
    # A heavy request that names no sandbox takes the mode codex would have used without one;
    # the test's user config sets none, so that is read-only.
    names_no_sandbox = not light and not kw.get("sandbox") and not kw.get("full_auto")
    read_only = kw.get("sandbox") == "read-only" or names_no_sandbox
    table = _profile_table(args)
    assert table == _expected_profile(
        env, write=[] if light or read_only else _default_roots(), light=light, read_only=read_only
    )
    assert table["extends"] == (":read-only" if read_only else ":workspace")
    # The test's user config sets no `network_access`, so the network is off for every request.
    assert table["network"]["enabled"] is False
    if light and not read_only:
        assert table["filesystem"][":tmpdir"] == "read"
        assert table["filesystem"][":slash_tmp"] == "read"
    if not light:
        assert ":tmpdir" not in table["filesystem"]
        assert ":slash_tmp" not in table["filesystem"]


def _linked_worktree(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    main.mkdir()
    for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *cmd], cwd=main, check=True
        )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    return wt


def test_a_heavy_profile_writes_exactly_the_roots_the_sandbox_mode_granted(
    tmp_path, sandbox_on, passing_binary
):
    wt = _linked_worktree(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    env = _home(tmp_path)
    args = _codex(wt, env, full_auto=True, add_dir=[str(extra)])
    # `--add-dir` is honoured under a profile, so the directories it names stay as they were.
    added = _pairs(args, "--add-dir")
    assert str(extra) in added
    assert len(added) > 1  # the linked worktree's git directories follow the caller's
    legacy = list(dict.fromkeys([*_default_roots(), *added]))
    assert _profile_table(args) == _expected_profile(env, write=legacy)
    assert set(added) <= set(_profile_table(args)["filesystem"])


def test_a_light_profile_writes_nothing_beyond_the_working_directory(
    tmp_path, sandbox_on, passing_binary
):
    wt = _linked_worktree(tmp_path)
    env = _home(tmp_path)
    args = _codex(wt, env, workload="light")
    assert "--add-dir" not in args
    assert _profile_table(args) == _expected_profile(env, write=[], light=True)


# ------------------------------------------------------------------- refusals before the spawn


def _gate(cwd: Path, env: dict[str, str], **kw):
    """Run the checks a profiled launch makes before it spawns."""
    request = CodexCodeRequest(prompt="hi", repo=cwd, env=env, **kw)
    plain = codex_module._plain_request(request)
    return codex_module._gate_a_profiled_launch(plain, Path(cwd), env)


@pytest.fixture
def passing_binary(tmp_path, record_dir, monkeypatch):
    """A codex binary with a passing probe record, as the launch's ``CODEX_CLI``."""
    binary = _fake_binary(tmp_path / "bin")
    record = probe_module.write_record(str(binary), _passing_arms())
    monkeypatch.setattr(codex_module, "CODEX_CLI", str(binary))
    return binary, record


@pytest.fixture
def codex_log():
    """The records the codex module's logger emits during a test."""

    class _Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    logger = codex_module.log
    handler = _Collect()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_a_launch_nothing_else_touches_passes_the_checks_quietly(
    tmp_path, sandbox_on, passing_binary, codex_log
):
    assert _gate(tmp_path, _home(tmp_path)) is None
    assert not [r for r in codex_log if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"sandbox_mode": "workspace-write"}, "sandbox_mode"),
        ({"default_permissions": "wide"}, "default_permissions"),
        ({"permissions.wide.network.enabled": True}, "permissions"),
    ],
)
def test_an_override_that_sets_the_sandbox_a_second_time_is_refused(
    tmp_path, sandbox_on, passing_binary, overrides, named
):
    with pytest.raises(CLIInputRefusedError, match=rf"nothing else may set its sandbox; .*{named}"):
        _gate(tmp_path, _home(tmp_path), config_overrides=overrides)


@pytest.mark.parametrize(
    ("config", "named"),
    [
        ('default_permissions = "wide"\n', "sets default_permissions"),
        (
            "[permissions.lionagi_child.network]\nenabled = true\n",
            r"defines \[permissions\.lionagi_child\]",
        ),
        ('sandbox_mode = "danger-full-access"\n', "danger-full-access"),
    ],
)
def test_a_user_config_that_could_weaken_the_profile_is_refused(
    tmp_path, sandbox_on, passing_binary, config, named
):
    with pytest.raises(CLIInputRefusedError, match=named) as refused:
        _gate(tmp_path, _home(tmp_path, config))
    assert "codex-home" in str(refused.value)


@pytest.mark.parametrize("below", ["", "sub/dir"])
def test_a_project_codex_config_is_refused(tmp_path, sandbox_on, passing_binary, below):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('model = "other"\n')
    cwd = repo / below
    cwd.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CLIInputRefusedError, match="codex would read the project config"):
        _gate(cwd, _home(tmp_path))


def test_a_codex_binary_with_no_probe_record_is_refused(
    tmp_path, sandbox_on, record_dir, monkeypatch
):
    monkeypatch.setattr(codex_module, "CODEX_CLI", str(_fake_binary(tmp_path / "bin")))
    with pytest.raises(CLIInputRefusedError, match="no probe record") as refused:
        _gate(tmp_path, _home(tmp_path))
    assert PROBE_COMMAND in str(refused.value)


def test_a_probe_record_for_another_version_is_no_record(tmp_path, sandbox_on, passing_binary):
    binary, _ = passing_binary
    _fake_binary(binary.parent, "0.163.0")  # the same path, upgraded since the probe ran
    with pytest.raises(CLIInputRefusedError, match="no probe record for codex 0.163.0") as refused:
        _gate(tmp_path, _home(tmp_path))
    assert PROBE_COMMAND in str(refused.value)


def test_a_probe_record_with_a_failing_arm_is_refused(
    tmp_path, sandbox_on, record_dir, monkeypatch
):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    arms["deny_canary"] = {"passed": False, "evidence": "canary was read"}
    probe_module.write_record(str(binary), arms)
    monkeypatch.setattr(codex_module, "CODEX_CLI", str(binary))
    with pytest.raises(CLIInputRefusedError, match="did not pass: deny_canary") as refused:
        _gate(tmp_path, _home(tmp_path))
    assert PROBE_COMMAND in str(refused.value)


def test_a_user_config_sandbox_mode_logs_one_line_naming_the_config_and_the_record(
    tmp_path, sandbox_on, passing_binary, codex_log
):
    _, record = passing_binary
    _gate(tmp_path, _home(tmp_path, 'sandbox_mode = "workspace-write"\n'))
    lines = [r.getMessage() for r in codex_log if r.levelno >= logging.WARNING]
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "permission profile governs over the sandbox_mode" in lines[0]
    assert os.path.join("codex-home", "config.toml") in lines[0]
    assert os.path.join("codex-sandbox-probes", Path(record).name) in lines[0]


# ----------------------------------------------------------------------- the child's own home


def test_argv_is_rendered_against_the_environment_the_child_really_gets(
    tmp_path, sandbox_on, passing_binary
):
    declared = tmp_path / "declared-home"
    final = tmp_path / "final-home"
    for home in (declared, final):
        home.mkdir()
        (home / "config.toml").write_text("")
    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, env={"CODEX_HOME": str(declared)}, sandbox="workspace-write"
    )
    plain = codex_module._plain_request(request)
    # The renderer `prepare_argv` calls once the secret fill has built the child's environment.
    args = codex_module._exclusive_cmd_args(request, plain, tmp_path, {"CODEX_HOME": str(final)})
    table = _profile_table(args)
    assert table == _expected_profile({"CODEX_HOME": str(final)}, write=_default_roots())
    assert table != _expected_profile({"CODEX_HOME": str(declared)}, write=_default_roots())
    row = [o for o in _pairs(args, "-c") if o.startswith(f"permissions.{PERMISSION_PROFILE}=")][0]
    assert str(final) in row
    assert str(declared) not in row


# ------------------------------------------------------------- the probe record cannot be forged


def test_the_probe_record_directory_is_denied_to_edit_by_the_assembled_policy(
    sandbox_on, record_dir
):
    entry = _probe_record_dir_entry()
    assert os.path.expanduser(entry) == str(record_dir)
    policy = resolve_child_sandbox(None)
    assert entry in policy.deny_edit
    # The documented constant keeps its meaning; the policy adds the entry where it is assembled.
    assert entry not in DEFAULT_DENY_EDIT


def test_the_probe_record_directory_is_written_with_a_tilde_under_the_home(tmp_path, monkeypatch):
    import lionagi._paths as paths

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / ".lionagi")
    assert _probe_record_dir_entry() == "~/.lionagi/codex-sandbox-probes"
    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path.parent / "elsewhere")
    assert _probe_record_dir_entry() == str(tmp_path.parent / "elsewhere" / "codex-sandbox-probes")


def test_the_probe_record_directory_is_a_deny_entry_of_the_profile(
    tmp_path, sandbox_on, record_dir, passing_binary
):
    env = _home(tmp_path)
    assert str(record_dir) in deny_entries([], codex_home_for_env(env), env)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, env=env, sandbox="workspace-write")
    plain = codex_module._plain_request(request)
    args = codex_module._exclusive_cmd_args(request, plain, tmp_path, env)
    row = [o for o in _pairs(args, "-c") if o.startswith(f"permissions.{PERMISSION_PROFILE}=")][0]
    assert str(record_dir) in row


def test_the_probe_record_directory_is_denied_where_a_link_on_its_path_resolves(
    tmp_path, sandbox_on, monkeypatch
):
    import lionagi._paths as paths

    real = tmp_path / "real-home"
    (real / "codex-sandbox-probes").mkdir(parents=True)
    link = tmp_path / "link-home"
    link.symlink_to(real)
    monkeypatch.setattr(paths, "LIONAGI_HOME", link)
    resolved = os.path.realpath(real / "codex-sandbox-probes")
    # The policy's edit denial names the directory as written and where the link resolves.
    edit = [os.path.expanduser(entry) for entry in resolve_child_sandbox(None).deny_edit]
    assert str(link / "codex-sandbox-probes") in edit
    assert resolved in edit
    env = _home(tmp_path)
    assert resolved in deny_entries([], codex_home_for_env(env), env)


def test_the_launch_looks_the_binary_up_on_the_path_of_the_childs_final_environment(
    tmp_path, sandbox_on, record_dir, monkeypatch
):
    probed = _fake_binary(tmp_path / "probed")
    other = _fake_binary(tmp_path / "other")
    probe_module.write_record(str(probed), _passing_arms())
    monkeypatch.setattr(codex_module, "CODEX_CLI", "codex")
    # The parent's PATH finds the unprobed binary first; the child's environment decides.
    monkeypatch.setenv("PATH", f"{other.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    home = _home(tmp_path)
    assert _gate(tmp_path, {**home, "PATH": str(probed.parent)}) is None
    with pytest.raises(CLIInputRefusedError, match="no probe record"):
        _gate(tmp_path, {**home, "PATH": str(other.parent)})
