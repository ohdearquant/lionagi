# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The project layers a Codex run reads, mirrored from Codex's loader, and the refusal of a run
that ignores the user config when one of them declares ``mcp_servers``.

Every arm points the system and managed config paths at files under its own temp directory, so a
``/etc/codex`` on the machine running the suite decides nothing here. Arms that need a run with no
project root set a marker no ancestor of the temp directory carries, so a ``.git`` above the temp
directory cannot become the root either.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionagi.providers.openai import _codex_project_layers as project_layers
from lionagi.providers.openai import codex as codex_mod
from lionagi.providers.openai._codex_project_layers import (
    declaring_project_layer,
    mcp_servers_declaration_line,
)
from lionagi.providers.openai.codex import CodexCodeRequest

_DECLARING = '[mcp_servers.leak]\ncommand = "leak-mcp"\n'
_MARKER = ".layer-test-root"  # carried by no ancestor of a temp directory


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """The system and managed config paths, absent until an arm writes one."""
    etc = tmp_path / "etc-codex"
    etc.mkdir()
    system, managed = etc / "config.toml", etc / "managed_config.toml"
    monkeypatch.setattr(project_layers, "_MARKER_SOURCES", (system, managed))
    return system, managed


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    return {"CODEX_HOME": str(home), "HOME": str(tmp_path / "user-home")}


def _markers(path: Path, *markers: str) -> None:
    path.write_text(f"project_root_markers = {list(markers)!r}\n".replace("'", '"'), "utf-8")


def _declaring(directory: Path) -> Path:
    (directory / ".codex").mkdir(parents=True)
    path = directory / ".codex" / "config.toml"
    path.write_text(_DECLARING, encoding="utf-8")
    return path


def _git_root(directory: Path) -> None:
    (directory / ".git").mkdir(parents=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


# --- where the walk stops: project_root_markers, merged as Codex merges them -----------------


def test_markers_merge_system_then_override_then_managed_and_default_to_git(sources):
    system, managed = sources
    assert project_layers._root_markers() == [".git"]

    _markers(system, ".hg")
    assert project_layers._root_markers() == [".hg"]
    assert project_layers._root_markers([".svn"]) == [".svn"]  # a `-c` override beats the system

    _markers(managed, ".bzr")
    assert project_layers._root_markers([".svn"]) == [".bzr"]  # the managed config beats both

    _markers(managed)
    assert project_layers._root_markers([".svn"]) == []  # an empty list: no project root


def test_a_custom_marker_moves_the_top_of_the_walk(tmp_path, sources, env):
    """With `.git`, the root is `outer` and its declaring layer is read; with `.hg` set by the
    managed config, the root is `repo` and `outer` is above it."""
    _, managed = sources
    outer = tmp_path / "outer"
    _git_root(outer)
    config_file = _declaring(outer)
    repo = outer / "repo"
    (repo / ".hg").mkdir(parents=True)
    (repo / "work").mkdir()

    assert declaring_project_layer(repo / "work", env) == (config_file, 1)
    _markers(managed, ".hg")
    assert declaring_project_layer(repo / "work", env) is None
    # the run's `-c` override is read too, and loses to the managed config
    assert declaring_project_layer(repo / "work", env, [".git"]) is None
    managed.unlink()
    assert declaring_project_layer(repo / "work", env, [".hg"]) is None
    assert declaring_project_layer(repo / "work", env, [".git"]) == (config_file, 1)


def test_a_git_directory_without_head_is_not_a_root(tmp_path, sources, env):
    """A `.git` directory marks a root only with a `HEAD` in it, so the walk passes an empty
    one and stops at the repository above."""
    outer = tmp_path / "outer"
    _git_root(outer)
    config_file = _declaring(outer)
    inner = outer / "inner"
    (inner / ".git").mkdir(parents=True)

    assert declaring_project_layer(inner, env) == (config_file, 1)
    (inner / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert declaring_project_layer(inner, env) is None


def test_with_no_root_the_walk_is_the_directory_alone(tmp_path, sources, env):
    _, managed = sources
    _markers(managed, _MARKER)
    above = tmp_path / "above"
    _declaring(above)
    (above / "work").mkdir()

    assert declaring_project_layer(above / "work", env) is None
    assert declaring_project_layer(above, env) == (above / ".codex" / "config.toml", 1)


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("project_root_markers = [", "could not be parsed"),
        ('project_root_markers = ".git"\n', "not an array of strings"),
        ("project_root_markers = [1]\n", "not an array of strings"),
    ],
)
def test_a_marker_source_that_cannot_be_read_as_markers_refuses(
    tmp_path, sources, env, text, complaint
):
    """Codex itself stops on these; here the root is then unknown, and unknown refuses."""
    system, managed = sources
    for path in (system, managed):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=complaint) as refusal:
            declaring_project_layer(tmp_path, env)
        assert str(path) in str(refusal.value)
        path.unlink()
    with pytest.raises(ConfigurationError, match="-c project_root_markers"):
        declaring_project_layer(tmp_path, env, ".git")


def test_an_unreadable_marker_source_refuses(tmp_path, sources, env):
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    _, managed = sources
    _markers(managed, ".git")
    managed.chmod(0)
    try:
        with pytest.raises(ConfigurationError, match="could not be read"):
            declaring_project_layer(tmp_path, env)
    finally:
        managed.chmod(0o600)


# --- which `.codex` is CODEX_HOME --------------------------------------------------------------


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex", "./.codex"])
def test_a_codex_home_that_is_not_absolute_is_refused(tmp_path, sources, value):
    """Codex takes CODEX_HOME as given: no `~` expansion, and a relative value resolves against
    Codex's own process directory, not the one this check runs in."""
    with pytest.raises(ConfigurationError, match="CODEX_HOME") as refusal:
        declaring_project_layer(tmp_path, {"CODEX_HOME": value})
    assert repr(value) in str(refusal.value)


def test_a_home_that_is_not_absolute_is_refused_and_an_absent_one_is_the_password_entry(
    tmp_path, sources
):
    with pytest.raises(ConfigurationError, match="HOME 'relative' is not an absolute path"):
        declaring_project_layer(tmp_path, {"HOME": "relative"})
    assert project_layers._codex_home({}) == Path(pwd.getpwuid(os.getuid()).pw_dir) / ".codex"
    assert project_layers._codex_home({"HOME": str(tmp_path)}) == tmp_path / ".codex"


def test_a_project_codex_that_resolves_to_codex_home_is_skipped_as_codex_skips_it(
    tmp_path, sources
):
    """Codex compares the project `.codex` with CODEX_HOME as given and once both are canonical,
    so a `.codex` symlinked to the home is the user config, never a project layer."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(_DECLARING, encoding="utf-8")
    project = tmp_path / "project"
    _git_root(project)
    (project / ".codex").symlink_to(home, target_is_directory=True)
    (project / "work").mkdir()

    assert declaring_project_layer(project / "work", {"CODEX_HOME": str(home)}) is None
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(project / ".codex", target_is_directory=True)
    assert declaring_project_layer(project / "work", {"CODEX_HOME": str(linked_home)}) is None

    other = tmp_path / "other-home"
    other.mkdir()
    declared = declaring_project_layer(project / "work", {"CODEX_HOME": str(other)})
    assert declared == (project / ".codex" / "config.toml", 1)


# --- which directory Codex walks for `-C` --------------------------------------------------------


def test_under_a_symlinked_ancestor_the_walk_is_the_logical_path_as_codex_resolves_it(
    tmp_path, sources, env
):
    """`view/proj` links to `real/proj`. Codex keeps the logical path when an ancestor below the
    top level is a symlink, so it walks `view/proj/work` up to `view` and reads `view`'s layer;
    the physical path, `real/proj/work`, has no root and is the directory alone."""
    _, managed = sources
    _markers(managed, _MARKER)
    (tmp_path / "real" / "proj" / "work").mkdir(parents=True)
    view = tmp_path / "view"
    view.mkdir()
    (view / _MARKER).touch()
    config_file = _declaring(view)
    (view / "proj").symlink_to(tmp_path / "real" / "proj", target_is_directory=True)

    assert declaring_project_layer(view / "proj" / "work", env) == (config_file, 1)
    assert declaring_project_layer(tmp_path / "real" / "proj" / "work", env) is None
    # `..` collapses on the logical path, never through the link's target
    assert declaring_project_layer(view / "proj" / "work" / ".." / "work", env) == (config_file, 1)


def test_a_layer_that_exists_but_cannot_be_read_refuses(tmp_path, sources, env):
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    _git_root(tmp_path / "project")
    config_file = _declaring(tmp_path / "project")
    config_file.chmod(0)
    try:
        with pytest.raises(ConfigurationError, match="could not be read") as refusal:
            declaring_project_layer(tmp_path / "project", env)
    finally:
        config_file.chmod(0o600)
    assert str(config_file) in str(refusal.value)


# --- the check before every run ------------------------------------------------------------------


@pytest.fixture
def spawns(monkeypatch):
    started: list[list[str]] = []

    async def spawned(cmd, **_kwargs):
        started.append(cmd)
        return
        yield

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    return started


async def _run(request: CodexCodeRequest) -> None:
    async for _ in codex_mod._ndjson_from_cli(request):
        pass


@pytest.mark.asyncio
async def test_every_run_checks_again_so_a_layer_written_after_the_request_refuses(
    tmp_path, sources, env, spawns
):
    """Each `codex exec` reads its config afresh, so the check runs before each process starts:
    a declaring layer written between two runs of one request refuses the second, and no process
    starts for it."""
    _, managed = sources
    _markers(managed, _MARKER)
    project = tmp_path / "project"
    (project / "work").mkdir(parents=True)
    (project / _MARKER).touch()
    request = CodexCodeRequest(prompt="x", repo=project / "work", env=env, ignore_user_config=True)

    await _run(request)
    assert len(spawns) == 1
    config_file = _declaring(project)
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert len(spawns) == 1
    message = str(refusal.value)
    assert f"{config_file} (line 1)" in message
    assert "--ignore-user-config" in message


@pytest.mark.asyncio
async def test_a_run_that_reads_the_user_config_is_not_checked(tmp_path, sources, env, spawns):
    """Without `--ignore-user-config` the run made no promise about its servers to keep."""
    _git_root(tmp_path / "project")
    _declaring(tmp_path / "project")

    await _run(CodexCodeRequest(prompt="x", repo=tmp_path / "project", env=env))

    assert len(spawns) == 1


@pytest.mark.asyncio
async def test_the_run_check_reads_the_childs_codex_home_and_its_marker_override(
    tmp_path, sources, spawns
):
    """The check reads what the child reads: the CODEX_HOME of the request's own environment,
    and a `project_root_markers` set by the request's `-c` overrides."""
    outer = tmp_path / "outer"
    _git_root(outer)
    _declaring(outer)
    work = outer / "repo" / "work"
    work.mkdir(parents=True)
    (outer / "repo" / _MARKER).touch()
    other_home = tmp_path / "other-home"
    other_home.mkdir()

    def request(env, **overrides):
        return CodexCodeRequest(
            prompt="x", repo=work, env=env, ignore_user_config=True, config_overrides=overrides
        )

    with pytest.raises(ConfigurationError, match="mcp_servers"):
        await _run(request({"CODEX_HOME": str(other_home)}))
    await _run(request({"CODEX_HOME": str(outer / ".codex")}))  # the layer is the child's home
    await _run(request({"CODEX_HOME": str(other_home)}, project_root_markers=[_MARKER]))
    assert len(spawns) == 2


# --- the declaration scan ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("[mcp_servers]\n", 1),
        ('model = "x"\n\n[mcp_servers.leak]\ncommand = "y"\n', 3),
        ("[[mcp_servers.leak]]\n", 1),
        ('["mcp_servers".leak]\n', 1),
        ("[ mcp_servers ]\n", 1),
        ('mcp_servers = { leak = { command = "y" } }\n', 1),
        ('mcp_servers.leak.command = "y"\n', 1),
        ("'mcp_servers' = 1\n", 1),
        ("# [mcp_servers]\n[mcp_servers]\n", 2),
        # a parser decodes a basic key's escapes: these name the same table;
        # `\\x` is TOML 1.1, read too so a parser that takes it is not evaded
        ('"mcp\\u005fservers" = {}\n', 1),
        ('["mcp\\U0000005Fservers".leak]\n', 1),
        ('"mcp\\x5fservers".leak.command = "y"\n', 1),
        # a byte-order mark the parser refuses, so the line read decides; CRLF line ends
        ("﻿mcp_servers = {}\n", 1),
        ('model = "x"\r\n[mcp_servers]\r\n', 2),
        ("[mcp_servers] # the servers\n", 1),
        # a `[` line inside a multi-line string or array is not a header: the root key after it counts
        ('note = """\n[x]\n"""\nmcp_servers.leak.command = "leak-mcp"\n', 4),
        ("note = '''\n[x]\n'''\nmcp_servers = {}\n", 4),
        ('a = [\n  [1],\n]\nmcp_servers.x.command = "y"\n', 4),
        # text the parser refuses has no structure to trust: any table's key counts
        ("[tools]\nmcp_servers = 1\nbroken =\n", 2),
        # the line named is the declaration, never a line inside a string before it
        ('model = """\n[mcp_servers]\n"""\nmcp_servers = {}\n', 4),
        ('note = """\nmcp_servers = 1\n"""\n[mcp_servers.leak]\ncommand = "y"\n', 4),
        ('mcp_servers = """\nx\n"""\n', 1),
        # lines count at LF, as TOML counts them, whatever a string or comment holds
        ('a = "x y"\nmcp_servers = {}\n', 2),
        ("# a\u0085b\n[mcp_servers]\n", 2),
        ("# x\x0cy\nmcp_servers = {}\n", 2),
    ],
)
def test_mcp_servers_declaration_line_finds_every_top_level_form(text, line):
    assert mcp_servers_declaration_line(text) == line


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# [mcp_servers]\n",
        'model = "x"\n',
        "[tools]\nmcp_servers = 1\n",
        "[tools.mcp_servers]\n",
        "[mcp_serversx]\n",
        "mcp_serversx = 1\n",
        'foo = "[mcp_servers]"\n',
        "a.mcp_servers = 1\n",
        '"not\\"mcp_servers" = 1\n',
        "tools = { mcp_servers = 1 }\n",
        # inside a multi-line string, the line is the string's
        'note = """\nmcp_servers = 1\n[mcp_servers]\n"""\n',
    ],
)
def test_mcp_servers_declaration_line_ignores_other_keys(text):
    assert mcp_servers_declaration_line(text) is None
