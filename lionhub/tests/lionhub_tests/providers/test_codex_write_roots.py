# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Under the child sandbox a Codex child runs under a permission profile whose write entries are
the set the sandbox mode it replaces granted: nothing for a read-only child, the working directory
alone for a light one, and for a heavy one the child sandbox's roots. A heavy child that names no
sandbox gets the mode Codex would have used without one, and read-only when no user config layer
sets it. A deny entry that begins with `~` is placed against the child's own home."""

from __future__ import annotations

import os

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai.codex import CodexCodeRequest, toml_override_value


@pytest.fixture(autouse=True)
def _child_sandbox_on(monkeypatch):
    """The child sandbox on with its default policy, and no host probe lookup."""
    import lionhub.agent.child_sandbox as child_sandbox
    import lionhub.providers.openai.codex as codex_module

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    monkeypatch.setattr(
        codex_module,
        "require_passing_probe_record",
        lambda binary, env=None, cwd=None: "probe-record",
    )


def _codex(tmp_path, **kw) -> list[str]:
    return CodexCodeRequest(prompt="hi", repo=tmp_path, **kw).as_cmd_args()


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def _project(tmp_path):
    """A working directory that is neither the home directory nor an ancestor of one."""
    project = tmp_path / "project"
    project.mkdir()
    return project


def _config_home(tmp_path, *lines: str) -> dict[str, str]:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("".join(f"{line}\n" for line in lines))
    return {"CODEX_HOME": str(home)}


def _toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the declared `tomli` reads the same TOML
        import tomli as tomllib

    return tomllib.loads(text)


def _table(args: list[str]) -> dict:
    """The permission profile table a command line carries, parsed back from its override."""
    rows = [o for o in _pairs(args, "-c") if o.startswith("permissions.lionagi_child=")]
    assert len(rows) == 1
    return _toml("table = " + rows[0].split("=", 1)[1])["table"]


def _expected(
    env,
    *,
    write,
    light=False,
    read_only=False,
    exclude_slash_tmp=False,
    exclude_tmpdir=False,
    network=False,
) -> dict:
    """The table the builder gives these inputs, rendered and parsed as a command line carries it."""
    from lionhub.agent.child_sandbox import resolve_child_sandbox
    from lionhub.providers.openai._codex_permissions import (
        deny_entries,
        permission_profile_table,
    )
    from lionhub.providers.openai._codex_profile import codex_home_for_env

    policy = resolve_child_sandbox(None)
    table = permission_profile_table(
        deny=deny_entries(policy.deny_read, codex_home_for_env(env), env),
        write=write,
        light=light,
        read_only=read_only,
        network=network,
        exclude_slash_tmp=exclude_slash_tmp,
        exclude_tmpdir=exclude_tmpdir,
    )
    return _toml("table = " + toml_override_value(table))["table"]


def _legacy_roots() -> list[str]:
    """The writable roots the workspace-write mode gave a heavy child: the child sandbox's list."""
    from lionhub.agent.child_sandbox import default_allow_write

    return [os.path.expanduser(path) for path in default_allow_write()]


def _legacy(workload: str, mode: str | None) -> dict:
    """What the old mode granted: a light child wrote its working directory and nothing else; a
    heavy one wrote the sandbox's roots under workspace-write and nothing under read-only (which
    is what Codex gave a run whose config set no mode)."""
    if workload == "light":
        return {"write": [], "light": True, "read_only": False}
    if mode == "workspace-write":
        return {"write": _legacy_roots(), "light": False, "read_only": False}
    return {"write": [], "light": False, "read_only": True}


@pytest.mark.parametrize("user_mode", [None, "read-only", "workspace-write"])
@pytest.mark.parametrize("workload", ["light", "heavy"])
def test_the_table_grants_the_write_set_the_old_mode_granted(tmp_path, workload, user_mode):
    lines = [f'sandbox_mode = "{user_mode}"'] if user_mode else []
    env = _config_home(tmp_path, *lines)
    args = _codex(tmp_path, workload=workload, env=env)
    assert _table(args) == _expected(env, **_legacy(workload, user_mode))


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_a_heavy_request_that_names_its_sandbox_gets_that_write_set(tmp_path, sandbox):
    env = _config_home(tmp_path)
    args = _codex(tmp_path, env=env, sandbox=sandbox)
    assert _table(args) == _expected(env, **_legacy("heavy", sandbox))


def test_a_heavy_request_naming_no_sandbox_is_read_only_when_no_config_sets_a_mode(tmp_path):
    env = _config_home(tmp_path)
    table = _table(_codex(tmp_path, env=env))
    assert table == _expected(env, write=[], read_only=True)
    assert table != _expected(env, write=_legacy_roots())


def test_full_auto_is_workspace_write_whatever_mode_the_user_config_sets(tmp_path):
    env = _config_home(tmp_path, 'sandbox_mode = "read-only"')
    table = _table(_codex(tmp_path, env=env, full_auto=True))
    assert table == _expected(env, write=_legacy_roots())


@pytest.mark.parametrize("key", ["exclude_slash_tmp", "exclude_tmpdir_env_var"])
def test_a_heavy_profile_honours_the_temp_directory_exclusions(tmp_path, key):
    env = _config_home(
        tmp_path,
        'sandbox_mode = "workspace-write"',
        "",
        "[sandbox_workspace_write]",
        f"{key} = true",
    )
    flags = {
        "exclude_slash_tmp": key == "exclude_slash_tmp",
        "exclude_tmpdir": key == "exclude_tmpdir_env_var",
    }
    table = _table(_codex(tmp_path, env=env))
    assert table == _expected(env, write=_legacy_roots(), **flags)
    assert table != _expected(env, write=_legacy_roots())


@pytest.mark.parametrize("granted", [True, False])
def test_a_heavy_profile_takes_the_network_setting_the_user_config_gives(tmp_path, granted):
    env = _config_home(
        tmp_path,
        'sandbox_mode = "workspace-write"',
        "",
        "[sandbox_workspace_write]",
        f"network_access = {str(granted).lower()}",
    )
    table = _table(_codex(tmp_path, env=env))
    assert table["network"] == {"enabled": granted}
    assert table == _expected(env, write=_legacy_roots(), network=granted)


def test_a_heavy_profile_has_the_network_off_when_no_config_sets_it(tmp_path):
    env = _config_home(tmp_path, 'sandbox_mode = "workspace-write"')
    assert _table(_codex(tmp_path, env=env))["network"] == {"enabled": False}


@pytest.mark.parametrize(
    ("kw", "mode"),
    [
        ({"workload": "light"}, "workspace-write"),
        ({}, "read-only"),
        ({"sandbox": "read-only"}, "workspace-write"),
    ],
)
def test_the_network_stays_off_for_light_and_read_only_profiles_whatever_the_config_says(
    tmp_path, kw, mode
):
    env = _config_home(
        tmp_path,
        f'sandbox_mode = "{mode}"',
        "",
        "[sandbox_workspace_write]",
        "network_access = true",
    )
    assert _table(_codex(tmp_path, env=env, **kw))["network"] == {"enabled": False}


def test_settings_carried_from_an_ignored_user_config_are_in_the_table_not_pinned(tmp_path):
    env = _config_home(
        tmp_path,
        'sandbox_mode = "workspace-write"',
        "",
        "[sandbox_workspace_write]",
        "exclude_slash_tmp = true",
    )
    args = _codex(tmp_path, env=env, ignore_user_config=True)
    assert not [o for o in _pairs(args, "-c") if o.startswith("sandbox_workspace_write")]
    assert _table(args) == _expected(env, write=_legacy_roots(), exclude_slash_tmp=True)


def test_a_tilde_in_a_denied_location_is_placed_against_the_child_home(tmp_path, monkeypatch):
    from lionhub.agent.child_sandbox import resolve_child_sandbox

    parent, child = tmp_path / "parent-home", tmp_path / "child-home"
    parent.mkdir()
    child.mkdir()
    monkeypatch.setenv("HOME", str(parent))
    entries = [e for e in resolve_child_sandbox(None).deny_read if e.startswith("~/")]
    assert entries
    env = {**_config_home(tmp_path), "HOME": str(child)}
    rendered = "\n".join(_pairs(_codex(_project(tmp_path), env=env), "-c"))
    for entry in entries:
        assert str(child / entry[2:]) in rendered
        assert str(parent / entry[2:]) not in rendered


def test_a_tilde_in_a_denied_location_is_also_placed_against_the_account_home(tmp_path):
    import pwd

    from lionhub.agent.child_sandbox import resolve_child_sandbox

    account = pwd.getpwuid(os.getuid()).pw_dir
    child = tmp_path / "child-home"
    child.mkdir()
    assert str(child) != account
    entries = [e for e in resolve_child_sandbox(None).deny_read if e.startswith("~/")]
    assert entries
    env = {**_config_home(tmp_path), "HOME": str(child)}
    denied = _table(_codex(_project(tmp_path), env=env))["filesystem"]
    for entry in entries:
        # A child given its own HOME still reaches the account's home by its absolute path.
        assert denied.get(str(child / entry[2:])) == "deny", entry
        assert denied.get(os.path.join(account, entry[2:])) == "deny", entry


@pytest.mark.parametrize("roots", ["/opt/build", ["/opt/build"], ("/opt/build",)])
def test_caller_writable_roots_of_every_shape_are_write_entries_not_pins(tmp_path, roots):
    env = _config_home(tmp_path)
    args = _codex(
        _project(tmp_path),
        env=env,
        full_auto=True,
        config_overrides={"sandbox_workspace_write.writable_roots": roots},
    )
    # Codex ignores `sandbox_workspace_write.*` under a profile, so nothing is pinned that way.
    assert not [o for o in _pairs(args, "-c") if o.startswith("sandbox_workspace_write")]
    assert _table(args) == _expected(env, write=["/opt/build"])


def test_a_tilde_in_caller_writable_roots_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env = _config_home(tmp_path)
    args = _codex(
        _project(tmp_path),
        env=env,
        full_auto=True,
        config_overrides={"sandbox_workspace_write.writable_roots": ["~/cache"]},
    )
    assert _table(args) == _expected(env, write=[str(home / "cache")])


def test_writable_roots_set_in_the_selected_config_profile_are_write_entries(tmp_path):
    env = _config_home(tmp_path)
    args = _codex(
        _project(tmp_path),
        env=env,
        full_auto=True,
        profile="build",
        config_overrides={"profiles.build.sandbox_workspace_write.writable_roots": ["/opt/build"]},
    )
    assert not [o for o in _pairs(args, "-c") if "sandbox_workspace_write" in o]
    assert _table(args) == _expected(env, write=["/opt/build"])


# ------------------------------------------------- a write root cannot reach the probe record


@pytest.fixture
def lionagi_home(tmp_path, monkeypatch):
    """A lionagi home under the test's own directory; the probe records live below it."""
    import lionagi._paths as paths

    home = tmp_path / "lionagi-home"
    (home / "codex-sandbox-probes").mkdir(parents=True)
    monkeypatch.setattr(paths, "LIONAGI_HOME", home)
    return home


def _record_overlaps(home):
    """Roots that contain the record directory, are it, or lie inside it."""
    record = home / "codex-sandbox-probes"
    return {"above": home, "equal": record, "inside": record / "sub"}


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
def test_an_add_dir_that_overlaps_the_probe_record_directory_is_refused(
    tmp_path, lionagi_home, where
):
    root = _record_overlaps(lionagi_home)[where]
    with pytest.raises(CLIInputRefusedError, match="probe record directory"):
        _codex(_project(tmp_path), env=_config_home(tmp_path), full_auto=True, add_dir=[str(root)])


@pytest.mark.parametrize("kw", [{"sandbox": "read-only"}, {}])
def test_an_add_dir_on_the_probe_record_directory_is_refused_under_a_read_only_profile(
    tmp_path, lionagi_home, kw
):
    # A heavy request with no sandbox and no configured mode is read-only too.
    root = _record_overlaps(lionagi_home)["equal"]
    with pytest.raises(CLIInputRefusedError, match="probe record directory"):
        _codex(_project(tmp_path), env=_config_home(tmp_path), add_dir=[str(root)], **kw)


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
def test_a_caller_writable_root_that_overlaps_the_probe_record_directory_is_refused(
    tmp_path, lionagi_home, where
):
    root = _record_overlaps(lionagi_home)[where]
    with pytest.raises(CLIInputRefusedError, match="probe record directory"):
        _codex(
            _project(tmp_path),
            env=_config_home(tmp_path),
            full_auto=True,
            config_overrides={"sandbox_workspace_write.writable_roots": [str(root)]},
        )


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
def test_a_heavy_root_from_settings_that_overlaps_the_probe_record_directory_is_refused(
    tmp_path, lionagi_home, monkeypatch, where
):
    import lionhub.agent.child_sandbox as child_sandbox

    root = _record_overlaps(lionagi_home)[where]
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: {"child_sandbox": {"allow_write": [str(root)]}},
    )
    with pytest.raises(CLIInputRefusedError, match="probe record directory"):
        _codex(_project(tmp_path), env=_config_home(tmp_path), full_auto=True)


@pytest.mark.parametrize("kw", [{"full_auto": True}, {"workload": "light"}])
@pytest.mark.parametrize("where", ["above", "equal", "inside"])
def test_a_working_directory_that_overlaps_the_probe_record_directory_is_refused(
    tmp_path, lionagi_home, where, kw
):
    # Light and heavy both extend `:workspace`, which grants write over the working directory.
    cwd = _record_overlaps(lionagi_home)[where]
    cwd.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CLIInputRefusedError, match="the working directory .* probe record"):
        _codex(cwd, env=_config_home(tmp_path), **kw)


def test_a_read_only_launch_may_start_over_the_probe_record_directory(tmp_path, lionagi_home):
    # `:read-only` grants no write over the working directory, so there is nothing to forge with.
    args = _codex(lionagi_home, env=_config_home(tmp_path), sandbox="read-only")
    assert _table(args)["extends"] == ":read-only"


def test_a_link_to_the_probe_record_directory_does_not_hide_it(tmp_path, lionagi_home):
    alias = tmp_path / "alias"
    alias.symlink_to(lionagi_home / "codex-sandbox-probes")
    with pytest.raises(CLIInputRefusedError, match="probe record directory"):
        _codex(_project(tmp_path), env=_config_home(tmp_path), full_auto=True, add_dir=[str(alias)])


def test_a_root_beside_the_probe_record_directory_is_still_granted(tmp_path, lionagi_home):
    # The same parent and a name that begins with the record directory's: not an overlap.
    beside = lionagi_home / "codex-sandbox-probes-other"
    args = _codex(
        _project(tmp_path), env=_config_home(tmp_path), full_auto=True, add_dir=[str(beside)]
    )
    assert str(beside) in "\n".join(_pairs(args, "-c"))
