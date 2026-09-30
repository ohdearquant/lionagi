"""`lion chat` reads its defaults from `~/.lion/config.toml`, then `<dir>/.lion/config.toml`, and a
flag on the line beats both; a key or a value the file cannot mean refuses; `lion config` prints each
value with where it came from."""

import re
import sys
from pathlib import Path

import pytest
from lion_cli.config import CHAT
from lion_cli.main import parser

cli = sys.modules["lion_cli.main"]  # the module: the package's `main` attribute is the function


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home, here = tmp_path / "home", tmp_path / "work"
    for d in (home, here):
        (d / ".lion").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # never the real home's file
    return home, here


@pytest.fixture
def run(monkeypatch):
    """`lion <argv>` up to the chat itself: the namespace `chat_main` would get."""
    seen = []

    async def fake(a):
        seen.append(a)

    monkeypatch.setattr(cli, "chat_main", fake)

    def go(*argv: str):
        cli.main(list(argv))
        return seen[-1]

    return go


def write(d, text: str):
    path = d / ".lion" / "config.toml"
    path.write_text(text)
    return path


def test_home_then_directory_then_flag_for_a_string_and_a_bool(dirs, run):
    home, here = dirs
    write(home, '[chat]\nmodel = "home/m"\nshell = true\nverbose = true\n')
    a = run("chat", "--dir", str(here))
    assert (a.model, a.shell, a.verbose) == ("home/m", True, True)
    write(here, '[chat]\nmodel = "dir/m"\nshell = false\n')
    a = run("chat", "--dir", str(here))
    assert (a.model, a.shell, a.verbose) == ("dir/m", False, True)  # verbose: still home's
    a = run("chat", "--dir", str(here), "--model", "flag/m", "--shell")
    assert (a.model, a.shell) == ("flag/m", True)


def test_a_flag_equal_to_the_built_in_default_still_beats_the_file(dirs, run):
    home, here = dirs
    write(here, '[chat]\nmodel = "dir/m"\nrounds = 50\n')
    built_in = parser().parse_args(["chat"])
    a = run("chat", "--dir", str(here), "--model", built_in.model, "--rounds", str(built_in.rounds))
    assert (a.model, a.rounds) == (built_in.model, built_in.rounds)
    a = run("chat", "--dir", str(here))
    assert (a.model, a.rounds) == ("dir/m", 50)


def test_no_file_leaves_the_parsers_own_defaults(dirs, run):
    home, here = dirs
    a = run("chat", "--dir", str(here))
    assert vars(a) == vars(parser().parse_args(["chat", "--dir", str(here)]))


@pytest.mark.parametrize(
    "text, says",
    [
        ("[chat]\nverbos = true\n", r"\[chat\] has no key 'verbos'; the keys are model, "),
        ('[chat]\ndir = "/tmp"\n', r"\[chat\] has no key 'dir'"),  # one run's own: not a default
        ('[chat]\ntools = ["Bash"]\n', r"\[chat\] has no key 'tools'"),  # the model has no tools of its own
        ("[caht]\nverbose = true\n", r"no table 'caht'; the file holds \[chat\]"),
        ("chat = 3\n", r"chat is a table"),
    ],
)
def test_an_unknown_key_refuses_naming_the_file_and_the_key(dirs, run, text, says):
    home, here = dirs
    path = write(here, text)
    with pytest.raises(SystemExit, match=rf"^{re.escape(str(path.resolve()))}: {says}"):
        run("chat", "--dir", str(here))


@pytest.mark.parametrize(
    "line, says",
    [
        ('rounds = "200"', r"rounds = '200' is not an integer"),
        ("rounds = true", r"rounds = True is not an integer"),  # a TOML bool is not an int
        ("verbose = 1", r"verbose = 1 is not true or false"),
        ("model = 3", r"model = 3 is not a string"),
        ('box = "docker"', r"box = 'docker' is not one of vm, daytona"),
    ],
)
def test_a_wrong_type_refuses_naming_the_file_and_the_key(dirs, run, line, says):
    home, here = dirs
    path = write(home, f"[chat]\n{line}\n")
    with pytest.raises(SystemExit, match=rf"^{re.escape(str(path.resolve()))}: \[chat\] {says}$"):
        run("chat", "--dir", str(here))


def test_a_file_that_is_not_toml_refuses_naming_the_file(dirs, run):
    home, here = dirs
    path = write(here, "[chat\n")
    with pytest.raises(SystemExit, match=rf"^{re.escape(str(path.resolve()))}: "):
        run("chat", "--dir", str(here))


def test_lion_config_prints_each_value_with_the_file_it_came_from(dirs, capsys):
    home, here = dirs
    h = write(home, '[chat]\nmodel = "home/m"\nverbose = true\n')
    d = write(here, '[chat]\nmodel = "dir/m"\nbox = "daytona"\n')
    cli.main(["config", "--dir", str(here)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "[chat]" and len(lines) == 1 + len(CHAT) + 1
    assert lines[-1] == (
        "# instructions: none; ~/.lion/CLAUDE.md, AGENTS.md, CLAUDE.md, .lion/CLAUDE.md are read"
    )
    rows = {line.removeprefix("# ").split(" ")[0].rstrip(":"): line for line in lines[1:]}
    assert rows["model"] == f'model = "dir/m"  # {d.resolve()}'
    assert rows["box"] == f'box = "daytona"  # {d.resolve()}'
    assert rows["verbose"] == f"verbose = true  # {h.resolve()}"
    assert rows["rounds"] == "rounds = 200  # built-in"
    assert rows["shell"] == "shell = false  # built-in"
    assert rows["image"] == "# image: unset  # built-in"


def test_lion_config_names_the_instruction_files_a_chat_composes_home_first(dirs, capsys):
    home, here = dirs
    (home / ".lion" / "CLAUDE.md").write_text("Answer in English.\n")
    (here / "AGENTS.md").write_text("Run the tests.\n")
    (here / "CLAUDE.md").write_text("   \n")  # holds no text: not composed, not named
    (here / ".lion" / "CLAUDE.md").write_text("Sign as the lab.\n")
    cli.main(["config", "--dir", str(here)])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("# instructions")]
    assert lines == [
        f"# instructions: {(home / '.lion' / 'CLAUDE.md').resolve()} (18 chars)",
        f"# instructions: {(here / 'AGENTS.md').resolve()} (14 chars)",
        f"# instructions: {(here / '.lion' / 'CLAUDE.md').resolve()} (16 chars)",
    ]


def test_khive_config_in_a_file_resolves_a_relative_path_beside_that_files_lion_and_the_flag_wins(dirs, run):
    home, here = dirs
    write(home, '[chat]\nkhive_config = "seats/a/config.toml"\n')
    a = run("chat", "--dir", str(here))
    assert a.khive_config == str(home / "seats" / "a" / "config.toml")  # relative to home, not the cwd
    write(here, '[chat]\nkhive_config = "/abs/khive.toml"\n')
    assert run("chat", "--dir", str(here)).khive_config == "/abs/khive.toml"
    assert run("chat", "--dir", str(here), "--khive-config", "x.toml").khive_config == "x.toml"
    write(here, '[chat]\nkhive_config = "../.khive/config.toml"\n')  # kept as written, opened as written
    assert run("chat", "--dir", str(here)).khive_config == str(here / ".." / ".khive" / "config.toml")


def test_a_relative_khive_config_through_a_link_names_the_file_the_filesystem_reaches(dirs, run, tmp_path):
    """`seat/../config.toml` with `seat` a link into another tree is that tree's file, never the
    `config.toml` beside `seat`: dropping `seat/..` by text would sign as another actor."""
    _, here = dirs
    named = tmp_path / "named"
    (named / "sub").mkdir(parents=True)
    (here / "seat").symlink_to(named / "sub", target_is_directory=True)
    (named / "config.toml").write_text('[actor]\nid = "named"\n')
    (here / "config.toml").write_text('[actor]\nid = "beside"\n')
    write(here, '[chat]\nkhive_config = "seat/../config.toml"\n')
    chosen = Path(run("chat", "--dir", str(here)).khive_config)
    assert chosen.resolve() == (named / "config.toml").resolve()
    assert chosen.read_text() == '[actor]\nid = "named"\n'


def test_the_chat_keys_are_the_chat_flags_less_the_ones_that_name_one_run():
    flags = set(vars(parser().parse_args(["chat"]))) - {"command"}
    assert set(CHAT) == flags - {"dir", "say", "log", "resume"}
