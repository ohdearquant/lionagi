# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The project layers a Codex run can read, and the refusal of a run that ignores the user config
when one of them declares ``mcp_servers``.

The walk reads every directory up to the filesystem root, so each arm first asserts that no
ancestor of its temp directory holds a declaring layer: one there would decide the arms that
expect none.
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


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    env = {"CODEX_HOME": str(home), "HOME": str(tmp_path / "user-home")}
    above = declaring_project_layer(tmp_path, env)
    assert above is None, f"an ancestor of the temp directory declares MCP servers: {above}"
    return env


def _declaring(directory: Path) -> Path:
    (directory / ".codex").mkdir(parents=True)
    path = directory / ".codex" / "config.toml"
    path.write_text(_DECLARING, encoding="utf-8")
    return path


def _git_root(directory: Path) -> None:
    (directory / ".git").mkdir(parents=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


# --- how far the walk goes -----------------------------------------------------------------------


def test_a_layer_above_the_project_root_refuses_too(tmp_path, env):
    """Codex stops at the root its `project_root_markers` pick, and an enterprise cloud config
    this check cannot read can set them, so a layer above the repository's own root counts."""
    outer = tmp_path / "outer"
    config_file = _declaring(outer)
    repo = outer / "repo"
    _git_root(repo)
    (repo / "work").mkdir()

    assert declaring_project_layer(repo / "work", env) == (config_file, 1, True)
    # the nearest declaring layer is the one named
    nearer = _declaring(repo)
    assert declaring_project_layer(repo / "work", env) == (nearer, 1, True)


def test_every_layer_is_listed_nearest_first(tmp_path, env):
    outer = tmp_path / "outer"
    (outer / ".codex").mkdir(parents=True)
    (outer / ".codex" / "config.toml").write_text('model = "a"\n', encoding="utf-8")
    inner = outer / "inner"
    (inner / ".codex").mkdir(parents=True)
    (inner / ".codex" / "config.toml").write_text('model = "b"\n', encoding="utf-8")

    listed = [(path, text) for path, text in project_layers.project_layers(inner, env)]

    assert listed[:2] == [
        (inner / ".codex" / "config.toml", 'model = "b"\n'),
        (outer / ".codex" / "config.toml", 'model = "a"\n'),
    ]


# --- which `.codex` is CODEX_HOME --------------------------------------------------------------


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex", "./.codex"])
def test_a_codex_home_that_is_not_absolute_is_refused(tmp_path, value):
    """A CODEX_HOME this check cannot resolve to one directory is refused: how codex itself
    reads either form makes no difference to that."""
    with pytest.raises(ConfigurationError, match="CODEX_HOME") as refusal:
        declaring_project_layer(tmp_path, {"CODEX_HOME": value})
    assert repr(value) in str(refusal.value)


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex"])
def test_a_project_layer_check_refuses_a_request_codex_home_that_is_not_absolute(tmp_path, value):
    """The project-layer reader takes the home from the request environment, so the same
    refusal applies before it reads any layer."""
    request = CodexCodeRequest(
        prompt="x", repo=tmp_path, env={"CODEX_HOME": value}, ignore_user_config=True
    )
    with pytest.raises(ConfigurationError, match="is not an absolute path") as refusal:
        codex_mod.refuse_declaring_project_layer(codex_mod._plain_request(request), tmp_path)
    assert repr(value) in str(refusal.value)


def test_a_home_that_is_not_absolute_is_refused_and_an_absent_one_is_the_password_entry(
    tmp_path,
):
    with pytest.raises(
        ConfigurationError,
        match="HOME 'relative' is not an absolute path: it names a different codex home "
        "depending on where it is resolved.",
    ):
        declaring_project_layer(tmp_path, {"HOME": "relative"})
    account = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".codex"
    assert project_layers._codex_home({}) == account
    # An empty HOME names no directory, so it reads as no HOME.
    assert project_layers._codex_home({"HOME": ""}) == account
    assert project_layers._codex_home({"HOME": str(tmp_path)}) == tmp_path / ".codex"


def test_the_project_layer_check_does_not_refuse_after_the_shared_check_has_passed(tmp_path):
    """The shared check is the one refusal for HOME: _codex_home calls it before choosing
    the variable, so an absolute HOME with no CODEX_HOME reaches the home it names."""
    assert project_layers._codex_home({"HOME": str(tmp_path)}) == tmp_path / ".codex"


def test_a_project_codex_that_resolves_to_codex_home_is_skipped_as_codex_skips_it(
    tmp_path,
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
    assert declared == (project / ".codex" / "config.toml", 1, True)


# --- which directory Codex walks for `-C` --------------------------------------------------------


def test_under_a_symlinked_ancestor_the_walk_is_the_logical_path_as_codex_resolves_it(
    tmp_path, env
):
    """`view/proj` links to `real/proj`. Codex keeps the logical path when an ancestor below the
    top level is a symlink, so it walks `view/proj/work` up to `view` and reads `view`'s layer;
    the physical path, `real/proj/work`, never passes `view`."""
    (tmp_path / "real" / "proj" / "work").mkdir(parents=True)
    view = tmp_path / "view"
    view.mkdir()
    config_file = _declaring(view)
    (view / "proj").symlink_to(tmp_path / "real" / "proj", target_is_directory=True)

    assert declaring_project_layer(view / "proj" / "work", env) == (config_file, 1, True)
    assert declaring_project_layer(tmp_path / "real" / "proj" / "work", env) is None
    # `..` collapses on the logical path, never through the link's target
    assert declaring_project_layer(view / "proj" / "work" / ".." / "work", env) == (
        config_file,
        1,
        True,
    )


def test_a_layer_that_exists_but_cannot_be_read_refuses(tmp_path, env):
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
    assert "Make it readable by this process as UTF-8 text" in str(refusal.value)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_a_layer_that_is_not_a_regular_file_refuses_without_reading_it(tmp_path, env, kind):
    """A FIFO would block the read until something writes to it, so the type is checked
    first; what a layer that is not a file would set is unknown either way."""
    (tmp_path / "project" / ".codex").mkdir(parents=True)
    config_file = tmp_path / "project" / ".codex" / "config.toml"
    if kind == "fifo":
        os.mkfifo(config_file)
    else:
        config_file.mkdir()

    with pytest.raises(ConfigurationError, match="not a regular file") as refusal:
        declaring_project_layer(tmp_path / "project", env)
    assert str(config_file) in str(refusal.value)
    assert "Replace it with a regular file or remove it" in str(refusal.value)


# --- the check before every run ------------------------------------------------------------------


@pytest.fixture
def spawns(monkeypatch):
    started: list[list[str]] = []

    async def spawned(cmd, **kwargs):
        prepare = kwargs.get("_prepare_argv")
        if prepare is not None:
            cmd = prepare(dict(kwargs.get("env") or os.environ))
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
    tmp_path, env, spawns
):
    """Each `codex exec` reads its config afresh, so the check runs before each process starts:
    a declaring layer written between two runs of one request refuses the second, and no process
    starts for it."""
    project = tmp_path / "project"
    (project / "work").mkdir(parents=True)
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
    assert "Remove the declaration from that file" in message


@pytest.mark.asyncio
async def test_a_run_that_reads_the_user_config_is_not_checked(tmp_path, env, spawns):
    """Without `--ignore-user-config` the run made no promise about its servers to keep."""
    _git_root(tmp_path / "project")
    _declaring(tmp_path / "project")

    await _run(CodexCodeRequest(prompt="x", repo=tmp_path / "project", env=env))

    assert len(spawns) == 1


@pytest.mark.asyncio
async def test_the_run_check_reads_the_childs_codex_home_and_no_marker_override_narrows_it(
    tmp_path, spawns
):
    """The check reads what the child reads: the CODEX_HOME of the request's own environment.
    A `project_root_markers` override of the run does not narrow it, since a managed config
    beats the override and an enterprise cloud config can move the root too."""
    outer = tmp_path / "outer"
    _declaring(outer)
    work = outer / "repo" / "work"
    _git_root(outer / "repo")
    work.mkdir(parents=True)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    assert declaring_project_layer(tmp_path, {"CODEX_HOME": str(other_home)}) is None

    def request(env, **overrides):
        return CodexCodeRequest(
            prompt="x", repo=work, env=env, ignore_user_config=True, config_overrides=overrides
        )

    with pytest.raises(ConfigurationError, match="mcp_servers"):
        await _run(request({"CODEX_HOME": str(other_home)}))
    with pytest.raises(ConfigurationError, match="mcp_servers"):
        await _run(request({"CODEX_HOME": str(other_home)}, project_root_markers=[".git"]))
    await _run(request({"CODEX_HOME": str(outer / ".codex")}))  # the layer is the child's home
    assert len(spawns) == 1


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
        # a code point past U+10FFFF names no key, and the parser refuses the file
        ('"mcp\\UFFFFFFFF" = 1\nmcp_servers = {}\n', 2),
        ('"\\U00110000" = 1\n[mcp_servers]\n', 2),
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
        # nor a line that closes a string, though removing it changes what the string holds
        ("x = '''\nmcp_servers.decoy = 1 '''\nmcp_servers.real = 2\ny = 1 # '''\n", 3),
        ('note = """\nmcp_servers = 1 """\n[mcp_servers]\n', 3),
        # lines count at LF, as TOML counts them, whatever a string or comment holds
        ('a = "x y"\nmcp_servers = {}\n', 2),
        ("# a\u0085b\n[mcp_servers]\n", 2),
        ("# x\x0cy\nmcp_servers = {}\n", 2),
    ],
)
def test_mcp_servers_declaration_line_finds_every_top_level_form(text, line):
    assert mcp_servers_declaration_line(text) == line


def test_the_line_search_stops_after_a_bounded_number_of_parses(monkeypatch):
    """Each line tried is a parse of the whole file, so a file with many declaration-shaped
    lines inside a string costs a bounded number of parses, not one per line; past the
    bound the first such line is named, and the parse still decides the refusal."""
    from lionagi.providers.openai import _codex_project_layers as layers

    parses = []
    loads = layers._tomllib.loads
    monkeypatch.setattr(
        layers._tomllib, "loads", lambda text: parses.append(1) or loads(text), raising=True
    )
    text = 'note = """\n' + "mcp_servers = 1\n" * 1000 + '"""\n[mcp_servers.leak]\ncommand = "y"\n'

    assert mcp_servers_declaration_line(text) == 2
    assert len(parses) == 1 + layers._LINES_TRIED
    # the control: with as many such lines as are tried, the declaration's own line is named
    parses.clear()
    few = 'note = """\n' + "mcp_servers = 1\n" * (layers._LINES_TRIED - 1) + '"""\n[mcp_servers]\n'
    assert mcp_servers_declaration_line(few) == layers._LINES_TRIED + 2


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
        '"mcp\\U00110000servers" = 1\n',
        # inside a multi-line string, the line is the string's
        'note = """\nmcp_servers = 1\n[mcp_servers]\n"""\n',
    ],
)
def test_mcp_servers_declaration_line_ignores_other_keys(text):
    assert mcp_servers_declaration_line(text) is None


def test_a_layer_nested_deeper_than_the_parser_follows_is_read_line_by_line(tmp_path, env):
    """A parse that runs out of stack is a parse refused: the layer is read line by line, so it
    refuses when a line declares `mcp_servers` and passes when none does."""
    _git_root(tmp_path / "project")
    (tmp_path / "project" / ".codex").mkdir()
    layer = tmp_path / "project" / ".codex" / "config.toml"
    deep = "note = " + "[" * 1500 + "]" * 1500 + "\n"
    layer.write_text(deep, encoding="utf-8")
    assert declaring_project_layer(tmp_path / "project", env) is None
    layer.write_text(deep + _DECLARING, encoding="utf-8")
    assert declaring_project_layer(tmp_path / "project", env) == (layer, 2, False)


def test_a_line_the_parse_does_not_show_declaring_is_named_as_one_that_reads_as_one(tmp_path, env):
    """Past the lines tried one by one, the first line that reads as a declaration is named, and
    the refusal says only that it reads as one: here it is text inside a string."""
    project = tmp_path / "project"
    _git_root(project)
    (project / ".codex").mkdir()
    layer = project / ".codex" / "config.toml"
    decoys = "mcp_servers.decoy = 1\n" * 9
    layer.write_text(f'note = """\n{decoys}"""\n{_DECLARING}', encoding="utf-8")
    assert declaring_project_layer(project, env) == (layer, 2, False)
    request = CodexCodeRequest(prompt="x", repo=project, env=env, ignore_user_config=True)
    with pytest.raises(ConfigurationError) as refusal:
        codex_mod.refuse_declaring_project_layer(codex_mod._plain_request(request), project)
    message = str(refusal.value)
    assert f"{layer} is a Codex project config in which line 2 reads as a `mcp_servers`" in message
    assert "(line 2)" not in message
    assert "line by line" not in message
    assert "where the file declares the key" in message
    assert "Remove the declaration from that file" in message


def test_a_line_that_closes_a_string_is_not_shown_declaring(tmp_path, env):
    """Removing a string's closing line changes what the parse reads, since the string then runs
    on; the line declaring the key is the one named as declaring it."""
    project = tmp_path / "project"
    _git_root(project)
    (project / ".codex").mkdir()
    layer = project / ".codex" / "config.toml"
    layer.write_text("x = '''\nmcp_servers.decoy = 1 '''\nmcp_servers.real = 2\n", encoding="utf-8")
    assert declaring_project_layer(project, env) == (layer, 3, True)
    request = CodexCodeRequest(prompt="x", repo=project, env=env, ignore_user_config=True)
    with pytest.raises(ConfigurationError) as refusal:
        codex_mod.refuse_declaring_project_layer(codex_mod._plain_request(request), project)
    message = str(refusal.value)
    assert f"{layer} (line 3) is a Codex project config declaring `mcp_servers`" in message
    assert "reads as" not in message


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ('x = """\nmcp_servers = 1 # """\nmcp_servers.real = 2\nz = 1 # """\n', 3),
        ("x = '''\nmcp_servers.a = 1 # '''\nmcp_servers.real = 2\nz = 1 # '''\n", 3),
        ('x = """\n[mcp_servers] # """\n[mcp_servers.real]\ncommand = "a" # """\n', 3),
        ('note = """\nmcp_servers = 1\n"""\nmcp_servers.x = nan\n', 4),
    ],
)
def test_a_string_line_that_parses_alone_is_not_shown_declaring(text, line):
    """A string's closing line can parse alone when a comment follows the close, and with a
    ``nan`` in the key every removal changes the parse; the line shown declaring is the first
    whose preceding text parses whole."""
    assert project_layers._declaration(text) == (line, True)


def test_a_line_whose_removal_exposes_a_nesting_too_deep_to_parse_is_named():
    """Removing a line can turn a string's text into TOML: when that text nests deeper than the
    parser can follow, the line carries structure the declaration needs, as with a parse refused."""
    text = 'mcp_servers = """\nnote = ' + "[" * 1500 + "]" * 1500 + '\n"""\n'

    assert mcp_servers_declaration_line(text) == 1
