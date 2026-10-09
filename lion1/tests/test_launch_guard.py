"""The suite refuses a test's launch of a khive binary or a provider CLI unless the test declares it, or the
program is a stand-in the test wrote under the session's temporary directory. The guard is the seam
tests/conftest.py takes on `subprocess.Popen._execute_child` as it is imported, bound to the running test by
the autouse fixture `_refuse_undeclared_cli_launches`. Each program here is a script named like the real one
that leaves a file behind when it runs, so a refusal shows as the file never appearing."""

import ast
import asyncio
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _program(directory: Path, name: str) -> tuple[Path, Path]:
    ran = directory / f"{name}.ran"
    program = directory / name
    program.write_text(f"#!/bin/sh\ntouch '{ran}'\n")
    program.chmod(0o755)
    return program, ran


@pytest.fixture
def elsewhere(tmp_path_factory):
    """A directory outside the session's temporary one: a program there is not one the suite wrote, so
    the guard takes it for the real thing."""
    d = Path(tempfile.mkdtemp(prefix="lion1-guard-"))
    assert not d.resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
    yield d
    shutil.rmtree(d)


@pytest.mark.parametrize("name", ["kkernel", "khive", "lion1-khive", "claude", "codex"])
def test_an_undeclared_launch_is_refused_before_the_program_starts(elsewhere, refused_launches, name):
    program, ran = _program(elsewhere, name)
    with pytest.raises(FileNotFoundError, match=f"launches_cli\\('{name}'\\)"):
        subprocess.run([str(program), "mcp"], check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])


def test_a_bare_name_found_on_path_is_refused(elsewhere, monkeypatch, refused_launches):
    _, ran = _program(elsewhere, "claude")
    monkeypatch.setenv("PATH", f"{elsewhere}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(FileNotFoundError):
        subprocess.run(["claude", "-p", "hello"], check=False)
    assert not ran.exists()
    refused_launches.expect(["claude"])


def test_a_bare_name_is_looked_up_on_the_path_the_child_gets(
    elsewhere, tmp_path, monkeypatch, refused_launches
):
    # the stand-in on the test's own PATH would start; the child's PATH names the other one
    _program(tmp_path, "codex")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    _, ran = _program(elsewhere, "codex")
    env = {**os.environ, "PATH": f"{elsewhere}{os.pathsep}{os.environ['PATH']}"}
    with pytest.raises(FileNotFoundError):
        subprocess.run(["codex"], env=env, check=False)
    assert not ran.exists()
    refused_launches.expect(["codex"])


def test_a_call_without_an_environment_starts_with_the_one_read(elsewhere, refused_launches):
    # with no `env` the child inherits the process's environment, which `os.putenv` changes without
    # `os.environ` seeing it: the copy of `os.environ` the reading reads is the one the child gets, so a
    # PATH set only through `os.putenv` leads the shell nowhere
    program, ran = _program(elsewhere, "codex")
    os.link(program, elsewhere / "helper")
    path = os.environ["PATH"]
    os.putenv("PATH", f"{elsewhere}{os.pathsep}/usr/bin{os.pathsep}/bin")
    try:
        done = subprocess.run("helper", shell=True)
    finally:
        os.putenv("PATH", path)
    assert done.returncode == 127 and refused_launches == []
    assert not ran.exists()


def test_a_program_on_a_path_only_os_putenv_set_does_not_start(elsewhere, refused_launches):
    # the copy is handed on for every call without an `env`, shell or not and guarded program or not:
    # a program the suite does not guard, found only on a PATH `os.putenv` set, is not found either, and
    # nothing is refused. A test sets the environment through `os.environ` or `monkeypatch.setenv`
    _, ran = _program(elsewhere, "putenv-only-helper")
    path = os.environ["PATH"]
    os.putenv("PATH", f"{elsewhere}{os.pathsep}/usr/bin{os.pathsep}/bin")
    try:
        shell = subprocess.run("putenv-only-helper", shell=True)
        argv = subprocess.run(["/usr/bin/env", "putenv-only-helper"])
    finally:
        os.putenv("PATH", path)
    assert (shell.returncode, argv.returncode) == (127, 127) and refused_launches == []
    assert not ran.exists()


def test_a_shell_command_is_refused_by_its_program_past_leading_assignments(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "kkernel")
    with pytest.raises(FileNotFoundError):
        subprocess.run(f"KHIVE_ACTOR=x '{program}' mcp", shell=True, check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])


def test_a_shell_commands_exec_or_command_builtin_hands_the_line_to_its_program(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "codex")
    texts = [f"exec '{program}'", f"command '{program}'", f"exec -a x '{program}'", f"X=1 exec '{program}'"]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(text, shell=True, check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)] * len(texts))


def test_a_shell_text_is_judged_by_every_word_of_it(elsewhere, refused_launches):
    # the guard reads words, not the shell's grammar: a guarded program inside `if`, past `&&` or a
    # pipe, under `exec -l`, or merely looked up with `command -v`, is refused
    program, ran = _program(elsewhere, "codex")
    texts = [
        f"if '{program}'; then :; fi",
        f"true && '{program}'",
        f"echo x | '{program}'",
        f"exec -l '{program}'",
        f"command -v '{program}'",
    ]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(text, shell=True, check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)] * len(texts))


def test_a_shell_operator_against_a_word_still_splits_it(elsewhere, refused_launches):
    # a bare name, so only the split can find it: `true&&codex` read as one word is no program
    program, ran = _program(elsewhere, "codex")
    env = {"PATH": f"{elsewhere}{os.pathsep}/usr/bin:/bin"}
    for text in ["true&&codex", "echo x|codex", "true;codex", "(codex)", "echo x>/dev/null;codex"]:
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(text, shell=True, env=env, check=False)
    assert not ran.exists()
    refused_launches.expect(["codex"] * 5)


def test_a_word_the_shell_would_expand_resolves_nothing_and_is_refused(elsewhere, refused_launches):
    # the shell would start the program from each of these; the guard cannot read which, so it refuses
    program, ran = _program(elsewhere, "codex")
    env = {**os.environ, "CLI": str(program), "HOME": str(elsewhere), "PATH": f"{elsewhere}:/usr/bin:/bin"}
    texts = [
        '"$CLI"',
        f'"$(echo {program})"',
        f'"`echo {program}`"',
        "co`printf dex`",
        "~/codex",
        "cod*x",
        "cod?x",
        "cod{e,}x",
        "code[x]",
    ]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env, check=False)
    assert not ran.exists()
    refused_launches.expect(texts)
    # inside quotes the shell expands none of these (`$` and a backtick inside double quotes only), and
    # a `~` or an escaped glob character inside a word is not an expansion, so these start; a glob the
    # quotes leave bare in part is one, and an escaped `$` or backtick is one too (inert on this read,
    # live on the next: `eval`, another shell)
    done = subprocess.run(
        "echo '*' '?' \"{a,b}\" '$CLI' '~' \"[x]\" a~b \\* '`x`'",
        shell=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.stdout == "* ? {a,b} $CLI ~ [x] a~b * `x`\n" and refused_launches == []
    for text in ("echo co'd'*x", "echo \\$CLI", "eval \\`printf codex\\`"):
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env)
    assert not ran.exists()
    refused_launches.expect(["echo co'd'*x", "echo \\$CLI", "eval \\`printf codex\\`"])


def test_a_text_the_scan_cannot_read_past_is_refused_whole(elsewhere, refused_launches):
    # past a comment, a here-document's body or an open quote the shell stops reading quotes, and this
    # scan would carry a quote state the shell does not, hiding a `"$CLI"` on the next line: each is
    # refused whole, before any word is looked up; the same `#` or `<<` inside quotes is an argument
    program, ran = _program(elsewhere, "codex")
    env = {**os.environ, "CLI": str(program)}
    texts = [
        '# \'\n"$CLI"',
        "# '\n\"$CLI\"\n# '",  # the quotes balance across two comment lines: only the `#` can say
        "cat <<EOF\n'$( \"$CLI\" )'\nEOF\n",
        "cat <<'END'\nx\nEND\n",
        "echo 'x",
        "echo x\\",
        "co\\\ndex",  # the shell deletes a backslash-newline before it reads the word: `codex`
        "\\\n~/codex",  # and the `~` past one begins a word
    ]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env, check=False)
    assert not ran.exists()
    refused_launches.expect(texts)
    done = subprocess.run("echo '#' \"<<\" a#b", shell=True, capture_output=True, text=True)
    assert done.stdout == "# << a#b\n" and refused_launches == []
    # an escaped or quoted `<` before a bare `<` is a `<` read from `<` (sh and bash print it), and a
    # bare `<` alone is a redirection: none is a here-document
    done = subprocess.run(
        "printf '%s' \\<< /dev/null; printf '%s' \"<\"< /dev/null; "
        "printf '%s' '<'< /dev/null; printf '<' < /dev/null",
        shell=True,
        capture_output=True,
        text=True,
    )
    assert done.stdout == "<<<<" and refused_launches == []
    # `<<<` is a here-string (bash), whose word is read like any other: the text starts, and a program
    # before it is judged by its name
    done = subprocess.run(
        "cat <<< here-string", shell=True, executable="/bin/bash", capture_output=True, text=True
    )
    assert done.stdout == "here-string\n" and refused_launches == []
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(f"'{program}' <<< input", shell=True, executable="/bin/bash", check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])
    with pytest.raises(
        FileNotFoundError, match="refused to start 'codex'"
    ):  # a quoted `<<`, then the program
        subprocess.run(f"echo \"<<'END'\"; '{program}'", shell=True, check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])


def test_a_comment_begins_after_any_word_boundary(elsewhere, refused_launches):
    # a `#` begins a comment after whitespace and after every operator that ends a word (`;`, `|`, `&`,
    # `(`, `)`, `<`, `>`): the quote it opens is not one, so the `"$CLI"` on the next line would be live
    program, ran = _program(elsewhere, "codex")
    env = {**os.environ, "CLI": str(program)}
    texts = [f"true{boundary}#'\n\"$CLI\" #'" for boundary in (";", "|", "&", "<", ">", "\t", "\n")]
    texts += ["(true)#'\n\"$CLI\" #'", "(#'\n\"$CLI\" #')"]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env, check=False)
    assert not ran.exists()
    refused_launches.expect(texts)
    # and a `~` after `<` or `>` is a redirection the shell expands under the home
    for text in ("printf x >~/out", "true <~/t"):
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env, check=False)
    refused_launches.expect(["printf x >~/out", "true <~/t"])
    # inside a word `#`, `!` and `}` are nothing, and a `~` after a `}` is not expanded: all print
    done = subprocess.run("printf '%s' 'x>~' x#y x!#y x}~ x}#y", shell=True, capture_output=True, text=True)
    assert done.stdout == "x>~x#yx!#yx}~x}#y" and refused_launches == []


def test_a_brace_group_is_not_a_brace_expansion(refused_launches):
    # `{ ...; }` is a group: a `{` followed by whitespace expands nothing (a brace expansion holds no
    # unquoted whitespace), so the group runs; `{a,b}` is still an expansion and is refused
    done = subprocess.run("{ printf '%s' grouped x{ y; }", shell=True, capture_output=True, text=True)
    assert done.stdout == "groupedx{y" and refused_launches == []
    with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
        subprocess.run("printf '%s' {a,b}", shell=True)
    refused_launches.expect(["printf '%s' {a,b}"])


def test_a_pathlike_program_is_judged_like_a_str_one(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([Path(program), "-p"])
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(Path(program))
    assert not ran.exists()
    refused_launches.expect([str(program)] * 2)


def test_a_hard_link_to_a_guarded_program_is_that_program_whatever_its_name(
    elsewhere, tmp_path, refused_launches
):
    # a hard link keeps none of the name and resolves to itself: the file says what it is, by the
    # guarded programs the child's PATH finds; even one placed under the session's directory is refused
    program, ran = _program(elsewhere, "codex")
    other, inside = elsewhere / "other-name", tmp_path / "stand-in-name"
    os.link(program, other)
    os.link(program, inside)
    env = {"PATH": f"{elsewhere}:/usr/bin:/bin"}
    for launch in ([str(other)], ["other-name"], [str(inside)]):
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(launch, env=env)
    assert not ran.exists()
    refused_launches.expect([str(other), "other-name", str(inside)])
    # and by the guarded programs the session's PATH finds, when the child's cannot
    with pytest.MonkeyPatch.context() as m:
        m.setenv("PATH", f"{elsewhere}:/usr/bin:/bin")
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run([str(other)], env={"PATH": "/usr/bin:/bin"})
    assert not ran.exists()
    refused_launches.expect([str(other)])
    # an empty entry on the session's PATH is the session's working directory
    with pytest.MonkeyPatch.context() as m:
        m.chdir(elsewhere)
        m.setenv("PATH", ":/usr/bin:/bin")
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run([str(other)], env={"PATH": "/usr/bin:/bin"})
    assert not ran.exists()
    refused_launches.expect([str(other)])
    # a test's own `shutil.which` does not say where the child's PATH leads: the lookup is the real one
    with pytest.MonkeyPatch.context() as m:
        m.setattr(shutil, "which", lambda *a, **k: None)
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(["other-name"], env=env)
    assert not ran.exists()
    refused_launches.expect(["other-name"])
    # the guarded programs are found on the child's PATH as the child searches it: a relative entry
    # from the child's working directory
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(["other-name"], cwd=elsewhere, env={"PATH": ".:/usr/bin:/bin"})
    assert not ran.exists()
    refused_launches.expect(["other-name"])


def test_a_path_relinked_between_the_reading_and_the_exec_starts_what_it_now_names(
    elsewhere, refused_launches, monkeypatch
):
    # ADR-0027 S4 pinned from outside: the guard reads a path and Popen then execs the name, so a relink
    # in between starts a file the reading never judged, here a guarded program the reading refuses
    program, ran = _program(elsewhere, "codex")
    helper, helper_ran = _program(elsewhere, "helper")
    swap = elsewhere / "swap"
    os.link(program, swap)
    env = {"PATH": f"{elsewhere}:/usr/bin:/bin"}
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([str(swap)], env=env)
    assert not ran.exists()
    refused_launches.expect([str(swap)])
    seam = subprocess.Popen._execute_child.__globals__
    execute_child = seam["_execute_child"]

    def relinked_after_the_reading(self, *args, **kwargs):
        os.replace(swap, helper)
        return execute_child(self, *args, **kwargs)

    monkeypatch.setitem(seam, "_execute_child", relinked_after_the_reading)
    subprocess.run([str(helper)], env=env, check=True)
    assert ran.exists() and not helper_ran.exists()


@pytest.mark.parametrize("carrier", ["child", "session"])
def test_a_stand_in_ahead_on_the_path_does_not_hide_the_guarded_program_behind_it(
    elsewhere, tmp_path, monkeypatch, refused_launches, carrier
):
    # every guarded program a PATH finds is read, not the first, on the child's PATH and the session's
    # alike: a stand-in the test wrote ahead of one would otherwise hide it, and a hard link to the one
    # behind would start
    stand_in, stand_in_ran = _program(tmp_path, "codex")
    program, ran = _program(elsewhere, "codex")
    alias = elsewhere / "alias-name"
    os.link(program, alias)
    ahead = f"{tmp_path}:{elsewhere}:/usr/bin:/bin"
    if carrier == "session":
        monkeypatch.setenv("PATH", ahead)
    env = {"PATH": ahead if carrier == "child" else "/usr/bin:/bin"}
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([str(alias)], env=env)
    assert not ran.exists()
    refused_launches.expect([str(alias)])
    subprocess.run([str(stand_in)], env=env, check=True)
    assert stand_in_ran.exists() and refused_launches == []


def test_a_test_that_swaps_the_seam_out_and_back_launches_unjudged_meanwhile(elsewhere, refused_launches):
    # ADR-0027 S8: the seam is checked at each test's setup and teardown, not at each launch, so a test
    # that puts back the function the guard wraps starts what it names until it restores the guard
    program, ran = _program(elsewhere, "codex")
    guarded = subprocess.Popen._execute_child
    subprocess.Popen._execute_child = guarded.__globals__["_execute_child"]
    try:
        subprocess.run([str(program)], check=True)
    finally:
        subprocess.Popen._execute_child = guarded
    assert ran.exists() and refused_launches == []


def test_a_wrapper_named_as_the_program_has_its_arguments_unread(elsewhere, refused_launches):
    # ADR-0027 S5: only the program a list launch names is judged, so `env` starts and what it is
    # handed starts unjudged
    program, ran = _program(elsewhere, "codex")
    subprocess.run(["/usr/bin/env", str(program)], check=True)
    assert ran.exists() and refused_launches == []


def test_a_link_the_test_wrote_to_a_guarded_program_elsewhere_is_that_program(
    elsewhere, tmp_path, refused_launches
):
    # what a PATH finds is judged by the file it resolves to, not where it was found: a link under the
    # session's directory to a guarded program elsewhere is no stand-in, so a hard link to that program
    # is refused though the link is the only way the PATH reaches it
    program, ran = _program(elsewhere, "codex")
    alias = elsewhere / "alias-name"
    os.link(program, alias)
    (tmp_path / "codex").symlink_to(program)
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([str(alias)], env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert not ran.exists()
    refused_launches.expect([str(alias)])


def test_a_relative_program_or_path_entry_resolves_where_the_child_starts(elsewhere, refused_launches):
    # an alias, so only the file's resolution can say what it is; resolved from the child's cwd
    program, ran = _program(elsewhere, "codex")
    (elsewhere / "local-name").symlink_to(program)
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(["./local-name"], cwd=elsewhere)
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(["local-name"], cwd=elsewhere, env={"PATH": "."})
    assert not ran.exists()
    refused_launches.expect(["./local-name", "local-name"])


def test_a_bytes_environment_is_read_like_a_str_one(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "codex")
    (elsewhere / "local-name").symlink_to(program)
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(["local-name"], env={b"PATH": os.fsencode(str(elsewhere))})
    assert not ran.exists()
    refused_launches.expect(["local-name"])


def test_an_asyncio_launch_is_refused(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "kkernel")

    async def launch() -> None:
        await asyncio.create_subprocess_exec(str(program), "mcp")

    with pytest.raises(FileNotFoundError):
        asyncio.run(launch())
    assert not ran.exists()
    refused_launches.expect([str(program)])


def test_a_shell_given_an_empty_sequence_starts_as_popen_starts_it(refused_launches):
    # an empty sequence is a shell given no text (`sh -c` alone), which names nothing to start: Popen's own
    # result stands, and a `preexec_fn` is still refused
    done = subprocess.run([], shell=True, capture_output=True)
    assert done.returncode != 0 and refused_launches == []
    with pytest.raises(FileNotFoundError, match="has a `preexec_fn`"):
        subprocess.run([], shell=True, preexec_fn=lambda: None)
    refused_launches.expect([""])


def test_a_call_with_a_preexec_fn_is_refused_whole(elsewhere, refused_launches):
    # it runs in the child after the guard's reading and before the program starts: a hard link made
    # then at a path that was empty would start a guarded program under a name the reading cleared
    program, ran = _program(elsewhere, "codex")
    other = elsewhere / "later-name"
    with pytest.raises(FileNotFoundError, match="has a `preexec_fn`"):
        subprocess.run([str(other)], preexec_fn=lambda: os.link(program, other))
    with pytest.raises(FileNotFoundError, match="has a `preexec_fn`"):
        subprocess.run(["/usr/bin/printf", "x"], preexec_fn=lambda: None)
    with pytest.raises(FileNotFoundError, match="has a `preexec_fn`"):
        subprocess.run("printf x", shell=True, preexec_fn=lambda: None)
    assert not ran.exists() and not other.exists()
    refused_launches.expect([str(other), "/usr/bin/printf", "printf x"])
    done = subprocess.run(["/usr/bin/printf", "x"], capture_output=True, text=True)
    assert done.stdout == "x" and refused_launches == []


@pytest.mark.parametrize(
    "option",
    [
        "close_fds",
        "pass_fds",
        "cwd",
        "env",
        "restore_signals",
        "start_new_session",
        "process_group",
        "umask",
        "user",
        "group",
        "extra_groups",
        "pipes",
    ],
)
def test_a_launch_is_refused_whatever_else_the_call_sets(elsewhere, refused_launches, option):
    # every other value Popen hands its child is read past, never branched on: a call that sets one
    # is judged by its program as a bare call is
    program, ran = _program(elsewhere, "codex")
    read_end, write_end = os.pipe()
    options = {
        "close_fds": {"close_fds": False},
        "pass_fds": {"pass_fds": (write_end,)},
        "cwd": {"cwd": elsewhere},
        "env": {"env": {"PATH": "/usr/bin:/bin"}},
        "restore_signals": {"restore_signals": False},
        "start_new_session": {"start_new_session": True},
        "process_group": {"process_group": 0},
        "umask": {"umask": 0o077},
        "user": {"user": os.getuid()},
        "group": {"group": os.getgid()},
        "extra_groups": {"extra_groups": [os.getgid()]},
        "pipes": {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE},
    }[option]
    try:
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run([str(program)], **options)
    finally:
        os.close(read_end)
        os.close(write_end)
    assert not ran.exists()
    refused_launches.expect([str(program)])


class _Encoded(str):
    """Text that encodes to another path's bytes."""

    def __new__(cls, text: str, other: str | Path):
        value = super().__new__(cls, text)
        value.other = os.fsencode(str(other))
        return value

    def encode(self, *args, **kwargs):
        return self.other


class _Decoded(bytes):
    """Bytes that decode to another path's text."""

    def __new__(cls, data: bytes, other: str):
        value = super().__new__(cls, data)
        value.other = other
        return value

    def decode(self, *args, **kwargs):
        return self.other


@pytest.mark.parametrize(
    "where", ["argument", "args", "executable", "environment", "bytes argument", "bytes executable"]
)
def test_a_value_is_judged_and_started_as_the_text_it_holds(elsewhere, refused_launches, where):
    # a str or bytes subclass answers a reading through methods of its own: Popen encodes the program and
    # the environment through `encode`, and a reading of bytes goes through `decode`. The text the value
    # holds is the one judged and the one started, whatever its type answers
    program, ran = _program(elsewhere, "codex")
    if where.startswith("bytes"):
        held = _Decoded(os.fsencode(str(program)), "/usr/bin/true")
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            if where == "bytes argument":
                subprocess.run([held])
            else:
                subprocess.run([b"true"], executable=held)
        refused_launches.expect([str(program)])
    elif where == "environment":
        os.link(program, elsewhere / "helper")
        held = _Encoded("/usr/bin:/bin", f"{elsewhere}:/usr/bin:/bin")
        done = subprocess.run("helper", shell=True, env={"PATH": held})
        assert done.returncode == 127 and refused_launches == []
    else:
        held = _Encoded("/usr/bin/true", program)
        call = {"argument": ([held],), "args": (held,), "executable": (["true"],)}[where]
        done = subprocess.run(*call, **({"executable": held} if where == "executable" else {}))
        assert done.returncode == 0 and refused_launches == []
    assert not ran.exists()


class _ShellOnlyToPopen:
    """True when `subprocess` asks, false when anything else does."""

    def __bool__(self):
        return Path(sys._getframe(1).f_code.co_filename).name == "subprocess.py"


def test_shell_is_read_once_and_handed_on_as_read(elsewhere, refused_launches):
    # whether the text goes to a shell is read once, as a bool, and that bool is what Popen gets: a value
    # answering the guard one way and Popen another would have the text judged as a program's name and
    # then run as shell text
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError) as refused:
        subprocess.run("$CLI", shell=_ShellOnlyToPopen(), env={"PATH": "/usr/bin:/bin", "CLI": str(program)})
    assert "refused" not in str(refused.value) and refused_launches == []
    assert not ran.exists()


@pytest.mark.launches_cli("claude")
def test_a_declared_launch_starts(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "claude")
    subprocess.run([str(program)], check=True)
    assert ran.exists()
    assert refused_launches == []


@pytest.mark.launches_cli("claude")
def test_a_declaration_covers_only_the_names_it_gives(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "kkernel")
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(program)], check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])


@pytest.mark.launches_cli("claude", "lion1-khive")
def test_a_declaration_of_several_names_covers_those_names_alone(elsewhere, refused_launches):
    # the marker takes any number of names; naming two declares two, not every guarded program
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([str(program)], check=False)
    assert not ran.exists()
    refused_launches.expect([str(program)])
    declared, declared_ran = _program(elsewhere, "lion1-khive")
    subprocess.run([str(declared)], check=True)
    assert declared_ran.exists() and refused_launches == []


@pytest.mark.launches_cli("codex")
def test_a_declaration_covers_the_name_called_though_its_file_is_also_another_guarded_name(
    elsewhere, refused_launches
):
    # `claude` on the child's PATH is a hard link to the `codex` file: called as `codex`, declared as
    # `codex`, it starts; called as `claude`, that name is the one undeclared
    program, ran = _program(elsewhere, "codex")
    os.link(program, elsewhere / "claude")
    env = {"PATH": f"{elsewhere}{os.pathsep}/usr/bin:/bin"}
    subprocess.run([str(program)], check=True, env=env)
    assert ran.exists() and refused_launches == []
    ran.unlink()
    with pytest.raises(FileNotFoundError, match="refused to start 'claude'"):
        subprocess.run(["claude"], env=env)
    assert not ran.exists()
    refused_launches.expect(["claude"])
    # called by no guarded name, the file is one a declared name leads to: it starts
    os.link(program, elsewhere / "helper")
    subprocess.run(["helper"], check=True, env=env)
    assert ran.exists() and refused_launches == []


@pytest.mark.parametrize(
    "name", ["kkernel-helper", "khive-helper", "lion1-khive-helper", "claude-helper", "codex-helper"]
)
def test_a_program_outside_the_guarded_set_starts(elsewhere, refused_launches, name):
    program, ran = _program(elsewhere, name)
    subprocess.run([str(program)], check=True)
    assert ran.exists()
    assert refused_launches == []


def test_the_guarded_set_is_the_khive_binaries_and_the_provider_clis(request):
    # pinned whole: a name added to the set refuses a program the suite may start, a name dropped
    # from it lets one through
    conftest = REPO / "tests" / "conftest.py"
    found = [
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) and Path(plugin.__file__).resolve() == conftest
    ]
    assert len(found) == 1, found
    assert found[0]._GUARDED_PROGRAMS == {"kkernel", "khive", "lion1-khive", "claude", "codex"}


def test_a_stand_in_the_test_wrote_starts_and_a_link_to_one_elsewhere_does_not(
    tmp_path, tmp_path_factory, elsewhere, refused_launches
):
    program, ran = _program(tmp_path, "kkernel")
    subprocess.run([str(program)], check=True)
    assert ran.exists()
    # whatever the file is: a compiled program under the directory is a stand-in too, not only a script
    (binary := tmp_path / "codex").write_bytes(Path("/usr/bin/true").read_bytes())
    binary.chmod(0o755)
    assert subprocess.run([str(binary)]).returncode == 0 and refused_launches == []
    # a link under the test's directory to a program outside it is the program it names
    real, real_ran = _program(elsewhere, "codex")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "codex").symlink_to(real)
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(tmp_path / "bin" / "codex")], check=False)
    assert not real_ran.exists()
    refused_launches.expect([str(tmp_path / "bin" / "codex")])
    # a directory whose name only begins with the session's is not under it
    sibling = Path(str(tmp_path_factory.getbasetemp().resolve()) + "-outside")
    sibling.mkdir()
    try:
        outside, outside_ran = _program(sibling, "codex")
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run([str(outside)], check=False)
        assert not outside_ran.exists()
        refused_launches.expect([str(outside)])
    finally:
        shutil.rmtree(sibling)


# Runs in a pytest of its own with the suite's conftest as a plugin. The launch's error is swallowed the
# way a degrade-quietly caller would, so only the guard's teardown can fail this test.
_SWALLOWING_PROBE = """
import os
import re
import subprocess


def test_swallows_the_refusal():
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass
"""


def _inner_suite(
    tmp_path: Path,
    files: dict[str, str],
    plugin: bool = True,
    later: tuple[str, ...] = (),
    earlier: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """A pytest session of its own over `files` (written under `tmp_path / "probe"`), with this suite's
    conftest as a plugin (unless `plugin` is False: the files then carry the wiring), the modules named
    in `earlier` and `later` (written among the files) as plugins registered before and after it, and
    a `kkernel` stand-in outside its temporary directory, named to it as PROBE_PROGRAM; its summary,
    the stand-in, and the file the stand-in leaves when it runs."""
    program, ran = _program(tmp_path, "kkernel")  # outside the inner session's temporary directory
    (tmp_path / "probe").mkdir()
    for name, text in files.items():
        (tmp_path / "probe" / name).write_text(text)
    env = {
        **os.environ,
        "PROBE_PROGRAM": str(program),
        "PROBE_SEEN": str(tmp_path / "seen"),
        "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO / "pyproject.toml"),
            "--rootdir",
            str(tmp_path / "probe"),
            *(arg for name in earlier for arg in ("-p", f"probe.{name}")),
            *(["-p", "tests.conftest"] if plugin else []),
            *(arg for name in later for arg in ("-p", f"probe.{name}")),
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-p",
            "no:randomly",  # a probe of two tests runs them in the order written
            "--no-cov",
            "--color=no",  # the summary is read as text; CI's FORCE_COLOR=1 would colour it
            f"--basetemp={tmp_path / 'inner'}",
            str(tmp_path / "probe"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    return done, program, ran


def test_a_test_that_swallows_the_refusal_still_fails_and_names_the_launch(tmp_path):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": _SWALLOWING_PROBE})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert f"this test started a program it did not declare: {program}" in said, said
    assert not ran.exists()


# a session-scoped fixture runs before any test's fixtures: the seam is already in place, since the
# conftest took it as it was imported, and the launch is refused with nothing declared; swallowed, it
# fails the first test's setup. KHIVE_NO_DAEMON is set there too.
_WIDER_SCOPE_CONFTEST = """
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def launches_before_any_test():
    Path(os.environ["PROBE_SEEN"]).write_text(os.environ.get("KHIVE_NO_DAEMON", "unset"))
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass
"""
_WIDER_SCOPE_PROBE = """
def test_runs_after_the_session_fixture():
    pass
"""


# A test that reloads `subprocess`, or drops it and imports it again, gets a `Popen` without the seam,
# and its launch through it is not judged: its teardown fails it, naming the replacement, and puts the
# seam back, so the next test's launch is refused.
_REPLACING_PROBE = """
import importlib
import os
import subprocess
import sys
from pathlib import Path


def test_replaces_subprocess_and_launches():
    REPLACE
    sys.modules["subprocess"].run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)


def test_launches_after_it():
    try:
        sys.modules["subprocess"].run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        Path(os.environ["PROBE_SEEN"]).write_text("refused")
"""


@pytest.mark.parametrize(
    "replace",
    [
        "importlib.reload(subprocess)",
        "del sys.modules['subprocess']\n    importlib.import_module('subprocess')",
    ],
    ids=["reloaded", "imported-again"],
)
def test_a_test_that_replaces_subprocess_fails_and_the_seam_is_put_back(tmp_path, replace):
    done, program, ran = _inner_suite(
        tmp_path, {"test_probe.py": _REPLACING_PROBE.replace("REPLACE", replace)}
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "subprocess.Popen lost the launch guard (at this test's teardown)" in said, said
    assert ran.exists()  # the launch through the replaced class ran: that is what the failure names
    assert (tmp_path / "seen").read_text() == "refused"  # the seam is back for the next test
    assert f"this test started a program it did not declare: {program}" in said, said


# A module that reloads `subprocess` as it is imported fails the first test's setup, before the test runs
_REPLACING_AT_IMPORT_PROBE = """
import importlib
import os
import subprocess

importlib.reload(subprocess)


def test_launches():
    subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
"""


def test_a_module_that_reloads_subprocess_as_it_is_imported_fails_the_first_setup(tmp_path):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": _REPLACING_AT_IMPORT_PROBE})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "subprocess.Popen lost the launch guard (before this test)" in said, said
    assert "1 error" in said and "passed" not in said, said
    assert not ran.exists()


# A session-scoped fixture that reloads `subprocess` in its teardown, past the last test, fails the run
_REPLACING_PAST_THE_LAST_TEST_CONFTEST = """
import importlib
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def replaces_subprocess_past_the_last_test():
    yield
    importlib.reload(subprocess)
"""


def test_a_fixture_that_reloads_subprocess_past_the_last_test_fails_the_run(tmp_path):
    done, _program, _ran = _inner_suite(
        tmp_path,
        {"conftest.py": _REPLACING_PAST_THE_LAST_TEST_CONFTEST, "test_probe.py": "def test_x():\n    pass\n"},
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "subprocess.Popen lost the launch guard (after the last test)" in said, said


def test_a_launch_from_a_fixture_of_a_wider_scope_is_refused_and_fails_the_next_test(tmp_path):
    done, program, ran = _inner_suite(
        tmp_path, {"conftest.py": _WIDER_SCOPE_CONFTEST, "test_probe.py": _WIDER_SCOPE_PROBE}
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 error" in said and "passed" not in said, said
    assert f"started a program no test declared (before this test): {program}" in said, said
    assert not ran.exists()
    assert (tmp_path / "seen").read_text() == "1"


def test_a_shell_command_under_startup_state_in_the_environment_is_refused_whole(
    elsewhere, tmp_path, refused_launches
):
    # the shell reads `BASH_ENV` (a file: here one turning `extglob` on, which makes `@(co|do)dex` the
    # program `codex`) before the text, so the text's words are not what starts: refused whole
    program, ran = _program(elsewhere, "codex")
    (startup := tmp_path / "startup.sh").write_text("shopt -s extglob\n")
    path = f"{elsewhere}{os.pathsep}/usr/bin:/bin"
    texts = []
    for name, value in (
        ("BASH_ENV", str(startup)),
        ("BASHOPTS", "extglob"),
        ("SHELLOPTS", "braceexpand"),
    ):
        text = f"@(co|do)dex {name}"
        with pytest.raises(FileNotFoundError, match=f"{name!r} is in the child's environment"):
            subprocess.run(text, shell=True, executable="/bin/bash", env={"PATH": path, name: value})
        texts.append(text)
    assert not ran.exists()
    refused_launches.expect(texts)
    # `ENV` is read by an interactive shell only: a plain program under it starts, bash and sh alike
    safe, safe_ran = _program(elsewhere, "safe-tool")
    for shell in ("/bin/bash", "/bin/sh"):
        subprocess.run("safe-tool", shell=True, executable=shell, env={"PATH": path, "ENV": str(startup)})
    assert safe_ran.exists() and refused_launches == []
    # without one, the same text is a syntax error to bash and starts nothing
    done = subprocess.run(
        "@(co|do)dex", shell=True, executable="/bin/bash", env={"PATH": path}, capture_output=True
    )
    assert done.returncode == 2 and not ran.exists() and refused_launches == []
    # the environment's own names are read: a startup file in the test's environment refuses too
    env = {**os.environ, "BASH_ENV": str(startup), "PATH": path}
    with pytest.raises(FileNotFoundError, match="'BASH_ENV' is in the child's environment"):
        subprocess.run("true", shell=True, env=env)
    refused_launches.expect(["true"])
    # an empty value names no file and turns nothing on: a plain program under one starts
    empty, empty_ran = _program(elsewhere, "safe-empty")
    for name in ("BASH_ENV", "BASHOPTS", "SHELLOPTS"):
        subprocess.run("safe-empty", shell=True, executable="/bin/bash", env={"PATH": path, name: ""})
    assert empty_ran.exists() and refused_launches == []
    # an exported function is imported before the text, and a plain word then names it: `helper` here
    # would start `codex` through a body this reading never sees, so the text is refused whole
    exported = {"PATH": path, "BASH_FUNC_helper%%": "() { codex; }"}
    with pytest.raises(FileNotFoundError, match="'BASH_FUNC_helper%%' is in the child's environment"):
        subprocess.run("helper", shell=True, executable="/bin/bash", env=exported)
    with pytest.raises(FileNotFoundError, match="'BASH_FUNC_sneaky_2%%' is in the child's environment"):
        subprocess.run(
            "sneaky_2",
            shell=True,
            executable="/bin/bash",
            env={"PATH": path, "BASH_FUNC_sneaky_2%%": "() { codex; }"},
        )
    assert not ran.exists()
    refused_launches.expect(["helper", "sneaky_2"])
    # a value not beginning `() {` imports no function (measured: bash defines none, the name is an
    # ordinary variable), so a plain program under one starts
    unshaped, unshaped_ran = _program(elsewhere, "safe-unshaped")
    for value in ("ordinary-value", " () { codex; }"):
        subprocess.run(
            "safe-unshaped",
            shell=True,
            executable="/bin/bash",
            env={"PATH": path, "BASH_FUNC_sneaky%%": value},
        )
    assert unshaped_ran.exists() and not ran.exists() and refused_launches == []
    # without the `%%` the name is an ordinary variable (measured: bash expands it as text and defines
    # no function), read by nothing before the text: a plain program under it starts
    plain, plain_ran = _program(elsewhere, "safe-plain")
    subprocess.run(
        "safe-plain", shell=True, executable="/bin/bash", env={"PATH": path, "BASH_FUNC_helper": "x"}
    )
    assert plain_ran.exists() and refused_launches == []
    # a stand-in as the shell reads no environment this reading knows: it starts under the same name
    stand_in, stand_in_ran = _program(tmp_path, "my-bash")
    subprocess.run("helper", shell=True, executable=str(stand_in), env=exported)
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []


def test_a_shell_command_whose_executable_is_a_guarded_program_is_refused(elsewhere, refused_launches):
    # `shell=True, executable=...` runs that program as the shell: the text is its script
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run("true", shell=True, executable=str(program))
    assert not ran.exists()
    refused_launches.expect([str(program)])


def test_a_calls_executable_is_the_program_it_starts_whatever_its_first_argument_says(
    elsewhere, tmp_path, refused_launches
):
    # without a shell, `executable=` is what starts and `args[0]` is only the name it is shown
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(["safe-name"], executable=str(program))
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):  # a path-like as well
        subprocess.run(["safe-name"], executable=program)
    assert not ran.exists()
    refused_launches.expect([str(program), str(program)])
    stand_in, stand_in_ran = _program(tmp_path, "codex")
    subprocess.run(["codex"], executable=str(stand_in))
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []


class _Shifting(os.PathLike):
    """A path-like that answers its first reading with one path and every later one with another."""

    def __init__(self, first: str, later: str):
        self.first, self.later, self.read = first, later, 0

    def __fspath__(self) -> str:
        self.read += 1
        return self.first if self.read == 1 else self.later


class _ShiftingEnv(dict):
    """An environment whose PATH is one directory on the first reading of its items and another after."""

    def __init__(self, first: str, later: str):
        super().__init__(PATH=first)
        self.later, self.read = later, 0

    def items(self):
        self.read += 1
        return [("PATH", super().__getitem__("PATH") if self.read == 1 else self.later)]


def test_a_calls_values_are_read_once_and_the_reading_is_what_starts(elsewhere, tmp_path, refused_launches):
    # a path-like or a mapping can answer Popen's own reading differently from the guard's: the guard
    # hands Popen what it read, so what starts is what it judged (the first answer, a harmless program)
    program, ran = _program(elsewhere, "codex")
    os.symlink(program, elsewhere / "codex-alias")
    shifting = _Shifting("/usr/bin/true", str(program))
    assert subprocess.run(shifting).returncode == 0 and shifting.read == 1
    shifting = _Shifting("/usr/bin/true", str(program))
    assert subprocess.run(["safe-name"], executable=shifting).returncode == 0 and shifting.read == 1
    shifting = _Shifting("/usr/bin/true", str(program))
    assert subprocess.run([shifting, "x"]).returncode == 0 and shifting.read == 1
    env = _ShiftingEnv("/usr/bin:/bin", f"{elsewhere}{os.pathsep}/usr/bin:/bin")
    assert subprocess.run("codex-alias", shell=True, env=env, capture_output=True).returncode == 127
    assert env.read == 1
    # a `cwd` likewise: a relative program resolves in the first directory read, which holds a plain
    # one, not in the second, where the same name is a hard link to the guarded program
    (plain := elsewhere / "plain").mkdir()
    (linked := elsewhere / "linked").mkdir()
    _, plain_ran = _program(plain, "prog")
    os.link(program, linked / "prog")
    shifting = _Shifting(str(plain), str(linked))
    assert subprocess.run(["./prog"], cwd=shifting).returncode == 0 and shifting.read == 1
    assert plain_ran.exists()
    assert not ran.exists() and refused_launches == []


def test_a_shell_commands_own_path_assignment_is_the_path_its_program_resolves_on(
    elsewhere, tmp_path, refused_launches
):
    # `PATH=<elsewhere> codex` finds the program there, not on the child's PATH (a stand-in under the
    # session's directory): the guard follows the assignment
    program, ran = _program(elsewhere, "codex")
    stand_in, stand_in_ran = _program(tmp_path, "codex")
    env = {"PATH": f"{tmp_path}{os.pathsep}/usr/bin:/bin"}
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(f"PATH={elsewhere} codex", shell=True, env=env)
    assert not ran.exists() and not stand_in_ran.exists()
    refused_launches.expect(["codex"])
    # and the inverse: the assignment selecting the stand-in starts it, whatever the child's PATH holds
    subprocess.run(
        f"PATH={tmp_path}:/usr/bin:/bin codex",
        shell=True,
        env={"PATH": f"{elsewhere}{os.pathsep}/usr/bin:/bin"},
    )
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []
    # a leading `PATH+=` appends to the child's PATH (bash and this platform's sh alike), so the words
    # after it resolve on a PATH this reading does not assemble: refused whole, as is `HOME+=`
    texts = [f"PATH+=:{elsewhere} codex-alias", "HOME+=/x codex-alias"]
    (tmp_path / "codex-alias").symlink_to(program)
    for text in texts:
        with pytest.raises(FileNotFoundError, match="changes where a later word"):
            subprocess.run(text, shell=True, env={"PATH": "/usr/bin:/bin"})
    assert not ran.exists()
    refused_launches.expect(texts)


def test_a_path_assignment_the_shell_would_expand_resolves_nothing_and_is_refused(
    elsewhere, tmp_path, refused_launches
):
    # refused by the assignment itself, before any word is looked up: a stand-in under the session's
    # directory in the child's cwd must not stand in for what the shell's PATH would find
    program, ran = _program(elsewhere, "codex")
    stand_in, stand_in_ran = _program(tmp_path, "codex")
    texts = [f"PATH={elsewhere}:$PATH codex", f"PATH={elsewhere}:$PATH echo"]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, cwd=tmp_path, env={"PATH": f"{elsewhere}:/usr/bin:/bin"})
    assert not ran.exists() and not stand_in_ran.exists()
    refused_launches.expect(texts)
    # a `~` after a `:` in an assignment's value is expanded (the alias is a name the guard would pass,
    # on a PATH the shell reads as the home); after a `:` in an argument it is not, and the word starts
    os.link(program, elsewhere / "colon-alias")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(elsewhere)}
    with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
        subprocess.run("PATH=/usr/bin:~ colon-alias", shell=True, cwd=tmp_path, env=env)
    assert not ran.exists()
    refused_launches.expect(["PATH=/usr/bin:~ colon-alias"])
    done = subprocess.run("printf x:~", shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "x:~" and refused_launches == []
    # after the `=` of an assignment-shaped word bash expands a `~`, an argument's included (`x=~`,
    # `x+=~`), and the text is refused; a word not shaped as an assignment (`a-b=~`) is literal in both
    texts = ["printf '%s' x=~", "printf '%s' x+=~"]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, executable="/bin/bash", env=env)
    refused_launches.expect(texts)
    # sh leaves both as written (measured: `/bin/sh -c "printf '%s' x=~"` prints `x=~`, bash prints the
    # home), and the scan, which does not tell one shell from another, refuses them there too: a refusal
    # of a text that would have expanded nothing, never a launch let through
    for text in texts:
        with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
            subprocess.run(text, shell=True, env=env)
    refused_launches.expect(texts)
    done = subprocess.run("printf '%s' a-b=~", shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "a-b=~" and refused_launches == []
    # the shell expands a `~` beginning a PATH entry as it searches, whatever quoting the entry came
    # through (the quotes are gone by then) and whether the text or the environment set it
    texts = [
        'PATH=/usr/bin:""~ colon-alias',
        "PATH=/usr/bin:'~' colon-alias",
        'PATH="/usr/bin:~" colon-alias',
    ]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="begins with a `~`, which the shell expands"):
            subprocess.run(text, shell=True, cwd=tmp_path, env=env)
    with pytest.raises(FileNotFoundError, match="begins with a `~`, which the shell expands"):
        subprocess.run("colon-alias", shell=True, cwd=tmp_path, env={**env, "PATH": "/usr/bin:~"})
    assert not ran.exists()
    refused_launches.expect([*texts, "colon-alias"])
    # an entry a `~` does not begin is not expanded, and a program started without a shell resolves no
    # entry at all: those start
    done = subprocess.run('PATH=/usr/bin:"x"~ printf x', shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "x" and refused_launches == []
    done = subprocess.run(
        ["/usr/bin/printf", "x"], env={**env, "PATH": "/usr/bin:~"}, capture_output=True, text=True
    )
    assert done.stdout == "x" and refused_launches == []


def test_a_link_to_a_guarded_program_under_another_name_is_that_program(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "codex")
    alias = elsewhere / "codex-alias"
    alias.symlink_to(program)
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run([str(alias)])
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):  # the alias by its bare name
        subprocess.run(["codex-alias"], env={"PATH": str(elsewhere)})
    assert not ran.exists()
    refused_launches.expect([str(alias), "codex-alias"])


def test_a_bytes_path_is_judged_like_a_str_one(elsewhere, refused_launches):
    program, ran = _program(elsewhere, "claude")
    with pytest.raises(FileNotFoundError, match="refused to start 'claude'"):
        subprocess.run(os.fsencode(str(program)))
    with pytest.raises(FileNotFoundError, match="refused to start 'claude'"):
        subprocess.run([os.fsencode(str(program)), b"-p"])
    assert not ran.exists()
    refused_launches.expect([str(program)] * 2)


# names that start a program without passing `subprocess.Popen` whatever holds them (`os`, `posix`, `nt`,
# a module that imported one, `subprocess`, `concurrent.futures`), or run code the scan cannot read (a
# loader's module, an interpreter's text, a code object, an entry point, which imports the module its
# value names: any name holding `entry_point` or `entrypoint`, in either case, so a plugin manager's
# loader (`load_setuptools_entrypoints`) reads as one too), so read on any object, imported or quoted,
# and as a keyword; and the calls of the libraries the code under test imports that do the same: a
# native loader (`numpy.ctypeslib`), a pickle read (`allow_pickle=`, `load_str_bytes`), a script runner
# (`CGIHTTPRequestHandler` forks and execs), a dotted name resolved (`ImportString`, `import_string`,
# `import_from_string`), text evaluated (`ForwardRef` and the evaluators of the standard library and of
# pydantic, by name or by family: `get_type_hints` and its kin, `eval_type` and its variants,
# `evaluate_forward_ref`, `resolve_annotations`, the forward-reference updaters, `model_rebuild`, a
# field rebuild, `eval_str=`, and the annotation readers of `inspect` (`get_annotations`, `signature`,
# `Signature.from_callable`), whatever keywords they are given), a code object made from text or a file
# (`source_to_code`, a loader's `get_code`), an unsafe YAML load.
# The evaluators end at the ones named here, and no list of them can be finished: any call that
# resolves an annotation evaluates text (`functools.singledispatch` registering a function by one, a
# validator, a serializer), and text built while the code runs, from code points or from data, names
# nothing a reading of the source can see. A launch hidden in such text is outside this scan. What the
# scan holds is narrower: code that launches, or evaluates text through a call named here, fails it
# the moment it is written, so ordinary code cannot move off the guard's seam unseen. ADR-0027 states
# this line and what would falsify it; `test_the_census_boundary_is_named_calls_as_written` pins it.
_LAUNCH_NAMES = re.compile(
    r"exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?|_?fork_exec|startfile|ProcessPoolExecutor"
    r"|exec_module|load_module|_gcd_import|_find_and_load\w*"
    r"|spec_from_file_location|spec_from_loader|module_from_spec|\w*FileLoader|FileFinder|PathFinder|LazyLoader"
    r"|runsource|runcode|CodeType|source_to_code|get_code"
    r"|\w*type_hints|_?eval_type\w*|try_eval_type|_evaluate|evaluate_forward_?ref|eval_str|resolve_annotations"
    r"|get_annotations|signature|from_callable"
    r"|\w*forward_refs\w*|model_rebuild|rebuild_\w+_fields|load_str_bytes"
    r"|(?i:\w*entry_?points?\w*)"
    r"|ctypeslib|allow_pickle|CGIHTTPRequestHandler|ImportString|import_string|import_from_string|ForwardRef"
    r"|unsafe_load\w*|UnsafeLoader|full_load\w*|FullLoader"
)
# modules whose import alone is a way to start one: native calls (`ctypes.CDLL(None).system`),
# multiprocessing's own spawns, the Windows process API, the private fork and exec; or to run code the
# scan cannot read: from bytes (`pickle`, `marshal`, `shelve`), a file or module (`runpy`, `zipimport`),
# a dotted name resolved to what it names (`pydoc.locate`, `pkgutil.resolve_name`,
# `pkg_resources.load_entry_point`), a package named by a string and imported to read its files
# (`importlib.resources.files`), a plugin manager that imports entry points under names of its own
# (`stevedore.ExtensionManager`, `pluggy`), or text (the debuggers, profilers, `timeit`, `doctest`, `codeop`);
# and uvicorn's process supervisors and its importer, which start workers through `multiprocessing` and
# import an app named by a string
_LAUNCH_MODULES = frozenset(
    {"_posixsubprocess", "ctypes", "_ctypes", "cffi", "multiprocessing", "_multiprocessing", "_winapi"}
    | {"pickle", "_pickle", "marshal", "shelve", "runpy", "zipimport", "pydoc", "pkgutil", "pkg_resources"}
    | {"importlib.resources", "importlib_resources"}
    | {"pluggy", "stevedore"}
    | {"pdb", "bdb", "cProfile", "timeit", "doctest", "codeop"}
    | {"uvicorn.supervisors", "uvicorn.importer"}
    | {"importlib._bootstrap", "importlib._bootstrap_external", "_frozen_importlib"}
    | {"_frozen_importlib_external", "_imp", "importlib.machinery"}
)
# the same, for modules named by an ordinary word (an attribute or a string `code` or `profile` is no
# launch): a launch when imported by that dotted name only. `site` runs the import lines of the `.pth`
# files in a directory it is handed (`addsitedir`)
_TEXT_RUNNERS = frozenset({"code", "profile", "trace", "logging.config", "site"})
# modules read only through literal names, with the names of their own that start a program or run text
# the scan cannot read (`subprocess` has none beyond those read everywhere; `importlib`'s importers are
# judged where they are called, its private names reach the machinery that imports by a name read
# nowhere, `_bootstrap._gcd_import`, and `resources` is the module above, reached as an attribute)
_PAST_SUBPROCESS = {
    "os": re.compile(r"system"),
    "posix": re.compile(r"system"),
    "nt": re.compile(r"system"),
    "pty": re.compile(r"spawn"),
    "subprocess": None,
    "importlib": re.compile(r"_\w*|resources"),
    "builtins": re.compile(r"exec|eval|compile|__import__"),
    # `uvicorn.run` starts its workers and its reloader through `multiprocessing`; `Config` and `Server`
    # serve in the calling process
    "uvicorn": re.compile(r"run|main|supervisors|importer"),
    # the import machinery's own lists; `sys.path` is judged by how it is used (`_import_path_handed_on`)
    "sys": re.compile(r"meta_path|path_hooks|path_importer_cache"),
}
# the names of `sys` that are import paths, refused in a `from` import under any name
_SYS_IMPORT_PATHS = ("modules", "path", "meta_path", "path_hooks", "path_importer_cache")
# files Python imports without source, under a package or a directory on the path: the census cannot
# read them, so the tree holds none
_SOURCELESS = (".pyc", ".pyo", ".pyd", ".so", ".zip", ".egg", ".whl")
# the suffixes this interpreter imports a module file by, longest first so `.abi3.so` is read before `.so`
_MODULE_SUFFIXES = tuple(sorted(importlib.machinery.all_suffixes(), key=len, reverse=True))


def _import_path_handed_on(node: ast.expr, parent: dict[ast.AST, ast.AST]) -> bool:
    # an import path (`sys.path`, a package's `__path__`, `getattr(sys, "path")`) is read in place:
    # iterated, indexed or compared. Anything else (assigned, deleted, a method called on it, handed to a
    # call, stored under a name) can put a directory, a zip or a `.pyc` the census never reads on it, so
    # it counts as a launch. A call has no context: it is a value, loaded
    if not isinstance(getattr(node, "ctx", ast.Load()), ast.Load):
        return True
    up = parent.get(node)
    if isinstance(up, (ast.For, ast.AsyncFor, ast.comprehension)) and up.iter is node:
        return False
    if isinstance(up, ast.Subscript) and up.value is node and isinstance(up.ctx, ast.Load):
        return False
    return not isinstance(up, ast.Compare)


# attributes through which any object reaches one of those modules, or the names a module, function or
# frame holds, or the code a function runs: read as an attribute, or named as quoted text, which is how
# `getattr(sys, "modules")` and `vars(shutil)["os"]` reach the same
_REACHING = {"os", "posix", "nt", "pty", "builtins", "modules", "__globals__", "__builtins__", "__dict__"} | {
    "f_globals",
    "f_locals",
    "f_builtins",
    "__code__",
}
# the calls that import a module named by text: `find_spec` imports the package above a dotted name to
# look in it, so `find_spec("pkg.child")` runs `pkg/__init__.py`
_IMPORTERS = ("import_module", "__import__", "find_spec")


def _importer_name(call: ast.Call) -> ast.expr | None:
    # the module name an importer is handed: its first argument, or `name=`, which all three accept
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "name"), None)


def _imports_a_launcher(dotted: str) -> bool:
    # `a.b.c` names `a`, `a.b` and `a.b.c`, so `import ctypes.util` and `from logging import config` count
    parts = dotted.split(".")
    return any(".".join(parts[: i + 1]) in _LAUNCH_MODULES | _TEXT_RUNNERS for i in range(len(parts)))


def _launches_past_subprocess(
    source: str, outer_bound: dict[str, str] | None = None, outer_importers: set[str] | None = None
) -> list[str]:
    # read as code, not as text, so an alias reaches the same call. A launch name above is a hit on any
    # object, in any import and as any quoted text; a module that starts programs by itself is a hit on
    # import, by name or reached as an attribute. A module in `_PAST_SUBPROCESS` is read only through a
    # literal name (`os.system`, `getattr(os, "system")`, `hasattr(os, ...)`); any other use of it (a
    # computed name, `vars(os)`, a dunder, the module stored or passed) counts as a launch, since what it
    # reaches cannot be read, and so do reaching one through another object, by attribute or by its name as
    # quoted text, an import of a computed module name, an importer stored, passed or aliased (an import of
    # it under another name included), a star import, a read of `globals()`, `locals()` or `vars()`, and
    # `exec`, `eval` or `compile` of text. An importer is called by its own name wherever it was imported
    # from, so it reaches another module only under that name, which this scan reads. An evaluator named above
    # is a hit whatever text it is handed. Literal text that reads as Python, a string or bytes constant
    # anywhere (each literal part of an f-string among them), is read again as code, with the names of the
    # module it stands in, since a library can evaluate it without being named: a pydantic model evaluates
    # its string annotations as it is built, as `typing.get_type_hints` and `functools.singledispatch` do
    # when called. Two things are not followed, and a launch hidden either way is beyond a source scan: a
    # lookup by a computed name on any other object, which the code under test makes on its own objects;
    # and text a computation builds or reads at run time (a concatenation, a format, an f-string's
    # replacement field, a decode, a join, a file's contents) handed to an evaluator the code does not name,
    # or to one of pydantic's schema builders (a model class, `create_model`, `TypeAdapter`,
    # `validate_call`), which evaluate the string forward references in the types they are handed as they
    # build a schema. Those builders are not launch names: every model the code under test defines is built
    # by one, so the name would refuse it whole, and the literal text in their annotations is read above.
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bound = {"__builtins__": "builtins", **(outer_bound or {})}
    importers, hits = {"__import__", *(outer_importers or ())}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if _imports_a_launcher(a.name):
                    hits.append(f"import {a.name}")
                # `import os.path` binds `os`; `import os.path as p` binds `os.path`, not `os`
                elif top in _PAST_SUBPROCESS and (a.asname is None or a.name == top):
                    bound[a.asname or top] = top
        elif isinstance(node, ast.ImportFrom):
            # a relative import names a module of this tree, which the scan reads as itself
            module = (node.module or "") if node.level == 0 else ""
            rule = _PAST_SUBPROCESS.get(module)
            for a in node.names:
                if (
                    (module and _imports_a_launcher(f"{module}.{a.name}"))
                    or a.name in _LAUNCH_MODULES
                    or _LAUNCH_NAMES.fullmatch(a.name)
                    # a star import binds names this scan does not list, whatever module it names, an
                    # importer another module of this tree holds among them (ruff's F403 refuses one too)
                    or a.name == "*"
                    # an importer imported under another name is imported under that name from this
                    # module by code that does not read it as one. The scan does not follow the name
                    # to the module that binds it, so a function of this tree that only shares an
                    # importer's name is refused the same way: import it under its own name, or name
                    # it something else
                    or (a.name in _IMPORTERS and a.asname not in (None, a.name))
                    or (rule and rule.fullmatch(a.name))
                    # a package's path imported into a name the scan does not read as one
                    or a.name == "__path__"
                ):
                    hits.append(
                        f"from {'.' * node.level}{node.module or ''} import {a.name}"
                        + (f" as {a.asname}" if a.asname else "")
                    )
                elif a.name in _PAST_SUBPROCESS:
                    bound[a.asname or a.name] = a.name
                elif a.name in _IMPORTERS:
                    # under its own name, from `importlib` or from any module that imported it
                    importers.add(a.name)
                elif module == "sys" and a.name in _SYS_IMPORT_PATHS:
                    hits.append(f"from sys import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in bound:
            module, up = bound[node.id], parent.get(node)
            rule = _PAST_SUBPROCESS[module]
            if isinstance(up, ast.Attribute):
                if (rule and rule.fullmatch(up.attr)) or up.attr.startswith("__"):
                    hits.append(f"{module}.{up.attr}")
                elif module == "sys" and up.attr == "path" and _import_path_handed_on(up, parent):
                    hits.append("sys.path handed on")
            elif (
                isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.args and up.args[0] is node
            ):
                name = up.args[1] if len(up.args) > 1 else None
                if up.func.id == "hasattr":
                    pass
                elif up.func.id != "getattr":
                    hits.append(f"{module} passed to {up.func.id}()")
                elif isinstance(name, ast.Constant) and isinstance(name.value, str):
                    if (rule and rule.fullmatch(name.value)) or name.value.startswith("__"):
                        hits.append(f"getattr({module}, {name.value!r})")
                    elif module == "sys" and name.value == "path" and _import_path_handed_on(up, parent):
                        hits.append("sys.path handed on")
                else:
                    hits.append(f"getattr({module}, <computed>)")
            else:
                hits.append(f"{module} used as a value")
        elif (
            (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in importers)
            or (isinstance(node, ast.Attribute) and node.attr in _IMPORTERS)
        ) and not (isinstance(parent.get(node), ast.Call) and parent[node].func is node):
            # an importer handed on, stored or aliased is called where its argument cannot be read
            hits.append(f"{ast.unparse(node)} used as a value")
        elif isinstance(node, (ast.Name, ast.Attribute)) and (
            (node.id if isinstance(node, ast.Name) else node.attr) == "__path__"
        ):
            # a package's own path, as a name in its `__init__` or as an attribute of any object
            if _import_path_handed_on(node, parent):
                hits.append("__path__ handed on")
        elif isinstance(node, ast.Attribute) and (
            node.attr in _REACHING
            or (
                isinstance(node.ctx, ast.Load)
                and (node.attr in _LAUNCH_MODULES or _LAUNCH_NAMES.fullmatch(node.attr))
            )
        ):
            hits.append(f".{node.attr}")
        elif isinstance(node, ast.keyword) and node.arg and _LAUNCH_NAMES.fullmatch(node.arg):
            hits.append(f"{node.arg}=")
        elif isinstance(node, ast.Constant) and node.value == "__path__":
            # a package's path named as quoted text: `hasattr(x, "__path__")` reads nothing, a comparison
            # reads the text, and `getattr(x, "__path__")` is read as the path it returns; any other use
            # (`setattr`, a key, the text stored or passed) counts as a launch, since what it reaches
            # cannot be read
            up = parent.get(node)
            if isinstance(up, ast.Compare):
                pass
            elif not (
                isinstance(up, ast.Call)
                and isinstance(up.func, ast.Name)
                and len(up.args) > 1
                and up.args[1] is node
            ):
                hits.append("'__path__'")
            elif up.func.id == "getattr":
                if _import_path_handed_on(up, parent):
                    hits.append("getattr(..., '__path__') handed on")
            elif up.func.id != "hasattr":
                hits.append(f"'__path__' passed to {up.func.id}()")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (
                node.value in _LAUNCH_MODULES
                or _LAUNCH_NAMES.fullmatch(node.value)
                or node.value in _IMPORTERS
                or node.value in _REACHING
            )
        ):
            hits.append(repr(node.value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            # bytes are read as the text they decode to; bytes that are not UTF-8 raise a ValueError
            try:
                text = node.value if isinstance(node.value, str) else node.value.decode()
                inner = _launches_past_subprocess(text, bound, importers)
            except (SyntaxError, ValueError):
                inner = []
            hits.extend(f"{node.value[:40]!r} read as code: {h}" for h in inner)
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in importers) or (
                isinstance(f, ast.Attribute) and f.attr in _IMPORTERS
            ):
                arg = _importer_name(node)
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    hits.append("an import of a computed module name")
                elif arg.value.split(".")[0] in _PAST_SUBPROCESS or _imports_a_launcher(arg.value):
                    hits.append(f"an import of {arg.value!r}")
            elif isinstance(f, ast.Name) and f.id in ("exec", "eval", "compile"):
                hits.append(f"{f.id}() of text")
            elif isinstance(f, ast.Name) and (
                f.id in ("globals", "locals") or (f.id == "vars" and not node.args)
            ):
                # the namespace that holds every module the code imported, read by a key
                hits.append(f"{f.id}() read")
    return hits


# every module the code under test imports beside its own, by its top-level name: each read for the calls
# that start a program or run code from data, text or a name past `subprocess` (those the scan above
# reads), so a module new to the code under test fails the census, named, until it is read and added,
# and one no longer imported is taken out. What a library does with a value the code hands it (a string
# `uvicorn.Config` would import as an app, an annotation pydantic evaluates) is the library's, beyond a
# source scan of this tree
_IMPORTED = frozenset(
    {"__future__", "argparse", "ast", "asyncio", "atexit", "base64", "calendar", "collections"}
    | {"contextlib", "contextvars", "copy", "dataclasses", "datetime", "decimal", "difflib", "enum"}
    | {"email", "errno", "fcntl", "fnmatch", "fractions", "functools", "getpass", "glob", "hashlib", "html"}
    | {"http", "importlib", "inspect", "io", "ipaddress", "itertools", "json", "keyword", "math"}
    | {"operator", "os", "pathlib", "plistlib", "posixpath", "re", "secrets", "select", "selectors"}
    | {"shlex", "shutil", "signal", "socket", "ssl", "stat", "string", "struct", "subprocess", "sys"}
    | {"tarfile", "tempfile", "termios", "textwrap", "threading", "time", "tokenize", "tomllib", "types"}
    | {"typing", "unicodedata", "urllib", "uuid", "warnings", "weakref", "xml"}
    | {"daytona", "fastapi", "httpx", "mcp", "mlx_whisper", "numpy", "prompt_toolkit", "pydantic"}
    | {"pydantic_core", "rich", "runta", "sounddevice", "starlette", "swebench", "uvicorn"}
)


def _imported_modules(source: str, outer_importers: dict[str, str] | None = None) -> set[str]:
    # the top-level name of each module imported by name: an import, an absolute `from` import, and a
    # literal one handed to an importer (`_IMPORTERS`), under its own name or the one a `from importlib`
    # or `from importlib.util` import binds it to (an importer stored or passed is a launch above, and a
    # computed name is pinned as one, below); and the same in literal text that reads as Python, read
    # again as code as the scan above reads it, since a library can evaluate it. `find_spec` imports only
    # the package above the name it looks up, so a top-level name it is handed pins nothing. A relative
    # name is pinned as written: the package it is relative to is not read here, so the census fails
    # naming it. An importer imported under another name, from any module, and a star import are pinned
    # as that, since other modules can import the importer under the name it binds, which the census
    # does not read as one
    tree = ast.parse(source)
    # each name an importer is called by, to the importer it is
    importers = (
        {n: n for n in _IMPORTERS}
        | dict(outer_importers or {})
        | {
            a.asname or a.name: a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for a in node.names
            if a.name in _IMPORTERS
        }
    )
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
            for a in node.names:
                if a.name == "*":
                    found.add(f"<a star import from {'.' * node.level}{node.module or ''}>")
                elif a.name in _IMPORTERS and a.asname not in (None, a.name):
                    found.add(f"<{a.name} imported as {a.asname}>")
        elif isinstance(node, ast.Call):
            f, arg = node.func, _importer_name(node)
            called = importers.get(f.id) if isinstance(f, ast.Name) else getattr(f, "attr", None)
            name = arg.value if isinstance(arg, ast.Constant) else None
            if called not in _IMPORTERS:
                pass
            elif not isinstance(name, str):
                # a name the census cannot read, starred or computed, is pinned as that, so the census
                # fails naming it even where the scan above did not see the importer
                found.add(f"<a computed name handed to {called}>")
            elif name.startswith("."):
                found.add(name)
            elif called != "find_spec" or "." in name:
                found.add(name.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            try:
                text = node.value if isinstance(node.value, str) else node.value.decode()
                found |= _imported_modules(text, importers)
            except (SyntaxError, ValueError):
                pass
    return found


def _own_modules(files: list[Path]) -> set[str]:
    # a script is imported by its siblings under its stem, with its directory on the path; a package by
    # its top-level name. A module inside a package (`hub/tools/daytona.py`) is neither, so the library
    # it shares a name with is not mistaken for it
    own = set()
    for f in files:
        if not (f.parent / "__init__.py").exists():
            own.add(f.stem)
        top = f.parent
        while (top / "__init__.py").exists() and (top.parent / "__init__.py").exists():
            top = top.parent
        if (top / "__init__.py").exists():
            own.add(top.name)
    return own


def _module_files_left_out(left_out: Path, repo: Path) -> list[Path]:
    # a directory the census leaves out is a package to Python all the same when every name from the tree
    # down to it is one an import can spell (`hub/__pycache__`, `hub/node_modules`), so a module file in it
    # (`hub/__pycache__/evil.py`, `hub/node_modules/evil.pyc`) is imported with its launches unread. Only
    # the `import` statement needs an identifier: `importlib.import_module` takes any name without a dot
    # in it, `hub.node_modules.node-gyp.payload` included, so a name with a dot in it (`.cache`, `pkg.v2`)
    # is the one no import can spell. A tagged bytecode cache (`x.cpython-311.pyc`) is not a module file
    # for the same reason. A linked directory under one is a package Python enters and this walk never
    # does, whatever it is called, as a link anywhere else in the tree is
    if any("." in p for p in left_out.relative_to(repo).parts):
        return []
    found = []
    for d, dirs, names in os.walk(left_out):
        found += [Path(d, n) for n in dirs if Path(d, n).is_symlink()]
        dirs[:] = [n for n in dirs if "." not in n]
        for n in names:
            suffix = next((s for s in _MODULE_SUFFIXES if n.endswith(s)), None)
            if suffix and "." not in n[: -len(suffix)]:
                found.append(Path(d, n))
    return found


def _source_population(repo: Path, tests: list[Path]) -> list[Path]:
    # every Python file of the tree outside the directories pytest collects tests from
    files = [
        f
        for f in repo.rglob("*.py")
        if not any(
            p.startswith(".") or p in ("__pycache__", "node_modules") for p in f.relative_to(repo).parts
        )
        and not any(f.is_relative_to(t) for t in tests)
    ]
    # the same population counted a second way, since a file left out above is one whose launches go
    # unread while every root below still finds one
    walked, linked, sourceless, left_out = set(), [], [], []
    for d, dirs, names in os.walk(repo):
        # read before any name is left out: Python enters a linked directory whatever it is called, and
        # imports a module file from a left-out directory it can name
        linked += [Path(d, n) for n in dirs if Path(d, n).is_symlink()]
        left_out += [
            f
            for n in dirs
            if n in ("__pycache__", "node_modules")
            for f in _module_files_left_out(Path(d, n), repo)
        ]
        dirs[:] = [
            n
            for n in dirs
            if not n.startswith(".") and n not in ("__pycache__", "node_modules") and Path(d, n) not in tests
        ]
        walked |= {Path(d, n) for n in names if n.endswith(".py") and not n.startswith(".")}
        sourceless += [Path(d, n) for n in names if n.endswith(_SOURCELESS)]
    # neither count enters a directory through a link, and Python imports a package through one: a
    # link to a dot-named directory, which no import can name, or to a package outside the tree, both
    # under a name the census reads as the tree's own. So the tree holds no linked directory
    assert linked == [], sorted(str(p.relative_to(repo)) for p in linked)
    # a `.pyc` beside a package, a compiled module or a zip on a path is imported with no source to
    # read; the code that would put a directory on a path is refused by the scan (`__path__`, `sys.path`)
    assert sourceless == [], sorted(str(p.relative_to(repo)) for p in sourceless)
    # a module file under `__pycache__` or `node_modules` is imported as a module of the package above
    # them, which the counts never read, and a linked directory under either is entered the same way
    assert left_out == [], sorted(str(p.relative_to(repo)) for p in left_out)
    assert set(files) == walked, sorted(str(f.relative_to(repo)) for f in set(files) ^ walked)
    return files


def test_the_code_under_test_launches_through_subprocess_only():
    # the guard sits on subprocess alone (its docstring says what it does not see); this keeps the code
    # under test on that seam, so a launch past it is a change this test sees first (what a reading of
    # names cannot see is stated above `_LAUNCH_NAMES`)
    # leaving a directory out of the scan means leaving it out of `testpaths`, where the suite stops
    # collecting it
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    tests = [REPO / t for t in config["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert {t.relative_to(REPO).as_posix() for t in tests} == {"tests", "apps/cli/tests"}
    files = _source_population(REPO, tests)
    for root in (
        "src",
        "hub",
        "apps/cli/lion_cli",
        "bench",
        "examples",
        "docs",
    ):  # each read, none gone empty
        assert any(f.is_relative_to(REPO / root) for f in files), root
    hits = [f"{f.relative_to(REPO)}: {h}" for f in files for h in _launches_past_subprocess(f.read_text())]
    assert hits == [], hits
    own = _own_modules(files)
    assert {"lion1", "hub", "lion_cli"} <= own, own
    imported = {(m, f.relative_to(REPO).as_posix()) for f in files for m in _imported_modules(f.read_text())}
    new = sorted(f"{path}: {m}" for m, path in imported if m not in own | _IMPORTED)
    assert new == [], f"modules new to the code under test, read and added to _IMPORTED first: {new}"
    gone = sorted(_IMPORTED - {m for m, _ in imported})
    assert gone == [], f"no longer imported, taken out of _IMPORTED: {gone}"


def test_a_package_reached_through_a_linked_directory_stops_the_census(tmp_path):
    # a package under a dot-named directory, imported through a link of an importable name, and a link
    # to a package outside the tree: neither count reads their files, so the census stops at the link
    (tmp_path / "hub" / ".hidden").mkdir(parents=True)
    (tmp_path / "hub" / "__init__.py").write_text("")
    (tmp_path / "hub" / ".hidden" / "__init__.py").write_text("import os\nos.system('x')\n")
    (tmp_path / "hub" / "alias").symlink_to(tmp_path / "hub" / ".hidden", target_is_directory=True)
    with pytest.raises(AssertionError, match="hub/alias"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "alias").unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "__init__.py").write_text("import os\nos.system('x')\n")
    (tmp_path / "hub" / "vendored").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError, match="hub/vendored"):
        _source_population(tmp_path, [tmp_path / "tests"])
    # the control: the same tree without a link is counted, and the dot-named directory is left out
    (tmp_path / "hub" / "vendored").unlink()
    assert _source_population(tmp_path, [tmp_path / "tests"]) == [tmp_path / "hub" / "__init__.py"]
    # a link under a dot name is read before the name leaves it out: a `__path__` entry can name it
    (tmp_path / "hub" / ".alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError, match=r"hub/\.alias"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / ".alias").unlink()
    # a linked file is counted by both, and its launches are read
    (outside / "linked.py").write_text("import os\nos.system('x')\n")
    (tmp_path / "hub" / "linked.py").symlink_to(outside / "linked.py")
    assert set(_source_population(tmp_path, [tmp_path / "tests"])) == {
        tmp_path / "hub" / "__init__.py",
        tmp_path / "hub" / "linked.py",
    }
    assert _launches_past_subprocess((tmp_path / "hub" / "linked.py").read_text()) == ["os.system"]
    # a file imported with no source stops the census, wherever it is: a `.pyc` beside the package, a zip
    # a path entry can name
    (tmp_path / "hub" / "payload.pyc").write_bytes(b"")
    with pytest.raises(AssertionError, match=r"hub/payload\.pyc"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "payload.pyc").unlink()
    (tmp_path / "hub" / "bundle.zip").write_bytes(b"")
    with pytest.raises(AssertionError, match=r"hub/bundle\.zip"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "bundle.zip").unlink()


def test_a_module_file_under_a_left_out_directory_stops_the_census(tmp_path):
    # `__pycache__` and `node_modules` are left out of both counts, and Python imports a module file
    # from either as `hub.__pycache__.evil` or `hub.node_modules.evil`, since each is a name an import
    # can spell; the tagged bytecode a cache holds is not a module file
    (tmp_path / "hub" / "__pycache__").mkdir(parents=True)
    (tmp_path / "hub" / "__init__.py").write_text("")
    (tmp_path / "hub" / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"")
    assert _source_population(tmp_path, [tmp_path / "tests"]) == [tmp_path / "hub" / "__init__.py"]
    (tmp_path / "hub" / "__pycache__" / "evil.py").write_text("import os\nos.system('x')\n")
    with pytest.raises(AssertionError, match=r"hub/__pycache__/evil\.py"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "__pycache__" / "evil.py").unlink()
    (tmp_path / "hub" / "node_modules" / "deep").mkdir(parents=True)
    (tmp_path / "hub" / "node_modules" / "deep" / "evil.pyc").write_bytes(b"")
    with pytest.raises(AssertionError, match=r"hub/node_modules/deep/evil\.pyc"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "node_modules" / "deep" / "evil.pyc").unlink()
    # a linked directory under a left-out one, under any name: the walk never enters it, and Python
    # imports `hub.__pycache__.shadow` through it
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "shadow.py").write_text("import os\nos.system('x')\n")
    (tmp_path / "hub" / "__pycache__" / "shadow").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError, match=r"hub/__pycache__/shadow"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "__pycache__" / "shadow").unlink()
    (tmp_path / "hub" / "node_modules" / "deep" / ".alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError, match=r"hub/node_modules/deep/\.alias"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "node_modules" / "deep" / ".alias").unlink()
    # a name the `import` statement cannot spell is one `importlib.import_module` can: a file below
    # `node-gyp` is imported as `hub.node_modules.node-gyp.payload`
    (tmp_path / "hub" / "node_modules" / "node-gyp").mkdir()
    (tmp_path / "hub" / "node_modules" / "node-gyp" / "payload.py").write_text("import os\nos.system('x')\n")
    with pytest.raises(AssertionError, match=r"hub/node_modules/node-gyp/payload\.py"):
        _source_population(tmp_path, [tmp_path / "tests"])
    (tmp_path / "hub" / "node_modules" / "node-gyp" / "payload.py").unlink()
    # the controls: a dot in a name is what no import can spell (a dot splits the name), so a file under
    # one, a left-out directory under one, and a link under that are not reported
    (tmp_path / "hub" / "node_modules" / "pkg.v2").mkdir()
    (tmp_path / "hub" / "node_modules" / "pkg.v2" / "evil.py").write_text("import os\nos.system('x')\n")
    (tmp_path / "hub" / "node_modules" / "pkg.v2" / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / ".cache" / "node_modules").mkdir(parents=True)
    (tmp_path / ".cache" / "node_modules" / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / ".cache" / "node_modules" / "evil.py").write_text("import os\nos.system('x')\n")
    assert _source_population(tmp_path, [tmp_path / "tests"]) == [tmp_path / "hub" / "__init__.py"]


def test_the_source_check_reads_every_launch_the_os_module_has():
    # taken from the module itself, so each optional letter of a family is a name read
    names = [n for n in dir(os) if n == "system" or n.startswith(("exec", "spawn", "posix_spawn"))]
    assert {"system", "posix_spawnp", "spawnle", "spawnvpe", "execl", "execvpe"} <= set(names), names
    assert [n for n in names if not _launches_past_subprocess(f"import os\nos.{n}(x)")] == []
    assert [n for n in names if not _launches_past_subprocess(f"import posix\nposix.{n}(x)")] == []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.system('x')",
        "import os as o\no.system('x')",
        "import os.path\nos.system('x')",
        "from os import execvp as launch\nlaunch('x', ['x'])",
        "from os import *\nexecvp('x', ['x'])",
        "from posix import posix_spawn",
        "import nt\nnt.system('x')",
        "import pty\npty.spawn(['x'])",
        "from pty import spawn",
        "import os\ngetattr(os, 'spawnv')(os.P_WAIT, p, a)",
        "import os\ngetattr(os, 'posix_' + 'spawn')(p, a, e)",
        "import os\n(lambda m: m.system('x'))(os)",
        "from shutil import os\nos.execvp('x', ['x'])",
        "import shutil\nshutil.os.system('x')",
        "import shutil\nshutil.posix.system('x')",
        "import shutil\nshutil.nt.system('x')",
        "import tty\ntty.pty.spawn(['x'])",
        "import shutil\nshutil.builtins.exec(text)",
        "import shutil\nshutil.__dict__[name].system('x')",
        "import shutil\nshutil.__builtins__['exec'](text)",
        "import sys\nsys.modules[name].system('x')",
        "from sys import modules",
        "def f():\n    pass\nf.__globals__[name].system('x')",
        "import subprocess\nsubprocess._fork_exec(args, exe)",
        "import subprocess as sp\nsp._fork_exec(args, exe)",
        "from subprocess import _fork_exec",
        "from .compat import _fork_exec",
        "from subprocess import _posixsubprocess",
        "import subprocess\ngetattr(subprocess, '_fork' + '_exec')(args, exe)",
        "import subprocess\nsubprocess._posixsubprocess.fork_exec(args)",
        "import asyncio\nasyncio.subprocess.subprocess._fork_exec(args, exe)",
        "import asyncio\ngetattr(asyncio.subprocess.subprocess, '_fork_exec')(args, exe)",
        "import asyncio\ngetattr(asyncio.subprocess.subprocess, '_posixsubprocess')",
        "import _posixsubprocess",
        "from _posixsubprocess import fork_exec",
        "import importlib\nimportlib.import_module('_posixsubprocess')",
        "import importlib\nimportlib.import_module('o' + 's').system('x')",
        "from importlib import import_module\nimport_module('o' + 's').system('x')",
        "__import__('subprocess').execvp('x', ['x'])",
        "import builtins\nbuiltins.__import__('subprocess')",
        "import builtins\ngetattr(builtins, 'ex' + 'ec')(text)",
        "import builtins\nbuiltins.exec(text)",
        "from builtins import eval",
        "from builtins import compile",
        "from builtins import __import__ as load\nload('subprocess').system('x')",
        "__builtins__['exec'](text)",
        "exec(text)",
        "eval(text)",
        "compile(text, 'x', 'exec')",
        "import ctypes\nctypes.CDLL(None).system(b'x')",
        "from ctypes import CDLL\nCDLL(None).system(b'x')",
        "import ctypes.util",
        "import _ctypes",
        "import cffi",
        "import multiprocessing\nmultiprocessing.set_executable(p)",
        "from multiprocessing import get_context",
        "import _winapi",
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()",
        "import os\nos.startfile(p)",
        "proc.execv(p, a)",
        "getattr(proc, 'execvp')('x', ['x'])",
        "from helpers import spawnv",
        "import importlib\nimportlib.import_module('ctypes')",
        "lib.ctypes.CDLL(None)",
        "import _multiprocessing",
        "helpers.fork_exec(args, exe)",
        "import importlib\nimportlib.import_module('ctypes.util')",
        "import importlib\nimportlib.import_module('subprocess').system('x')",
        "import os\ngetattr(os, 'system')('x')",
        "import os\ngetattr(os, '__spec__')[name]('x', ['x'])",
        "import os\nos.__getattribute__(name)('x', ['x'])",
        "import os\nvars(os)[name]('x', ['x'])",
        "import os\nlaunch = os\nlaunch.system('x')",
        "from shutil import os\nos.system('x')",
        "from shutil import os as o\no.system('x')",
        "import importlib\nload = importlib.import_module\nload('ct' + 'ypes').CDLL(None).system(b'x')",
        "from importlib import import_module\nhand_on(import_module)",
        "load = __import__",
        "import importlib\ngetattr(importlib, 'import' + '_module')",
        "import importlib\ngetattr(importlib, 'import_module')(name)",
        "import pickle\npickle.loads(data)",
        "import _pickle",
        "from marshal import loads",
        "import shelve",
        "import runpy\nrunpy.run_path(p)",
        "import zipimport",
        "import sys\nsys.path.insert(0, p)",
        "import sys\nsys.path.append(p)",
        "import sys\nsys.path += [p]",
        "import sys\nsys.path[0] = p",
        "import sys\nsys.path = [p]",
        "import sys\npaths = sys.path",
        "import sys\nlist(sys.path)",
        "import sys\ngetattr(sys, 'path').insert(0, p)",
        "import sys\ngetattr(sys, 'path').append(p)",
        "import sys\npaths = getattr(sys, 'path')",
        "from hub import __path__ as package_paths\npackage_paths.insert(0, p)",
        "from hub import __path__",
        "from . import __path__ as paths",
        "from sys import path",
        "from sys import path as p",
        "import sys\nsys.meta_path.append(finder)",
        "import sys\nsys.path_hooks",
        "hub.__path__.append(p)",
        "hub.__path__ = [p]",
        "__path__.append(p)",
        "__path__ = [p]",
        "paths = __path__",
        "getattr(hub, '__path__')",
        "paths = getattr(hub, '__path__')",
        "setattr(hub, '__path__', [p])",
        "delattr(hub, '__path__')",
        "ns['__path__'].append(p)",
        "name = '__path__'",
        "f'{p}__path__'",
        "import importlib.util\nimportlib.util.spec_from_file_location(n, p)",
        "from importlib.util import module_from_spec",
        "import importlib.machinery",
        "from importlib.machinery import SourceFileLoader",
        "loader.exec_module(m)",
        "importlib.machinery.PathFinder.find_spec(n)",
        "import pydoc\npydoc.locate(name)('x')",
        "from pkgutil import resolve_name",
        "from importlib.metadata import EntryPoint\nEntryPoint(name='p', value=target, group='g').load()()",
        "from importlib.metadata import entry_points\nfor ep in entry_points(group='g'):\n    ep.load()",
        "import importlib.metadata\nimportlib.metadata.entry_points()",
        "import importlib\nimportlib._bootstrap._gcd_import('site').addsitedir(p)",
        "import importlib\ngetattr(importlib, '_bootstrap')",
        "from importlib import _bootstrap",
        "import importlib._bootstrap as b\nb.anything",
        "import _frozen_importlib",
        "machinery._find_and_load('x', importer)",
        "import site\nsite.addsitedir(p)",
        "from site import addsitedir",
        "import importlib\nimportlib.import_module('site')",
        "plugins.iter_entry_points('g')",
        "dist.load_entry_point('g', 'n')",
        "import pkg_resources",
        "from pluggy import PluginManager\nPluginManager('lion1').load_setuptools_entrypoints('lion1')",
        "manager.load_setuptools_entrypoints('lion1')",
        "getattr(manager, 'load_setuptools_entrypoints')('lion1')",
        "metadata.EntryPoints(points)",
        "registry.entry_points_for('g')",
        "import pluggy",
        "from stevedore import ExtensionManager\nExtensionManager('lion1', invoke_on_load=True)",
        "import yaml\nyaml.unsafe_load(payload)",
        "from yaml import unsafe_load_all",
        "yaml.load(payload, Loader=yaml.UnsafeLoader)",
        "yaml.load(payload, Loader=yaml.FullLoader)",
        "yaml.full_load(payload)",
        "yaml.full_load_all(stream)",
        "np.load(path, allow_pickle=True)",
        "np.load(path, **{'allow_pickle': True})",
        "np.ctypeslib.load_library('libx', '.')",
        "from http.server import CGIHTTPRequestHandler",
        "from pydantic import ImportString",
        "from pydantic.v1.utils import import_string",
        "from uvicorn.importer import import_from_string",
        "def resolve(module, target):\n    return module.import_from_string(target)",
        "import uvicorn.importer",
        "import uvicorn\nload = uvicorn.importer",
        "typing.ForwardRef(text)",
        "from typing import get_type_hints\n"
        "def f(p: \"__import__('os').system('x')\"):\n    pass\nget_type_hints(f)",
        "def f(p: \"__import__('os').system('x')\"):\n    pass",
        "import os\ndef f(p: 'os.system(\"x\")'):\n    pass",
        "import typing\ntyping.List[\"__import__('os').system('x')\"]",
        "x = \"exec('y')\"",
        "x = \"getattr(__builtins__, 'ex' + 'ec')\"",
        "from importlib import import_module\nx = \"import_module('o' + 's')\"",
        "import typing\ntyping.get_type_hints(f)",
        "inspect.get_annotations(f, **options)",
        "from inspect import get_annotations",
        "inspect.signature(f, **options)",
        "inspect.Signature.from_callable(f, **options)",
        "hints(f, eval_str=True)",
        "ref._evaluate(g, l, frozenset())",
        "typing.evaluate_forward_ref(ref)",
        "typing._eval_type(ref, g, l)",
        "loader.get_code(name)",
        "importlib.abc.InspectLoader.source_to_code(text)",
        "x = f\"__import__('os').system('x')\"",
        "x = f\"{y}\" f\"\\n__import__('os').system('x')\"",
        "x = b\"__import__('os').system('x')\"",
        "from pydantic import BaseModel\nclass M(BaseModel):\n    v: \"__import__('os').system('x')\"",
        "from pydantic._internal._typing_extra import eval_type_backport",
        "_typing_extra.try_eval_type(value, g, l)",
        "_typing_extra.eval_type_lenient(value, g, l)",
        "pydantic.v1.typing.evaluate_forwardref(ref, g, l)",
        "_typing_extra.get_cls_type_hints(cls)",
        "_typing_extra.get_function_type_hints(f)",
        "from pydantic.v1.typing import resolve_annotations",
        "Model.update_forward_refs()",
        "_model_construction.__try_update_forward_refs__()",
        "Model.model_rebuild()",
        "_fields.rebuild_model_fields(cls, ns_resolver=r, typevars_map={})",
        "parse.load_str_bytes(data, proto=proto)",
        "import uvicorn\nuvicorn.run(app, workers=2)",
        "from uvicorn import main",
        "from uvicorn.supervisors import Multiprocess",
        "import uvicorn\nuvicorn.supervisors.ChangeReload(config, target, sockets)",
        "import pdb",
        "import bdb",
        "import cProfile",
        "import timeit\ntimeit.timeit(text)",
        "import doctest",
        "import codeop",
        "import code\ncode.InteractiveInterpreter()",
        "import profile",
        "import trace",
        "import logging.config",
        "from logging import config",
        "import importlib\nimportlib.import_module('pickle')",
        "spec.loader.exec_module(module)",
        "spec.loader.load_module(name)",
        "interpreter.runsource(text)",
        "interpreter.runcode(code_object)",
        "kinds.CodeType(*parts)",
        "globals()[name].system('x')",
        "locals()[name]",
        "vars()[name]",
        "frame.f_globals[name]",
        "frame.f_locals[name]",
        "frame.f_builtins[name]",
        "f.__code__.replace(co_code=data)",
        "import sys\ngetattr(sys, 'modules')['os'].system('x')",
        "import shutil\ngetattr(shutil, 'os').system('x')",
        "import sys\nvars(sys)['modules']",
        "import shutil\nvars(shutil)['posix'].system('x')",
        "getattr(run, '__globals__')",
        "getattr(frame, 'f_globals')",
        "operator.attrgetter('builtins')(shutil)",
        "import importlib.util\nimportlib.util.find_spec(name)",
        "import importlib.util\nimportlib.util.find_spec(name=name)",
        "from importlib.util import *",
        "from importlib.util import *\nfind_spec(*['x.y'])",
        "import importlib\nimportlib.import_module(**spec)",
        "import importlib\nimportlib.import_module(name='ctypes.util')",
        "from importlib.util import find_spec\nfind_spec(name)",
        "from importlib.util import find_spec as module_probe",
        "from importlib import import_module as load",
        "from hub.reexport import find_spec as probe",
        # refused whatever `hub.helpers` binds under that name, an importer or a function of its own
        "from hub.helpers import find_spec as find_plugin_spec\nfind_plugin_spec('plugins')",
        "from hub.reexport import *",
        "from . import *",
        "from hub.reexport import find_spec\nfind_spec(*['x.y'])",
        "import importlib.util\nlook = importlib.util.find_spec",
        "import importlib.util\ngetattr(importlib.util, 'find_spec')('x.y')",
        "import importlib.resources",
        "from importlib import resources",
        "from importlib.resources import files",
        "import importlib\nimportlib.resources.files('pkg')",
        "import importlib_resources",
    ],
)
def test_the_source_check_reads_every_launch_past_subprocess(source):
    assert _launches_past_subprocess(source), source


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['x'])",
        "import os\nos.path.exists(p)\nos.environ.get('PATH')",
        "import os.path as osp\nosp.execvp = None",
        "from os import path\npath.exists(p)",
        "import os\nhasattr(os, 'pidfd_open')",
        "import os\ngetattr(os, 'environ')",
        "import os\ndel os",
        "import asyncio\nasyncio.subprocess.PIPE",
        "from asyncio import subprocess\nsubprocess.PIPE",
        "import importlib\nimportlib.import_module('json')",
        "import re\nre.compile('x')",
        "import platform\nplatform.system()",
        "def execvp(*a):\n    pass\nexecvp('x', ['x'])",
        "getattr(request, name)",
        "# os.execvp('x', ['x'])",
        "from concurrent.futures import ThreadPoolExecutor",
        "x.execute(q)",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "loop.subprocess_exec(factory, 'x')",
        "proc.execv = None",
        "import os.path as osp\nosp.system = None",
        "from hub import code\ncode.Tree()",
        "from .code import Edit",
        "from .ctypes import CDLL",
        "result.code\nargs.profile\nspan.trace",
        "{'code': 1, 'profile': 'p', 'trace': 't'}",
        "import importlib.util\nimportlib.util.find_spec('x')",
        "from importlib.util import find_spec\nfind_spec('x.y')",
        "import importlib.util\nimportlib.util.find_spec(name='x')",
        "from importlib.util import find_spec\nfind_spec(name='x.y')",
        "from hub.reexport import find_spec\nfind_spec('x.y')",
        "from importlib import import_module as import_module\nimport_module('json')",
        "pack.resources\nfiles = []",
        "from importlib.metadata import version",
        "page.site = site\nrow = {'site': s}",
        "import logging\nlogging.getLogger(__name__)",
        "vars(record)",
        "path = 'modules/os.py'\nkind = 'osx'",
        "entry = rows[0]\npoint = entry.point\nentries = [entry]\nnote = 'entry_points.txt'",
        "import uvicorn\nconfig = uvicorn.Config(app, http='h11')\nclass Server(uvicorn.Server): pass",
        "np.load(path)\nnp.concatenate(chunks)",
        "yaml.safe_load(text)",
        "loader = load\nreloaded = False\nrun(app)",
        "import subprocess\nx = \"subprocess.run(['x'])\"",
        "def f(p: 'int') -> 'list[str]':\n    pass",
        "import typing\ntyping.List['Node']",
        "x = 'a + b'\ny = 'Run the thing.'",
        "x = '\\ud800'",
        "x = f'{a}-{b}'\ny = b'payload'",
        "x = b'\\xff\\xfe'",
        "index.rebuild()\nhints = model_fields",
        "import sys\nfor p in sys.path:\n    pass",
        "import sys\nfirst = sys.path[0]",
        "import sys\nif p in sys.path:\n    pass",
        "import sys\nrows = [p for p in sys.path]",
        "import sys\nfor p in getattr(sys, 'path'):\n    pass",
        "import sys\nfirst = getattr(sys, 'path')[0]",
        "for p in hub.__path__:\n    pass",
        "import hub\nIS_PACKAGE = hasattr(hub, '__path__')",
        "for p in getattr(hub, '__path__'):\n    pass",
        "first = getattr(hub, '__path__')[0]",
        "name == '__path__'",
        "import sys\nsys.exit(1)\nsys.stderr.write(x)\nsys.argv[1:]",
        "import importlib.util\nimportlib.util.find_spec('hub')",
    ],
)
def test_the_source_check_passes_what_stays_on_subprocess(source):
    assert _launches_past_subprocess(source) == [], source


def test_the_census_boundary_is_named_calls_as_written():
    # the line the census holds (docs/adr/027-suite): a launch in text the code builds while it runs is
    # caught when a call the census names evaluates it, and passes when an evaluator it does not name
    # does, here `functools.singledispatch` registering a function by its annotation. A change to the
    # census that moves the line either way turns this red, and the record moves with it
    codes = tuple(map(ord, "__import__('os').system('true')"))
    source = (
        "import functools\nimport typing\n"
        "@functools.singledispatch\ndef handle(value):\n    return value\n"
        "def register():\n    def impl(value):\n        return value\n"
        f"    impl.__annotations__ = {{'value': ''.join(map(chr, {codes}))}}\n"
    )
    assert _launches_past_subprocess(source + "    handle.register(impl)\n") == []
    assert _launches_past_subprocess(source + "    typing.get_type_hints(impl)\n") == [".get_type_hints"]


def test_the_census_reads_every_module_imported_by_name():
    source = (
        "import yaml.cyaml\nfrom ruamel.yaml import YAML\nimport_module('dill')\n"
        "importlib.import_module('jsonpickle.ext')\n__import__('cloudpickle')\n"
        "from . import sibling\nfrom .pkg import thing\nimport_module(name)\n"
        "importlib.util.find_spec('msgpack.ext')\nT = \"__import__('ujson').X\"\n"
    )
    expected = {"yaml", "ruamel", "dill", "jsonpickle", "cloudpickle", "msgpack", "ujson"}
    assert _imported_modules(source) == expected | {"<a computed name handed to import_module>"}
    assert not _imported_modules(source) & _IMPORTED
    # an importer under the name a `from importlib` import binds it to imports by name all the same
    # and the binding under another name is pinned, from whichever module it is imported
    aliased = "from importlib import import_module as load\nload('execnet.gateway')\n"
    assert _imported_modules(aliased) == {"importlib", "execnet", "<import_module imported as load>"}
    looked = "from importlib.util import find_spec as look\nlook('orjson.x')\n"
    assert _imported_modules(looked) == {"importlib", "orjson", "<find_spec imported as look>"}
    reexported = "from hub.reexport import find_spec as probe\nprobe(*['orjson.x'])\n"
    assert _imported_modules(reexported) == {
        "hub",
        "<find_spec imported as probe>",
        "<a computed name handed to find_spec>",
    }
    assert _imported_modules("from hub.reexport import find_spec\nfind_spec('orjson.x')\n") == {
        "hub",
        "orjson",
    }
    # a star import binds names the census does not read, so it is pinned whatever module it names
    assert _imported_modules("from hub.reexport import *\nprobe('orjson.x')\n") == {
        "hub",
        "<a star import from hub.reexport>",
    }
    assert _imported_modules("from . import *\n") == {"<a star import from .>"}
    # a lookup of a top-level name imports nothing; a relative name is pinned as written
    assert _imported_modules("importlib.util.find_spec('simplejson')\n") == set()
    relative = "importlib.import_module('.sibling', 'pkg')\nimportlib.util.find_spec('.other', __package__)\n"
    assert _imported_modules(relative) == {".sibling", ".other"}
    # a name handed as `name=` is read as a first argument is, whatever keyword comes before it
    keyword = (
        "import_module(name='execnet')\n__import__(name='dill.x')\n"
        "importlib.util.find_spec(name='orjson.x')\nimportlib.import_module(package='pkg', name='.kw')\n"
    )
    assert _imported_modules(keyword) == {"execnet", "dill", "orjson", ".kw"}
    assert _imported_modules("importlib.util.find_spec(name='simplejson')\n") == set()
    # a name it cannot read, starred or computed, is pinned as that, under any binding of the importer
    starred = "from importlib.util import *\nfind_spec(*['orjson.x'])\n"
    assert _imported_modules(starred) == {
        "importlib",
        "<a star import from importlib.util>",
        "<a computed name handed to find_spec>",
    }


def test_a_lookup_of_a_submodule_runs_the_package_above_it(tmp_path, monkeypatch):
    # why `find_spec` is an importer to the census: the package above the name it looks up is imported
    (tmp_path / "census_lookup_parent").mkdir()
    (tmp_path / "census_lookup_parent" / "__init__.py").write_text("RAN = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert importlib.util.find_spec("census_lookup_parent.missing") is None
        assert sys.modules["census_lookup_parent"].RAN
    finally:
        sys.modules.pop("census_lookup_parent", None)
    source = "import importlib.util\nimportlib.util.find_spec('census_lookup_parent.missing')\n"
    assert _imported_modules(source) == {"importlib", "census_lookup_parent"}
    keyword = "import importlib.util\nimportlib.util.find_spec(name='census_lookup_parent.missing')\n"
    assert _imported_modules(keyword) == {"importlib", "census_lookup_parent"}


def test_a_module_inside_a_package_is_not_the_library_it_shares_a_name_with(tmp_path):
    for path in ("pkg/__init__.py", "pkg/tools/__init__.py", "pkg/tools/daytona.py", "scripts/run.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("")
    assert _own_modules(sorted(tmp_path.rglob("*.py"))) == {"pkg", "run"}


def test_a_text_that_changes_where_a_later_word_resolves_or_reads_text_again_is_refused_whole(
    elsewhere, tmp_path, refused_launches
):
    # past a `cd`, a PATH assignment that is not a leading one, an `eval`, a sourced file or another
    # shell, this reading no longer knows what a later word names: each text is refused whole, before
    # any word is looked up (the alias is a name the guard would pass, in a directory the text reaches)
    program, ran = _program(elsewhere, "codex")
    os.symlink(program, elsewhere / "codex-alias")
    env = {"PATH": "/usr/bin:/bin"}
    texts = [
        f"true; PATH={elsewhere} codex-alias",
        f"true; HOME={elsewhere} ./codex-alias",
        f"true; CDPATH={elsewhere} ./codex-alias",
        f"cd {elsewhere}; ./codex-alias",
        f"true && cd {elsewhere} && ./codex-alias",
        f"pushd {elsewhere}; ./codex-alias",
        f"pushd {elsewhere} && popd; {elsewhere}/codex-alias",
        "eval printf x",
        f". {elsewhere}/codex-alias",
        f"source {elsewhere}/codex-alias",
        f"unset PATH; {elsewhere}/codex-alias",
        f"sh -c '{elsewhere}/codex-alias'",
        f"/bin/bash -c printf; {elsewhere}/codex-alias",
        f"zsh -c '{elsewhere}/codex-alias'",
        f"dash -c '{elsewhere}/codex-alias'",
        f"ksh -c '{elsewhere}/codex-alias'",
        "shopt -s extglob; printf x",
        "setopt extendedglob; printf x",
        # the variable named bare, or as an assignment's whole value, to a builtin that assigns it
        "read PATH; codex-alias",
        f"for PATH in {elsewhere}; do codex-alias; done",
        f"printf -v PATH {elsewhere}; codex-alias",
        f"declare -n r=PATH; r={elsewhere}; codex-alias",
        f"getopts a HOME; {elsewhere}/codex-alias",
        # quoted or not: at a command's head `'cd'` is still the builtin, and this reading does not
        # place commands, so the word is refused as an argument too
        "printf '%s' 'cd'",
        f"'cd' {elsewhere}; ./codex-alias",
    ]
    for text in texts:
        with pytest.raises(
            FileNotFoundError, match="changes where a later word resolves or reads text again"
        ):
            subprocess.run(text, shell=True, cwd=tmp_path, env=env)
    assert not ran.exists()
    refused_launches.expect(texts)
    # the name inside a longer word is an argument
    done = subprocess.run("printf '%s' PATHS a=PATHx", shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "PATHSa=PATHx" and refused_launches == []
    # a `#` inside a word is the word's, not a comment (measured: `printf x#y` prints `x#y`), so a
    # second command after one is read: refused by its program, nothing started
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run("printf '%s' x#y; codex", shell=True, env=env)
    assert not ran.exists()
    refused_launches.expect(["codex"])
    done = subprocess.run("printf '%s' x#y", shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "x#y" and refused_launches == []
    # so is a `#` after an assignment's `=` or a `:` in its value: the PATH it begins is read, and the
    # program found there is refused by what it is; a text that starts nothing guarded runs
    os.mkdir(elsewhere / "#literal")
    os.symlink(program, elsewhere / "#literal" / "codex-alias")
    texts = ["PATH=#literal codex-alias", "PATH=/usr/bin:#literal codex-alias"]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
            subprocess.run(text, shell=True, cwd=elsewhere, env=env)
    assert not ran.exists()
    refused_launches.expect(["codex-alias", "codex-alias"])
    done = subprocess.run("X=#a Y=b:#c /usr/bin/env", shell=True, env=env, capture_output=True, text=True)
    assert {"X=#a", "Y=b:#c"} <= set(done.stdout.splitlines()) and refused_launches == []
    # a builtin is the bare word: a stand-in named `cd`, given by its path, is the file there and starts
    stand_in, stand_in_ran = _program(tmp_path, "cd")
    subprocess.run(f"{stand_in}", shell=True, env=env)
    assert stand_in_ran.exists() and refused_launches == []
    # a shell named by its path reads the text again all the same
    with pytest.raises(FileNotFoundError, match="'/bin/sh' changes where a later word"):
        subprocess.run(f"/bin/sh -c {stand_in}", shell=True, env=env)
    refused_launches.expect([f"/bin/sh -c {stand_in}"])
    # a leading `PATH=` is its own command's: a second command resolves on the child's PATH, where the
    # alias is, so the text is refused whole rather than read on the assignment's PATH, where it is not
    env = {"PATH": f"{elsewhere}{os.pathsep}/usr/bin:/bin"}
    texts = [
        "PATH=/usr/bin:/bin true; codex-alias",
        "PATH=/usr/bin:/bin true && codex-alias",
        "PATH=/usr/bin:/bin false || codex-alias",
        "PATH=/usr/bin:/bin true | codex-alias",
        "PATH=/usr/bin:/bin true & codex-alias",
        "PATH=/usr/bin:/bin (true); codex-alias",
        "PATH=/usr/bin:/bin true\ncodex-alias",
        'PATH=/usr/bin:/bin printf ";"; codex-alias',
        # an escaped `>` or `<` is an argument, so the `|` or `&` after it starts a second command
        "PATH=/usr/bin:/bin printf x \\>|codex-alias",
        "PATH=/usr/bin:/bin printf x \\<&codex-alias",
    ]
    for text in texts:
        with pytest.raises(FileNotFoundError, match="'PATH=/usr/bin:/bin' changes where a later word"):
            subprocess.run(text, shell=True, cwd=tmp_path, env=env)
    assert not ran.exists()
    refused_launches.expect(texts)
    # a redirection does not end the command, nor does a quoted or escaped operator, which is an
    # argument: the assignment's PATH is still the one the word resolves on
    for text, out in (
        ("PATH=/usr/bin:/bin printf x 2>&1", "x"),
        (f"PATH=/usr/bin:/bin printf x >| {tmp_path / 'clobbered'}", ""),
        ("PATH=/usr/bin:/bin printf '%s%s%s%s' ';' '|&' \"(\" '&&'", ";|&(&&"),
        ("PATH=/usr/bin:/bin printf '%s' \\;", ";"),
        ("PATH=/usr/bin:/bin printf '%s' \\>", ">"),
    ):
        done = subprocess.run(text, shell=True, env=env, capture_output=True, text=True)
        assert done.stdout == out and refused_launches == [], text
    assert (tmp_path / "clobbered").read_text() == "x"
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(f"PATH={elsewhere} codex-alias >/dev/null", shell=True, cwd=tmp_path, env=env)
    refused_launches.expect(["codex-alias"])
    # the words that start, with the same alias on the leading PATH, are refused by name as before
    env = {"PATH": "/usr/bin:/bin"}
    with pytest.raises(FileNotFoundError, match="refused to start 'codex'"):
        subprocess.run(f"PATH={elsewhere} codex-alias", shell=True, cwd=tmp_path, env=env)
    refused_launches.expect(["codex-alias"])
    # (a word that only holds one of these names is a word like any other)
    done = subprocess.run("printf x-cd; printf evalx", shell=True, env=env, capture_output=True, text=True)
    assert done.stdout == "x-cdevalx" and refused_launches == []


def test_a_shell_command_whose_executable_is_not_sh_bash_or_dash_is_refused_whole(
    elsewhere, tmp_path, refused_launches
):
    # zsh expands `^z` under `extendedglob` and `=name` by default, ksh `@(...)`, csh its history: none
    # of which this reading of /bin/sh's text sees, so the text is refused whole before the shell starts
    program, ran = _program(elsewhere, "codex")
    env = {"PATH": f"{elsewhere}{os.pathsep}/usr/bin:/bin"}
    texts = ["setopt extendedglob; ^z", "true", "=codex"]
    for shell in ("/bin/zsh", "/bin/ksh", "/bin/mksh", "/bin/csh", "/bin/tcsh", "/usr/bin/fish"):
        for text in texts:
            with pytest.raises(FileNotFoundError, match=f"{shell}' changes where a later word"):
                subprocess.run(text, shell=True, executable=shell, cwd=elsewhere, env=env)
        assert not ran.exists()
        refused_launches.expect(texts)

    async def through_asyncio():
        await asyncio.create_subprocess_shell(texts[0], executable="/bin/zsh", cwd=elsewhere, env=env)

    with pytest.raises(FileNotFoundError, match="/bin/zsh' changes where a later word"):
        asyncio.run(through_asyncio())
    assert not ran.exists()
    refused_launches.expect([texts[0]])
    # the shell is known by the file its name leads to, links resolved, wherever the link lies: a
    # link under another name, outside or under the session's temporary directory, is still zsh
    (elsewhere / "my-shell").symlink_to("/bin/zsh")
    (tmp_path / "zsh-link").symlink_to("/bin/zsh")
    for link in (elsewhere / "my-shell", tmp_path / "zsh-link"):
        with pytest.raises(FileNotFoundError, match=f"{link}' changes where a later word"):
            subprocess.run("=codex", shell=True, executable=str(link), cwd=elsewhere, env=env)
        assert not ran.exists()
        refused_launches.expect(["=codex"])
    # and both names must be listed: a link named `bash` that leads to zsh is zsh
    (elsewhere / "listed").mkdir()
    (elsewhere / "listed" / "bash").symlink_to("/bin/zsh")
    for text in ("true", "=codex"):
        with pytest.raises(FileNotFoundError, match="listed/bash' changes where a later word"):
            subprocess.run(
                text, shell=True, executable=str(elsewhere / "listed" / "bash"), cwd=elsewhere, env=env
            )
        assert not ran.exists()
        refused_launches.expect([text])
    # a bare name is looked up on the child's PATH, where it leads to the same file
    with pytest.raises(FileNotFoundError, match="'my-shell' changes where a later word"):
        subprocess.run("=codex", shell=True, executable="my-shell", cwd=elsewhere, env=env)
    assert not ran.exists()
    refused_launches.expect(["=codex"])
    # a relative PATH entry is searched from the child's working directory, not pytest's
    with pytest.raises(FileNotFoundError, match="'my-shell' changes where a later word"):
        subprocess.run(
            "=codex", shell=True, executable="my-shell", cwd=elsewhere, env={"PATH": ".:/usr/bin:/bin"}
        )
    assert not ran.exists()
    refused_launches.expect(["=codex"])
    # the executable as a PathLike or as bytes leads to the same file
    for exe in (elsewhere / "my-shell", os.fsencode(elsewhere / "my-shell")):
        with pytest.raises(FileNotFoundError, match="my-shell' changes where a later word"):
            subprocess.run("=codex", shell=True, executable=exe, cwd=elsewhere, env=env)
        assert not ran.exists()
        refused_launches.expect(["=codex"])
    # a file under the session's temporary directory is a stand-in the test wrote, whatever its name,
    # as a str or a PathLike
    stand_in, stand_in_ran = _program(tmp_path, "zsh")
    subprocess.run("=codex", shell=True, executable=str(stand_in), cwd=elsewhere, env=env)
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []
    stand_in, stand_in_ran = _program(tmp_path, "ksh")
    subprocess.run("=codex", shell=True, executable=stand_in, cwd=elsewhere, env=env)
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []
    # an executable outside the directory under a name this reading does not know is refused whole,
    # whatever its text: which expansions it makes cannot be read without running it, and a shell
    # copied under a name of its own (zsh as `zsh-copy`) expands `=codex` to the program
    (copy := elsewhere / "zsh-copy").write_text('#!/bin/sh\nexec /bin/bash "$@"\n')
    copy.chmod(0o755)
    for text in ("=codex", "true"):
        with pytest.raises(FileNotFoundError, match="zsh-copy' changes where a later word"):
            subprocess.run(text, shell=True, executable=str(copy), cwd=elsewhere, env=env)
    assert not ran.exists()
    refused_launches.expect(["=codex", "true"])
    # the stand-in is the one program the call starts and the text is its argument, so the text is not
    # read as shell text: a bare `$` in it, refused under a shell, starts the stand-in
    stand_in, stand_in_ran = _program(tmp_path, "my-shell")
    text = "echo $CLI"
    with pytest.raises(FileNotFoundError, match="the shell reads before a program is named"):
        subprocess.run(text, shell=True, executable="/bin/bash", cwd=elsewhere, env=env)
    refused_launches.expect([text])
    subprocess.run(text, shell=True, executable=str(stand_in), cwd=elsewhere, env=env)
    assert stand_in_ran.exists() and not ran.exists() and refused_launches == []
    # an environment without PATH is searched on `os.defpath`, as Popen searches it, not on the test's
    # PATH: `sh` is /bin/sh there, whatever the test's PATH leads `sh` to
    (tmp_path / "sh").symlink_to("/bin/zsh")
    with pytest.MonkeyPatch.context() as m:
        m.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
        done = subprocess.run(
            "printf safe", shell=True, executable="sh", env={}, capture_output=True, text=True
        )
    assert done.stdout == "safe" and refused_launches == []
    # /bin/sh's own reading, and bash's and dash's, is the one the scan makes: those start
    for shell in ("/bin/sh", "/bin/bash", "/bin/dash"):
        done = subprocess.run(
            "printf x", shell=True, executable=shell, env=env, capture_output=True, text=True
        )
        assert done.stdout == "x" and refused_launches == []


# a session-scoped fixture's teardown runs after the last test's guard has reset the binding: a launch
# there is refused with nothing declared, and the swallowed refusal fails the session's own teardown
_TEARDOWN_CONFTEST = """
import os
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def launches_after_the_last_test():
    yield
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass
"""


def test_a_launch_from_a_wider_scoped_fixtures_teardown_fails_the_run_after_the_last_test(tmp_path):
    done, program, ran = _inner_suite(
        tmp_path, {"conftest.py": _TEARDOWN_CONFTEST, "test_probe.py": _WIDER_SCOPE_PROBE}
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert f"started a program no test declared (after the last test): {program}" in said, said
    assert not ran.exists()


# a thread the last test leaves running launches after the test's guard has reset the binding and
# after the session's other fixtures are gone: the session guard waits for it before reading the ledger
_LATE_THREAD_PROBE = """
import os
import subprocess
import threading
import time


def test_leaves_a_thread_running():
    def launch_later():
        time.sleep(1)
        try:
            subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
        except FileNotFoundError:
            pass

    threading.Thread(target=launch_later).start()
"""


_LATE_POOL_PROBE = """
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor


def test_leaves_a_pool_worker_running():
    def launch_later():
        time.sleep(1)
        try:
            subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
        except FileNotFoundError:
            pass

    ThreadPoolExecutor(1).submit(launch_later)
"""


# a thread the wait is joining starts another, which launches after the first is gone: the wait reads
# the running threads again after each join
_LATE_NESTED_PROBE = """
import os
import subprocess
import threading
import time


def test_leaves_a_thread_that_starts_another():
    def launch_later():
        time.sleep(1)
        try:
            subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
        except FileNotFoundError:
            pass

    def start_another():
        time.sleep(0.5)
        threading.Thread(target=launch_later).start()

    threading.Thread(target=start_another).start()
"""


@pytest.mark.parametrize(
    "probe", [_LATE_THREAD_PROBE, _LATE_POOL_PROBE, _LATE_NESTED_PROBE], ids=["thread", "pool", "nested"]
)
def test_a_launch_from_a_thread_the_last_test_left_running_fails_the_run(tmp_path, probe):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": probe})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert f"started a program no test declared (after the last test): {program}" in said, said
    assert not ran.exists()


# a hook past the session's teardown launches: refused with nothing declared, and the swallowed refusal
# fails the run at `pytest_unconfigure`, past every report
_UNCONFIGURE_CONFTEST = """
import os
import subprocess


def pytest_unconfigure(config):
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass
"""


def test_a_launch_from_a_hook_past_the_sessions_teardown_fails_the_run(tmp_path):
    done, program, ran = _inner_suite(
        tmp_path, {"conftest.py": _UNCONFIGURE_CONFTEST, "test_probe.py": _WIDER_SCOPE_PROBE}
    )
    said = done.stdout + done.stderr
    assert done.returncode != 0, said
    assert "1 passed" in said, said
    assert "a hook past the session's teardown started a program no test declared" in said, said
    assert f"(at pytest_unconfigure): {program}" in said, said
    assert not ran.exists()


# a plugin registered after the guard's: its `pytest_unconfigure` runs after a plain `trylast` hook of the
# guard's would, and a wrapper of its own marked `tryfirst` runs outside the guard's wrapper too; the
# launch is read all the same, after every hook or at the interpreter's exit
_LATER_PLUGIN = """
import os
import subprocess

import pytest


def _launch():
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    _launch()
"""
_LATER_WRAPPING_PLUGIN = """
import os
import subprocess

import pytest


def _launch():
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config):
    try:
        return (yield)
    finally:
        _launch()
"""


@pytest.mark.parametrize(
    ("plugin", "when"),
    [(_LATER_PLUGIN, "at pytest_unconfigure"), (_LATER_WRAPPING_PLUGIN, "at exit")],
    ids=["trylast", "wrapper"],
)
def test_a_launch_from_a_plugin_registered_after_the_guard_fails_the_run(tmp_path, plugin, when):
    done, program, ran = _inner_suite(
        tmp_path, {"later_plugin.py": plugin, "test_probe.py": _WIDER_SCOPE_PROBE}, later=("later_plugin",)
    )
    said = done.stdout + done.stderr
    assert done.returncode != 0, said
    assert "1 passed" in said, said
    assert (
        f"a hook past the session's teardown started a program no test declared ({when}): {program}" in said
    ), said
    assert not ran.exists()


# a worker an unmarked test leaves waiting, released by a later test marked for the program it then
# starts: the declaration is the later test's, not the worker's, and the launch is refused and fails
# the test that released it
_INHERITED_WORKER_PROBE = """
import os
import subprocess
import threading

import pytest

release = threading.Event()
tried = threading.Event()


def launch():
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass


def work():
    release.wait(30)
    threading.current_thread().name = "renamed"  # the thread, not its name, is what was inherited
    child = threading.Thread(target=launch, name="child-of-inherited")  # started after the bind
    child.start()
    child.join(30)
    launch()
    tried.set()


def test_a_starts_a_waiting_worker():
    threading.Thread(target=work, daemon=True, name="inherited-worker").start()


@pytest.mark.launches_cli("kkernel")
def test_b_releases_it():
    release.set()
    assert tried.wait(30)
"""


# a worker a session-scoped fixture owns, used by a marked test: alive before the test was bound, so
# its launch is refused too, and the refusal names the thread and the rule
_SHARED_WORKER_PROBE = """
import os
import queue
import subprocess
import threading

import pytest

jobs = queue.Queue()


def worker():
    while (job := jobs.get()) is not None:
        try:
            subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
        except FileNotFoundError as e:
            job["error"] = str(e)
        finally:
            job["done"].set()


@pytest.fixture(scope="session", autouse=True)
def shared_worker():
    t = threading.Thread(target=worker, name="shared-worker")
    t.start()
    yield
    jobs.put(None)
    t.join(30)


@pytest.mark.launches_cli("kkernel")
def test_launches_through_the_shared_worker():
    job = {"done": threading.Event()}
    jobs.put(job)
    assert job["done"].wait(30)
    assert "from thread 'shared-worker', alive before this test was bound" in job["error"], job
"""


def test_a_worker_from_an_earlier_test_carries_none_of_a_later_tests_declarations(tmp_path):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": _INHERITED_WORKER_PROBE})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "2 passed, 1 error" in said, said  # the error is test_b's teardown, past its own pass
    assert "ERROR at teardown of test_b_releases_it" in said, said
    assert (
        f"this test started a program it did not declare: {program} (from thread 'renamed', alive before "
        f"this test was bound), {program} (from thread 'renamed', alive before this test was bound)"
    ) in said, said
    assert not ran.exists()


# a worker started past `threading.Thread.start` (`_thread.start_new_thread`): its line is unknown, so it
# is read as inherited whoever started it, and the refusal says so
_RAW_THREAD_PROBE = """
import _thread
import os
import subprocess
import threading

import pytest

release = threading.Event()
tried = threading.Event()


def work():
    release.wait(30)
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass
    finally:
        tried.set()


def test_a_starts_a_raw_worker():
    _thread.start_new_thread(work, ())


@pytest.mark.launches_cli("kkernel")
def test_b_releases_it():
    release.set()
    assert tried.wait(30)
"""


def test_a_worker_started_past_thread_start_is_read_as_inherited(tmp_path):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": _RAW_THREAD_PROBE})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "2 passed, 1 error" in said, said
    assert (
        f"{program} (from thread 'Dummy-1', started past `threading.Thread.start`, so of a line this "
        "reading does not know)"
    ) in said, said
    assert not ran.exists()


def test_a_marked_test_launching_through_a_fixtures_worker_is_refused_naming_the_thread(tmp_path):
    done, program, ran = _inner_suite(tmp_path, {"test_probe.py": _SHARED_WORKER_PROBE})
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert f"{program} (from thread 'shared-worker', alive before this test was bound)" in said, said
    assert not ran.exists()


# a test's own thread begins a launch, blocks in the guard's PATH lookup until the test's teardown has
# run (the plugin releases it after every teardown hook), and swallows the refusal: judged after the
# test was unbound, the launch lands in the suite's ledger and fails the next test's setup
_IN_FLIGHT_PROBE = """
import os
import subprocess
import threading

from probe.release_plugin import launch_done, release_launch


def test_a_leaves_a_launch_in_flight_at_its_teardown(monkeypatch):
    guard = subprocess.Popen._execute_child.__globals__
    which = guard["_which"]
    entered = threading.Event()

    def slow_which(name, path=None):
        if not entered.is_set():
            entered.set()
            release_launch.wait(30)
        return which(name, path=path)

    monkeypatch.setitem(guard, "_which", slow_which)

    def launch_and_swallow():
        try:
            subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
        except FileNotFoundError:
            pass
        finally:
            launch_done.set()

    threading.Thread(target=launch_and_swallow, name="in-flight").start()
    assert entered.wait(30)


def test_b_runs_after_it():
    assert launch_done.is_set()
"""

_RELEASE_PLUGIN = """
import threading

import pytest

release_launch = threading.Event()
launch_done = threading.Event()


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    if item.name == "test_a_leaves_a_launch_in_flight_at_its_teardown":
        release_launch.set()
        assert launch_done.wait(30)
    return result
"""


def test_a_launch_in_flight_at_the_tests_teardown_fails_the_next_tests_setup(tmp_path):
    done, program, ran = _inner_suite(
        tmp_path,
        {"test_probe.py": _IN_FLIGHT_PROBE, "release_plugin.py": _RELEASE_PLUGIN},
        later=("release_plugin",),
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert "ERROR at setup of test_b_runs_after_it" in said, said
    assert (
        f"a thread a test left in flight started a program no test declared (before this test): {program} "
        "(from thread 'in-flight', past its test's teardown)"
    ) in said, said
    assert not ran.exists()


# a module imported before the guard's registers an `atexit` callback that runs after the guard's
# exit-time read; its launch is refused and ends the process there, the exit status as the report
_EARLIER_ATEXIT_PLUGIN = """
import atexit
import os
import subprocess


def _launch():
    try:
        subprocess.run(LAUNCH, check=False, shell=SHELL)
    except FileNotFoundError:
        pass


atexit.register(_launch)
"""


# the launch as a list, refused by name, and as a shell text the scan refuses whole before any name is
# read (`"$PROBE_PROGRAM"` is an expansion): each refusal path past the exit-time read ends the process
@pytest.mark.parametrize(
    ("launch", "shell", "said_as"),
    [
        ('[os.environ["PROBE_PROGRAM"], "mcp"]', "False", "{program}"),
        ("'\"$PROBE_PROGRAM\" mcp'", "True", '"$PROBE_PROGRAM" mcp'),
    ],
    ids=["list", "shell-text"],
)
def test_a_launch_from_an_atexit_callback_registered_before_the_guard_fails_the_run(
    tmp_path, launch, shell, said_as
):
    plugin = _EARLIER_ATEXIT_PLUGIN.replace("LAUNCH", launch).replace("SHELL", shell)
    done, program, ran = _inner_suite(
        tmp_path,
        {"earlier_plugin.py": plugin, "test_probe.py": _WIDER_SCOPE_PROBE},
        earlier=("earlier_plugin",),
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed" in said, said
    started = said_as.format(program=program)
    assert f"a callback at the interpreter's exit started {started!r}, refused." in said, said
    assert not ran.exists()


# ADR-0027 S9: nothing checks the seam past the session's teardown, so a hook there, or an `atexit`
# callback registered before the guard's, that reloads `subprocess` first launches through a class the
# guard never wrapped
_RELOAD_AND_LAUNCH = """
import atexit
import importlib
import os
import subprocess


def _launch(*_):
    importlib.reload(subprocess)
    subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
"""


@pytest.mark.parametrize(
    ("name", "wiring", "earlier"),
    [
        ("conftest.py", "pytest_unconfigure = _launch\n", ()),
        ("earlier_plugin.py", "atexit.register(_launch)\n", ("earlier_plugin",)),
    ],
    ids=["pytest_unconfigure", "atexit"],
)
def test_a_reload_of_subprocess_past_the_sessions_teardown_launches_unjudged(tmp_path, name, wiring, earlier):
    done, _program, ran = _inner_suite(
        tmp_path,
        {name: _RELOAD_AND_LAUNCH + wiring, "test_probe.py": _WIDER_SCOPE_PROBE},
        earlier=earlier,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout + done.stderr
    assert ran.exists()


@pytest.mark.parametrize(
    "reader",
    ["expect", "_fail_on_launches_outside_tests", "_fail_the_process_on_launches_past_teardown"],
)
def test_every_reader_that_empties_a_ledger_takes_the_guards_lock(refused_launches, reader):
    # a refusal a thread records between a ledger's read and its emptying would be erased unread: the
    # test's own ledger (`expect`), and the suite's, read before each test, after the last, and after
    # every `pytest_unconfigure` hook (both empty here; the exit-time read is not called in-process)
    guard = subprocess.Popen._execute_child.__globals__
    read = {
        "expect": lambda: refused_launches.expect([]),
        "_fail_on_launches_outside_tests": lambda: guard[reader]("probe"),
        "_fail_the_process_on_launches_past_teardown": lambda: guard[reader]("probe"),
    }[reader]
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with guard["_guarding"].lock:
            held.set()
            release.wait(30)

    def run_reader():
        read()
        done.set()

    holder = threading.Thread(target=hold, name="lock-holder")
    holder.start()
    assert held.wait(30)
    threading.Thread(target=run_reader, name="reading").start()
    assert not done.wait(1.0), f"{reader} ran while another thread held the guard's lock"
    release.set()
    assert done.wait(30)
    holder.join(30)


@pytest.mark.parametrize(
    "launch_args, launch_kwargs, entry",
    [
        pytest.param(["/usr/bin/true"], {"preexec_fn": lambda: None}, "/usr/bin/true", id="preexec_fn"),
        pytest.param(
            '"$CLI"', {"shell": True, "env": {"CLI": "/usr/bin/true"}}, '"$CLI"', id="bare-expansion"
        ),
        pytest.param("true", {"shell": True, "env": {"BASH_ENV": "/dev/null"}}, "true", id="startup-state"),
        pytest.param("cd /tmp; true", {"shell": True}, "cd /tmp; true", id="resolution-change"),
        pytest.param(
            "true", {"shell": True, "env": {"PATH": "~/x:/usr/bin:/bin"}}, "true", id="tilde-path-entry"
        ),
        pytest.param(["codex"], {"env": {"PATH": "/nonexistent"}}, "codex", id="by-name"),
    ],
)
def test_every_refusal_is_recorded_under_the_guards_lock(refused_launches, launch_args, launch_kwargs, entry):
    # the writer's side, one launch per place a refusal is recorded: all but the by-name refusal are
    # recorded outside the judging block, so `_record`'s own hold is what keeps each from landing
    # between a reader's read of a ledger and its emptying
    guard = subprocess.Popen._execute_child.__globals__
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    raised = []

    def hold():
        with guard["_guarding"].lock:
            held.set()
            release.wait(30)

    def launch():
        try:
            subprocess.run(launch_args, **launch_kwargs)
        except FileNotFoundError as e:
            raised.append(e)
        done.set()

    holder = threading.Thread(target=hold, name="lock-holder")
    holder.start()
    assert held.wait(30)
    threading.Thread(target=launch, name="launching").start()
    finished = done.wait(1.0)
    assert refused_launches == [], "a refusal was recorded while another thread held the guard's lock"
    assert not finished, "a launch was refused while another thread held the guard's lock"
    release.set()
    assert done.wait(30)
    holder.join(30)
    assert len(raised) == 1
    refused_launches.expect([entry])


# a thread still running when the wait is over is named: the suite's own leak, not a quiet pass
_LINGERING_CONFTEST = """
import tests.conftest

tests.conftest._THREAD_WAIT_S = 1.0
"""
_LINGERING_PROBE = """
import threading
import time


def test_leaves_a_thread_running():
    threading.Thread(target=time.sleep, args=(5,), name="lingers").start()
"""


def test_a_thread_still_running_after_the_wait_fails_the_run_naming_it(tmp_path):
    done, program, ran = _inner_suite(
        tmp_path, {"conftest.py": _LINGERING_CONFTEST, "test_probe.py": _LINGERING_PROBE}
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert "a thread is still running 1.0s after the last test: lingers" in said, said


# the CLI suite takes the guard through its own conftest's imports, which this suite's conftest does
# not reach as a plugin: the same teardown launch, under that conftest's text as the probe's, fails the
# run only if the session guard is among what it imports
_CLI_TEARDOWN_PROBE = """
import os
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def launches_after_the_last_test():
    yield
    try:
        subprocess.run([os.environ["PROBE_PROGRAM"], "mcp"], check=False)
    except FileNotFoundError:
        pass


def test_runs_after_the_session_fixture():
    assert os.environ.get("KHIVE_NO_DAEMON") == "1"
"""


def test_the_cli_suites_conftest_carries_the_session_guard_to_its_teardown(tmp_path):
    cli_conftest = (REPO / "apps" / "cli" / "tests" / "conftest.py").read_text()
    done, program, ran = _inner_suite(
        tmp_path, {"conftest.py": cli_conftest, "test_probe.py": _CLI_TEARDOWN_PROBE}, plugin=False
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "1 passed, 1 error" in said, said
    assert f"started a program no test declared (after the last test): {program}" in said, said
    assert not ran.exists()


def test_the_refusal_ledger_is_emptied_only_by_declaring_what_it_holds(elsewhere, refused_launches):
    # a test cannot clear the ledger: it declares the launches it expected, which are checked
    program, ran = _program(elsewhere, "codex")
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(program)], check=False)
    assert not hasattr(refused_launches, "clear") and not hasattr(refused_launches, "append")
    with pytest.raises(AssertionError):
        refused_launches.expect(["something else"])
    assert refused_launches == [str(program)]  # a wrong declaration leaves the ledger as it was
    refused_launches.expect([str(program)])
    assert not ran.exists() and refused_launches == []
