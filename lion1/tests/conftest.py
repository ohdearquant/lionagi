import atexit
import errno
import gc
import inspect
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from hypothesis import settings

# `ci` searches wider and drops the per-example deadline, which a loaded shared runner can miss;
# `deep` is for the scheduled run. Pick one with HYPOTHESIS_PROFILE.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1_000, deadline=None, print_blob=True)
settings.register_profile("deep", max_examples=20_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


# the stub `kkernel`: the lion pack's note verbs over a JSON file beside it, and a refusal of every
# other call. Written by `kkernel_stub` under the test's interpreter.
_STUB_KKERNEL = """\
\"\"\"The stub `kkernel` of the tests: the lion pack answered by `tests/lionfake.py` over a JSON file
beside this script (an empty store at first); any other pack is refused.\"\"\"
import fcntl, json, os, sys
from pathlib import Path

sys.path.insert(0, REPO)
from lion1.khive import KhiveError  # noqa: E402
from tests.lionfake import LionFake  # noqa: E402

actor = sys.argv[sys.argv.index("--expect-actor") + 1]  # one store per actor, as the binary's namespaces
store = Path(__file__).with_name("lion.json")
# one call at a time, as the binary serialises its writers: a server thread reads a resident's notes
# while the test puts them, and a read of a half-written file is a crash the real store never has
with open(store.with_name("lion.lock"), "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    stores = json.loads(store.read_text()) if store.exists() else {}
    fake = LionFake.from_json(stores[actor]) if actor in stores else LionFake()
    try:
        results = [{"ok": True, "tool": "lion", "result": r} for r in fake.answer(sys.argv[-1])]
        status = "success"
    except KhiveError as e:
        results = [{"ok": False, "tool": "lion", "error": {"kind": e.kind, "message": str(e)}}]
        status = "failed"
    stores[actor] = fake.to_json()
    tmp = store.with_name("lion.json.tmp")
    tmp.write_text(json.dumps(stores))
    os.replace(tmp, store)
print(json.dumps({"results": results, "status": status}))
sys.exit(status != "success")
"""


# a test that reaches a real `kkernel` (one on PATH) runs it in-process: `exec` would otherwise start
# a daemon under the test's home that nothing stops, or hand the call to the machine's own. Set here,
# as this module is imported, so a fixture of any scope sees it; a test that means to start the daemon
# deletes it with `monkeypatch`, which puts it back.
os.environ["KHIVE_NO_DAEMON"] = "1"


# The khive binaries and the provider CLIs, by program name. A khive binary leaves a daemon running
# against whatever store its home and working directory lead it to, and a provider CLI spends a real
# model call, so a test starts one only when it says so with @pytest.mark.launches_cli.
_GUARDED_PROGRAMS = frozenset({"kkernel", "khive", "lion1-khive", "claude", "codex"})
_which = shutil.which  # the real lookup, kept from before any test replaces `shutil.which` with its own
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")  # `NAME=` or the appending `NAME+=`
# after one of these a word begins: a `~` there is expanded, a `#` there starts a comment (measured on
# bash and this platform's sh: `(true)#x` is a comment, `printf x >~/out` writes under the home)
_WORD_STARTS = frozenset(" \t\n;|&()<>")


def _bare_expansion(command: str) -> str | None:
    """The first thing in a shell text that the shell reads before a program is named, so what starts is
    not in the text: `$` or a backtick outside single quotes, a glob or brace character (`*`, `?`, `[`,
    `{`) outside quotes, a `~` beginning a bare word (or, in an assignment's value, following a `:`);
    and what this scan cannot read past, since the shell stops reading quotes there and the scan would
    carry a quote state the shell does not: a `#` beginning a bare word (a comment), a bare `<<` (a
    here-document; a `<<<` is a here-string, whose word this scan reads like any other), a quote or a
    backslash left open at the end, a backslash before a newline (the shell deletes the pair before it
    reads a word, so a name split across the two lines is one word to it and two to this reading),
    and an escaped `$` or backtick outside single quotes (inert on this read and live on the next,
    which `eval` or another shell makes). None when there is none: every program the text starts is
    then written in it."""
    # `word_start`: a word begins here, so a `#` is a comment; `tilde`: a `~` here is expanded, which is
    # also true after an assignment's `=` and a `:` in its value, where a `#` is the value's (measured,
    # sh and bash: `X=#a /usr/bin/env` and `X=a:#b /usr/bin/env` run and print the value)
    quote, escaped, word_start, tilde, prev, word = None, False, True, True, "", ""
    skip = -1  # the third `<` of a here-string's `<<<`, read with the first two
    for i, c in enumerate(command):
        if i == skip:
            prev = ""
            continue
        bare = not escaped and quote is None  # an escaped or quoted character is no operator
        if escaped:
            if c == "\n":
                return "\\\n"
            if c in "$`" and quote != "'":  # inert on this read, live on the next (`eval`, another shell)
                return "\\" + c
            escaped, word_start, tilde = False, False, False
        elif quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            escaped = True
        elif quote == '"':
            if c in "$`":
                return c
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote, word_start, tilde = c, False, False
        elif c == "{" and command[i + 1 : i + 2] in (" ", "\t", "\n", ""):
            # a brace group's `{ ` (measured: `{ /usr/bin/true; }` runs, `{a, b}` and `x{ y` print as
            # written; a brace expansion holds no unquoted whitespace)
            word_start, tilde, word = False, False, word + c
        elif c in "$`*?[{" or (c == "#" and word_start) or (c == "~" and tilde):
            return c
        elif c == "<" and prev == "<":
            if command[i + 1 : i + 2] != "<":
                return "<<"
            # a here-string (measured: sh and bash run `cat <<< x`; dash refuses the line, so nothing
            # starts), whose word follows as any redirection's does
            skip, word_start, tilde, word = i + 1, True, True, ""
        elif c in _WORD_STARTS:
            word_start, tilde, word = True, True, ""
        else:  # a `~` is expanded after the `=` of an assignment-shaped word, an argument's included
            # (bash: `x=~`, `x+=~`, not `a-b=~`; sh expands neither, and this reading, which does not
            # tell the shells apart, takes bash's and refuses more under sh), and after a `:` in an
            # assignment's value
            word_start = False
            tilde = (c == "=" and _ENV_ASSIGNMENT.fullmatch(word + "=") is not None) or (
                c == ":" and _ENV_ASSIGNMENT.match(word) is not None
            )
            word += c
        prev = c if bare else ""  # `\<<` and `"<"<` are a `<` redirected from `<` (measured, sh and bash)
    return quote if quote is not None else ("\\" if escaped else None)


# words past which this reading no longer knows where a later word resolves or what it says: a change
# of directory or of PATH past the leading assignments, a word read again as shell text (`eval`, a
# sourced file, another shell). A text holding one is refused whole; the test writes one command.
_RESOLUTION_BUILTINS = frozenset(
    {"cd", "pushd", "popd", "eval", "source", ".", "unset"}
    | {"shopt", "setopt"}  # turn on expansions this scan does not read (`extglob`, `extendedglob`)
)
_RESOLUTION_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})  # by name or by path, both read text
_RESOLUTION_ASSIGNMENT = re.compile(r"(PATH|HOME|CDPATH)\+?=")
# the variable named bare, or as the whole value of an assignment-shaped word: to a builtin that
# assigns it (`read PATH`, `for PATH in`, `printf -v PATH`, `declare -x PATH`, `declare -n r=PATH`)
_RESOLUTION_NAME = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\+?=)?(PATH|HOME|CDPATH)")
# a leading `PATH+=` appends to the child's PATH (bash and this platform's sh alike): the words after
# it resolve on a PATH this reading does not assemble
_APPENDING_ASSIGNMENT = re.compile(r"(PATH|HOME|CDPATH)\+=")
# environment names the shell reads before the text: a startup file (`BASH_ENV`) or options to turn on
# (`BASHOPTS`, `SHELLOPTS`); a shell command under any of them is refused whole. `ENV` is read by an
# interactive shell only (measured: neither `bash -c` nor this platform's `sh -c` reads it)
_STARTUP_STATE = frozenset({"BASH_ENV", "BASHOPTS", "SHELLOPTS"})
# an exported function (`BASH_FUNC_<name>%%`, the `%%` included, the value beginning `() {`: otherwise
# the name is an ordinary variable): bash imports it before the text, and a plain word then names
# the function, whose body this reading never sees
_EXPORTED_FUNCTION = re.compile(r"BASH_FUNC_.+%%")


def _startup_state(child_env: dict[str, str]) -> set[str]:
    """The names in the child's environment that a shell reads before the text; empty when none."""
    # bash imports a function only when its value begins `() {` exactly (measured: a leading space or
    # another value defines nothing, and the name is an ordinary variable)
    return {name for name in _STARTUP_STATE if child_env.get(name)} | {
        name
        for name, value in child_env.items()
        if _EXPORTED_FUNCTION.fullmatch(name) and value.startswith("() {")
    }


# the shells whose text this scan reads, as `/bin/sh` reads it (`dash` is `/bin/sh` on Debian-family
# systems): a shell command whose executable is any other program, by the name written or by the file it
# leads to, is refused whole, since which expansions it makes cannot be read without running it (zsh's
# `=name` and `^`, ksh's `@(...)`, csh's history, and the same shell copied under a name of its own)
_SCANNED_SHELLS = frozenset({"sh", "bash", "dash"})


def _resolution_change(words: list[str]) -> str | None:
    """The first word of a shell text's command words that changes how a later word resolves, or that
    reads text again; None when there is none."""
    for word in words:
        if (
            # a builtin is the bare word: a path (`/tmp/x/cd`) starts the file there, not the builtin
            (word in _RESOLUTION_BUILTINS)
            or os.path.basename(word) in _RESOLUTION_SHELLS
            or _RESOLUTION_ASSIGNMENT.match(word)
            or _RESOLUTION_NAME.fullmatch(word)
        ):
            return word
    return None


def _ends_a_command(command: str) -> bool:
    """Whether the shell text holds a second command: a `;`, a newline, a parenthesis, a `|` (not the
    `>|` redirection) or a `&` (not the `>&`, `<&`, `&>` redirections) outside quotes and unescaped.
    Read on the raw text, since a quoted `';'` is an argument."""
    quote, escaped, prev = None, False, ""  # prev: the last bare character, "" after a quoted or escaped one
    for i, c in enumerate(command):
        bare = False
        if escaped:
            escaped = False
        elif quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            escaped = True
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c in ";\n()":
            return True
        elif c == "|" and prev != ">":
            return True
        elif c == "&" and prev not in ("<", ">") and command[i + 1 : i + 2] != ">":
            return True
        else:  # an escaped `>` is an argument, so a `|` after it starts a second command
            bare = True
        prev = c if bare else ""
    return False


def _resolved(program: str, cwd: str, path: str | None) -> str | None:
    """The file a program name leads to, links resolved: from the child's working directory when the
    name holds a separator, else the first on `path`; None when none is found."""
    found = os.path.join(cwd, program) if os.sep in program else _which(program, path=path)
    return os.path.realpath(found) if found is not None else None


def _launched_programs(args, executable, shell, shell_file) -> tuple[list[str], str | None]:
    """The programs a Popen call starts, as its caller wrote them, and the PATH a shell command's own
    leading `PATH=` assignment sets (None when it sets none). A shell command starts its shell
    (`executable`, when given) and is judged by every word of its text past leading `VAR=value` words:
    the text is not parsed as the shell would, so a program anywhere in it (after `exec`, inside `if`,
    past `&&` or `|`, in a here-document's body) counts, and so does a guarded name given as an
    argument or looked up with `command -v`. The text has no bare expansion (`_bare_expansion` is
    asked first), so every word, the PATH value included, is what the shell would run. A leading
    `PATH=` is the PATH of its own simple command only, so a text with one and a second command (past
    an unquoted `;`, `&&`, `||`, `|`, `&`, a parenthesis or a newline) is refused whole, and so is a
    text with a leading `PATH+=` (or `HOME+=`, `CDPATH+=`), which appends to the child's. An executable
    other than `sh`, `bash` or `dash`, by the name written or by the file it leads to (`shell_file`,
    links resolved), is refused whole as well: the scan reads `/bin/sh`'s. A file under the session's
    temporary directory is a stand-in a test wrote, whatever its name, and starts. Any other call starts
    `executable`, else its first argument."""
    if shell:
        # an empty sequence is a shell given no text: `sh -c` alone, which starts nothing it names
        command = os.fsdecode(args if isinstance(args, (str, bytes)) else (args[0] if args else ""))
        if executable is not None:
            names = {os.path.basename(os.fsdecode(executable))}
            if shell_file is not None:
                names.add(os.path.basename(shell_file))
            # a guarded program as the shell is left to the judging below, whose refusal names it
            if (
                not names <= _SCANNED_SHELLS
                and not names & _GUARDED_PROGRAMS
                and not (shell_file is not None and _stand_in(shell_file))
            ):
                raise _ResolutionChange(command, os.fsdecode(executable))
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)  # `;`, `&&`, `|` split off a word
        # a `#` inside a word is the word's (`x#y`), as the shell reads it; a comment is a `#` beginning
        # a word, and `_bare_expansion` has refused the text already when there is one
        lexer.commenters = ""
        lexer.whitespace_split = True
        try:
            words = list(lexer)
        except ValueError:
            words = command.split()
        assignments = list(itertools.takewhile(_ENV_ASSIGNMENT.match, words))
        path = None
        for word in assignments:
            if _APPENDING_ASSIGNMENT.match(word):
                raise _ResolutionChange(command, word)
            if word.startswith("PATH="):
                path = word[5:]
        if (word := _resolution_change(words[len(assignments) :])) is not None:
            raise _ResolutionChange(command, word)
        if path is not None and _ends_a_command(command):
            raise _ResolutionChange(command, next(w for w in assignments if w.startswith("PATH=")))
        programs = [os.fsdecode(executable)] if executable is not None else []
        return programs + words[len(assignments) :], path
    if executable is not None:
        return [os.fsdecode(executable)], None
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)], None
    return ([os.fsdecode(args[0])] if args else []), None


class _ResolutionChange(Exception):
    def __init__(self, command: str, word: str):
        super().__init__(command, word)
        self.command, self.word = command, word


class Refusals:
    """The launches the guard below refused during this test. A test of the guard declares the ones it
    expected with `expect`, which checks them and empties the ledger; nothing else empties it, and
    anything left in it at teardown fails the test."""

    def __init__(self):
        self._refused: list[str] = []

    def __eq__(self, other):
        return self._refused == other

    def __repr__(self):
        return repr(self._refused)

    def expect(self, launches: list[str]) -> None:
        # checked and emptied under the guard's lock: a refusal a thread of the test records between
        # the two would otherwise be erased unread
        with _guarding.lock:
            assert self._refused == list(launches), self._refused
            self._refused.clear()


@pytest.fixture
def refused_launches():
    return Refusals()


class _Guarding:
    """What the seam below judges a launch by: the running test's declarations and ledger, bound by
    `_refuse_undeclared_cli_launches`, and the session's temporary directory, under which a program is
    a stand-in a test wrote. Outside any test (collection, a fixture of a wider scope) nothing is
    declared, no directory holds stand-ins, and the ledger is the suite's own."""

    declared: frozenset[str] = frozenset()
    refused: Refusals = Refusals()
    stand_ins: str | None = None
    # the threads alive when the running test was bound: an earlier test's or a wider-scoped fixture's,
    # whose launches carry none of this test's declarations
    inherited: frozenset[threading.Thread] = frozenset()
    # past the exit-time read of the ledger: nothing reports after it, so a refusal ends the process
    exiting: bool = False
    # held while a launch is judged and recorded, and while a test is bound or unbound: a launch from a
    # thread of the test still in flight at its teardown then lands in the ledger read next, never in
    # the one just read and let go
    lock: threading.RLock = threading.RLock()


_guarding = _Guarding()
_outside_tests = _guarding.refused


def _inherited(thread: threading.Thread) -> tuple[threading.Thread, str] | None:
    """The thread in the bound test's inherited set that `thread` is or descends from, with why, else
    None: a thread an inherited one started after the test was bound is that thread's work, not the
    test's. A thread whose start this seam did not see (started past `threading.Thread.start`) is
    read the same way, since its line is unknown."""
    while thread is not threading.main_thread():
        if thread in _guarding.inherited:
            return thread, "alive before this test was bound"
        creator = getattr(thread, "_lion1_creator", None)
        if creator is None:
            return thread, "started past `threading.Thread.start`, so of a line this reading does not know"
        thread = creator
    return None


def _stand_in(real: str) -> bool:
    return _guarding.stand_ins is not None and Path(real).is_relative_to(_guarding.stand_ins)


def _record(entry: str, program: str | None = None) -> None:
    # every refusal's one record: into the ledger bound now, under the lock the binding takes (a launch
    # in flight at the test's teardown lands in the one read next); past the exit-time read of the
    # ledger nothing reports a refusal, whatever its reason, so it ends the process here
    with _guarding.lock:
        _guarding.refused._refused.append(entry)
        exiting = _guarding.exiting
    if exiting:
        sys.stderr.write(f"a callback at the interpreter's exit started {program or entry!r}, refused.\n")
        sys.stderr.flush()
        os._exit(1)


def _text(value) -> str:
    # the exact `str` a value holds, read through the base types: a subclass's own `encode` is what Popen
    # reads the program and the environment through, and its own `decode`, `split` or `startswith` what a
    # reading of it would, so either could find a program other than the one the value names
    value = os.fspath(value)
    if isinstance(value, bytes):
        return bytes.decode(value, sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())
    return str.__str__(value)


def _guarded_execute_child(self, *args, **kwargs):
    bound = _signature.bind(self, *args, **kwargs)
    call = bound.arguments
    # read each value once, as the text it holds, and hand that reading to Popen: a path-like, a mapping
    # or a str or bytes subclass can answer a second reading differently, and the program that starts is
    # the one Popen's reading finds
    given = call["args"]
    call["args"] = _text(given) if isinstance(given, (str, bytes, os.PathLike)) else [_text(a) for a in given]
    for key in ("executable", "cwd"):
        if call.get(key) is not None:
            call[key] = _text(call[key])
    # whether the text goes to a shell is the one other value both readings branch on: its truth, read
    # once as a bool, is what Popen gets
    call["shell"] = bool(call.get("shell"))
    if call.get("preexec_fn") is not None:
        # it runs in the child after this reading and before the program starts, and can change what
        # a name leads to (a link made at a path that was empty); nothing here can read what it does
        given = call["args"]
        written = given if isinstance(given, (str, bytes, os.PathLike)) else (given[0] if given else "")
        _record(os.fsdecode(written))
        raise FileNotFoundError(
            errno.ENOENT,
            f"the test suite refused to start {os.fsdecode(written)!r}: the call has a `preexec_fn`, "
            "which runs in the child between this reading and the program's start, so what starts "
            "is not what this reading finds; drop it",
            written,
        )
    env = call.get("env")  # its keys and values Popen takes as bytes too
    # with none given the child would inherit the process's environment, which `os.putenv` changes
    # without `os.environ` seeing it: the copy read here is the one Popen gets
    env = call["env"] = dict(os.environ) if env is None else {_text(k): _text(v) for k, v in env.items()}
    child_env = env
    child_path = os.pathsep.join(os.get_exec_path(child_env))  # Popen's own: `os.defpath` when unset
    # a relative program or PATH entry resolves where the child starts, not where pytest runs
    cwd = os.fsdecode(call["cwd"]) if call.get("cwd") is not None else os.getcwd()
    shell_file = None
    if call.get("shell") and call.get("executable") is not None:  # Popen starts it on the child's PATH
        anchored = None
        if child_path is not None:
            anchored = os.pathsep.join(os.path.join(cwd, e) for e in child_path.split(os.pathsep))
        shell_file = _resolved(os.fsdecode(call["executable"]), cwd, anchored)
    # a stand-in the test wrote as the shell is the one program the call starts: the text is its
    # argument, read by nothing this reading knows, so the text is not read as shell text
    shell = call.get("shell") and not (shell_file is not None and _stand_in(shell_file))
    if shell:
        given = call["args"]
        text = os.fsdecode(given if isinstance(given, (str, bytes)) else (given[0] if given else ""))
        if (c := _bare_expansion(text)) is not None:
            _record(text)
            raise FileNotFoundError(
                errno.ENOENT,
                f"the test suite refused to run the shell text {text!r}: a bare {c!r} is one the "
                "shell reads before a program is named (an expansion, a comment, a here-document, "
                "an open quote, a line continuation, an escaped expansion), so what starts is not in "
                "the text; quote it or write the program's path",
                text,
            )
    if shell and (startup := _startup_state(child_env)):
        # the shell reads a file, turns options on (`extglob` makes `@(co|do)dex` a program name), or
        # imports a function (a plain word then names it) before it reads the text: what starts is then
        # not what this reading of the text finds; an empty startup value names no file and turns
        # nothing on (measured: a program under one starts)
        _record(text)
        raise FileNotFoundError(
            errno.ENOENT,
            f"the test suite refused to run the shell text {text!r}: {sorted(startup)[0]!r} is in the "
            "child's environment, and the shell reads it before the text (a startup file, shell "
            "options, an exported function), so what starts is not what this reading finds; drop it "
            "from the environment",
            text,
        )
    try:
        programs, own_path = _launched_programs(call["args"], call.get("executable"), shell, shell_file)
    except _ResolutionChange as e:
        _record(e.command)
        raise FileNotFoundError(
            errno.ENOENT,
            f"the test suite refused to run the shell text {e.command!r}: {e.word!r} changes where a "
            "later word resolves or reads text again (a change of directory or of PATH, a leading "
            "`PATH=` before a second command, `eval`, a sourced file, another shell, an executable "
            "other than sh, bash or dash, `shopt`, `setopt`), so what starts is not what this reading "
            "finds; write one command for /bin/sh with the program's path",
            e.command,
        ) from None
    path = child_path if own_path is None else own_path
    if shell and path is not None:
        # the shell expands a `~` beginning a PATH entry as it searches, however the entry was quoted
        # and whether the text's assignment or the environment set it; this reading resolves none of it
        for entry in path.split(os.pathsep):
            if entry.startswith("~"):
                _record(text)
                raise FileNotFoundError(
                    errno.ENOENT,
                    f"the test suite refused to run the shell text {text!r}: the PATH entry {entry!r} "
                    "begins with a `~`, which the shell expands as it searches for a program, so what "
                    "starts is not what this reading finds; write the directory's path",
                    text,
                )
    if path is not None:
        path = os.pathsep.join(os.path.join(cwd, e) for e in path.split(os.pathsep))
    # the guarded programs' own files, as the child or the session would find them: a hard link
    # keeps none of the name, so the file is what says a program under another name is one of them.
    # A guarded program neither PATH reaches is known by its name alone: a hard link to it under
    # another name starts, since nothing about that file says what it is and a PATH is the one place
    # this reading knows to look
    guarded_files = []
    for name in _GUARDED_PROGRAMS:
        for p in {path, os.environ.get("PATH")}:
            # every match on the PATH, not the first: a stand-in the test wrote ahead of a guarded
            # program would hide it, and a hard link to the one behind would start (an empty entry is
            # the current directory, as a lookup over the whole PATH reads it)
            for entry in p.split(os.pathsep) if p is not None else [None]:
                found = _which(name, path=(entry or os.curdir) if entry is not None else None)
                if found is not None and not _stand_in(os.path.realpath(found)):
                    try:
                        guarded_files.append((name, os.stat(found)))
                    except OSError:  # the file went between the lookup and the stat, so it starts nothing
                        continue
    for program in programs:
        real = _resolved(program, cwd, path)
        names = {os.path.basename(program)} | ({os.path.basename(real)} if real else set())
        same_file = {
            n
            for n, st in guarded_files
            if real and os.path.exists(real) and os.path.samestat(os.stat(real), st)
        }
        # called by a guarded name, every such name is declared; called by none, its file is one a
        # declared name leads to (a hard link to `codex` under another name starts under `codex`)
        by_name = names & _GUARDED_PROGRAMS
        # a declaration is the running test's: a launch from a thread alive before the test was bound
        # (an earlier test's worker released now, a wider-scoped fixture's), or from one such a thread
        # started since, carries none of it. Judged and recorded under the lock the binding takes, so
        # a launch in flight at the test's teardown lands in the ledger read next, never in one let go
        with _guarding.lock:
            inherited = _inherited(threading.current_thread())
            declared = frozenset() if inherited is not None else _guarding.declared
            undeclared = by_name - declared if by_name else same_file - declared
            if not undeclared or (not by_name and same_file & declared):
                continue
            if not same_file and real is not None and _stand_in(real):
                continue
            name = sorted(undeclared)[0]
            current = threading.current_thread()
            if inherited is not None:
                thread, because = inherited
                why = (
                    f"the launch comes from thread {thread.name!r}, {because}, and a declaration covers "
                    "the threads the test starts through `threading.Thread.start`: start the worker in the "
                    "test"
                )
                entry = f"{program} (from thread {thread.name!r}, {because})"
            elif _guarding.refused is _outside_tests and current is not threading.main_thread():
                # no test is bound, and the thread's line leads to one: a worker the test left in flight
                because = "past its test's teardown"
                why = (
                    f"the launch comes from thread {current.name!r} {because}: the test waits for its worker"
                )
                entry = f"{program} (from thread {current.name!r}, {because})"
            else:
                why = f"mark the test @pytest.mark.launches_cli({name!r}) if it means to"
                entry = program
            _record(entry, program)
        raise FileNotFoundError(errno.ENOENT, f"the test suite refused to start {name!r}: {why}", program)
    return _execute_child(*bound.args, **bound.kwargs)


# the seam, taken as this module is imported: before collection, and before a fixture of any scope
# can start a program. Imported under a second name (`-p tests.conftest`), the module finds its own
# wrapper there and keeps it.
_guarded_execute_child._lion1_launch_guard = True  # type: ignore[attr-defined]
if not getattr(subprocess.Popen._execute_child, "_lion1_launch_guard", False):
    _execute_child = subprocess.Popen._execute_child
    _signature = inspect.signature(_execute_child)
    subprocess.Popen._execute_child = _guarded_execute_child


def _the_seam_holds(when: str) -> None:
    # the seam is on the `Popen` that the `subprocess` in `sys.modules` holds now: a reload of the module,
    # or the module dropped and imported again, makes a class without it, and a launch through that
    # class is not judged. The seam is put back on it, so the rest of the run is guarded, and the test
    # fails, named
    module = sys.modules.get("subprocess")
    if module is not None and getattr(module.Popen._execute_child, "_lion1_launch_guard", False):
        return
    if module is None:
        sys.modules["subprocess"] = module = subprocess
    module.Popen._execute_child = _guarded_execute_child
    pytest.fail(
        f"subprocess.Popen lost the launch guard ({when}): a reload of `subprocess`, or the "
        "module imported again, made a class without it, and a launch through that class was not judged. "
        "The guard is back on it; a test does not reload `subprocess`.",
        pytrace=False,
    )


def _recording_start(self: threading.Thread) -> None:
    # a thread records the thread that started it, so a launch is read against the test that owns
    # the thread's line, not the thread alone (a worker alive before the test may start another now)
    self._lion1_creator = threading.current_thread()  # type: ignore[attr-defined]
    return _thread_start(self)


_recording_start._lion1_launch_guard = True  # type: ignore[attr-defined]
if not getattr(threading.Thread.start, "_lion1_launch_guard", False):
    _thread_start = threading.Thread.start
    threading.Thread.start = _recording_start  # type: ignore[method-assign]


_THREAD_WAIT_S = 60.0  # how long the session's teardown waits for the threads the last test left


def _fail_on_launches_outside_tests(when: str) -> None:
    # taken and emptied under the guard's lock: a refusal a thread records between the two would be
    # erased unread
    with _guarding.lock:
        launched, _outside_tests._refused[:] = list(_outside_tests._refused), []
    if launched:
        pytest.fail(
            "collection, a fixture of a wider scope, or a thread a test left in flight started a program no "
            f"test declared ({when}): "
            + ", ".join(launched)
            + ". A launch belongs in a test, under its @pytest.mark.launches_cli(<name>), or to a stand-in "
            "written under the session's temporary directory; a test that starts a worker waits for it.",
            pytrace=False,
        )


@pytest.fixture(scope="session", autouse=True)
def _the_session_guard(tmp_path_factory):
    # set up before any other session fixture, so one that writes a stand-in under the temporary
    # directory may start it; torn down after every other, so a launch from a wider-scoped fixture's
    # teardown, past the last test, still fails the run
    _guarding.stand_ins = os.path.realpath(tmp_path_factory.getbasetemp())
    yield
    # a thread the last test left running may still launch: wait for every one the interpreter would
    # wait for at exit, then read the ledger (a daemon thread's launch past this point is not seen);
    # one still running past the wait is a leak of the suite's own, named
    deadline = time.monotonic() + _THREAD_WAIT_S

    def running() -> list[threading.Thread]:  # a joined thread may have started another: read again
        return [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]

    while (threads := running()) and time.monotonic() < deadline:
        threads[0].join(max(0.0, deadline - time.monotonic()))
    _fail_on_launches_outside_tests("after the last test")
    _the_seam_holds("after the last test")
    if lingering := [t.name for t in running()]:
        pytest.fail(
            f"a thread is still running {_THREAD_WAIT_S}s after the last test: " + ", ".join(lingering),
            pytrace=False,
        )


def _fail_the_process_on_launches_past_teardown(when: str) -> None:
    # hooks past the session's teardown (`pytest_sessionfinish`, `pytest_unconfigure`) can still launch;
    # a refusal swallowed there is past every test's and fixture's report, so the run itself fails
    with _guarding.lock:
        launched, _outside_tests._refused[:] = list(_outside_tests._refused), []
        if when == "at exit" and not launched:
            # `atexit` runs callbacks last-registered first, so one a module imported before this one
            # registered runs after this read: a launch from it ends the process at the refusal itself.
            # Set with the read under the lock, so a refusal lands either in this read or past it
            _guarding.exiting = True
    if launched:
        sys.stderr.write(
            f"a hook past the session's teardown started a program no test declared ({when}): "
            + ", ".join(launched)
            + ". A launch belongs in a test, under its @pytest.mark.launches_cli(<name>).\n"
        )
        sys.stderr.flush()
        if when == "at exit":  # past every hook: nothing else reports, and the exit status is the report
            os._exit(1)
        raise SystemExit(1)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config):
    # a wrapper runs around every plain hook, and `tryfirst` puts it outside the wrappers registered
    # before it, so a plugin loaded after this one and launching at `pytest_unconfigure` is read here
    try:
        return (yield)
    finally:
        _fail_the_process_on_launches_past_teardown("at pytest_unconfigure")


# a plugin registered later with a `tryfirst` wrapper of its own runs outside that one; the interpreter's
# exit is past every hook, so the ledger is read there last of all (the last test's threads were joined)
atexit.register(_fail_the_process_on_launches_past_teardown, "at exit")


@pytest.fixture(autouse=True)
def _refuse_undeclared_cli_launches(request, refused_launches):
    """Refuse a launch of a khive binary or a provider CLI the test did not declare. A test declares one
    with ``@pytest.mark.launches_cli("<name>", ...)``. The check sits on ``subprocess.Popen._execute_child``,
    which every launch made through `subprocess` goes through, asyncio's included; it is installed as
    this module is imported, so it is there before collection and before a fixture of any scope, and
    this fixture binds the running test's declarations and ledger to it. It is on the class the
    `subprocess` in ``sys.modules`` holds: a reload of the module, or the module dropped and imported
    again, makes a class without it, so each test's setup and teardown and the session's teardown check
    it is there, put it back, and fail, naming the replacement; a launch through the replaced class in
    between is not judged. It matches the program by
    name, as written and as its file resolves (a link to a guarded program under another name is that
    program), and by file: a hard link to a guarded program found on the child's or the session's PATH
    is that program whatever its name or place. Any other program whose file, links resolved, lies
    under the session's temporary directory is one a test wrote there, a stand-in, and starts
    undeclared: the suite's stubs live there, and a marker would also let the real program start in the
    tests that use them. A shell command is judged by its shell, when the call names one, and by every
    word of its text, each looked up on the PATH the text's own leading assignment sets, else the
    child's, from the child's working directory; a leading `PATH=` reaches its own command only, so a
    text with one and a second command is refused whole.

    A refused launch raises ``FileNotFoundError``, what a machine without the program would raise, so
    nothing starts. Code under test may swallow that error and carry on, so the test also fails at
    teardown, naming each launch. Outside any test nothing is declared: a launch from collection or
    from a fixture of a wider scope is refused the same way, and if swallowed, fails the setup of the
    next test, or the session's teardown after the last, naming it; so is one judged after the test
    that started its thread was unbound (a worker left in flight at the teardown: the launch is judged
    and recorded under the lock the binding takes, so it lands in the ledger read next, naming the
    thread, never in the one just read); that teardown first waits (a
    minute at most, then names what still runs) for the threads the interpreter would wait for at
    exit, so a launch from a thread the last test left running is read too (one from a daemon thread
    past that point is not); a launch from a hook past that teardown (`pytest_sessionfinish`,
    `pytest_unconfigure`) is read after every `pytest_unconfigure` hook, and again at the
    interpreter's exit, and fails the run itself; a launch from an `atexit` callback registered before
    this module's (run after its read) ends the process at the refusal, whatever its reason (a name,
    a text refused whole), with the exit status as the report. Nothing checks the seam past the
    session's teardown, so a hook or a callback there that reloads `subprocess` first launches
    unjudged (ADR-0027 S9). A declaration is the running test's
    own: a thread alive before the test was bound (one an
    earlier test or a wider-scoped fixture started), and any thread such a thread starts since, carries
    none of it, so a worker released by a marked test is refused, the refusal naming the thread and
    the rule, as if from no test; a thread the marked test starts through `threading.Thread.start`
    carries its declarations, and one started past it (`_thread.start_new_thread`, a `start` of its
    own) is of a line this reading does not know and is refused the same way. A word shaped
    `PATH=...` anywhere past the leading assignments is refused, an argument to a plain program
    included: this reading does not place commands, and a second command's leading assignment would
    read as an argument otherwise. So is a word that changes where a later word resolves or reads
    text again (`cd`, `pushd`, `popd`, `eval`, `source`, `.`, `unset`, `shopt`, `setopt`, a shell by
    name), quoted or not: at a command's head a quoted `'cd'` is still the builtin (measured with sh
    and bash), so `printf '%s' 'cd'` is refused, and a test that prints such a word runs without a
    shell. A
    shell command under a non-empty `BASH_ENV`, `BASHOPTS` or `SHELLOPTS`, or a `BASH_FUNC_<name>%%`
    whose value begins `() {` (an exported function, which a plain word then names), in the child's
    environment is refused whole: the shell reads those before the text. Each value of the call is
    read once, and that reading is what Popen starts: a path-like or an `env` mapping answering a
    second reading differently starts what the first found. A call with no `env` is handed the copy of
    `os.environ` this reading read, for every program, guarded or not, so a value set only through
    `os.putenv`, which `os.environ` does not see, reaches no child the suite starts: a program found
    only on such a PATH is not found (a shell exits 127). A test sets the environment through
    `os.environ` or `monkeypatch.setenv`.

    It reads a shell command's words, not the shell's grammar, so a guarded name anywhere in the text
    is refused, an argument or a `command -v` lookup included; and a text the shell reads before a
    program is named (`$` or a backtick outside single quotes, a glob or brace character outside
    quotes, a `~` beginning a word; a comment, a here-document, an open quote or a line continuation,
    past which this reading cannot follow the shell's) is refused whole, since what it starts is not in it:
    over-refusal fails a test loudly, and the test quotes the text or writes the literal path. With
    `shell=True`, an `executable` outside the session's temporary directory starts only when it is
    `sh`, `bash` or `dash` by the name written and by the file it leads to, and its text is read as
    `/bin/sh` reads it; any other is refused whole, since which expansions it makes cannot be read
    without running it, and the test writes a stand-in. A program under one of those three names is
    read as that shell, and what it starts is not seen (ADR-0027 S5). A call
    with a `preexec_fn` is refused whole: it runs in the child after this reading and before the
    program starts, and can change what a name leads to. A change made by another thread of the test
    in that same window (a link replaced, the working directory relinked) is not seen either: this
    reading stats a path and Popen starts the name, so a test does not change what a path leads to
    while a launch of it is in flight, except the one that pins this limit (ADR-0027 S4). It
    does not see what `env`, `nohup`, a script or another program starts in turn; and it does not see
    launches made by a child process the test started, nor one made past `subprocess.Popen`
    (`os.system`, `os.posix_spawn`, `os.spawn*`, `os.exec*`, `pty.spawn`, `subprocess._fork_exec`,
    `_posixsubprocess.fork_exec`, a native call through `ctypes` or `cffi`, `multiprocessing`): the code
    under test makes none, and `tests/test_launch_guard.py` keeps it so for code that names what it
    calls, refusing what runs code it cannot read (text, bytes, a dotted name, a computed import); text
    built while the code runs and handed to an evaluator it does not name, and a lookup by a computed
    name on an object other than those modules, are beyond a source scan (ADR-0027). Nor does it see
    a launch made while a test has put another function in the seam's place: the seam is checked at
    each test's setup and teardown, not at each launch, so a test does not replace it, except the one
    that pins this limit (ADR-0027 S8)."""
    _fail_on_launches_outside_tests("before this test")
    _the_seam_holds("before this test")
    with _guarding.lock:
        _guarding.declared = frozenset(
            name for marker in request.node.iter_markers("launches_cli") for name in marker.args
        )
        _guarding.inherited = frozenset(t for t in threading.enumerate() if t is not threading.main_thread())
        _guarding.refused = refused_launches
    yield
    # unbound under the lock: a launch of the test's own thread judged after this lands in the suite's
    # ledger and fails the next test's setup, or the session's teardown, naming the thread
    with _guarding.lock:
        _guarding.declared, _guarding.inherited, _guarding.refused = frozenset(), frozenset(), _outside_tests
    _the_seam_holds("at this test's teardown")
    if refused_launches._refused:
        pytest.fail(
            "this test started a program it did not declare: "
            + ", ".join(refused_launches._refused)
            + ". A khive binary leaves a daemon running and a provider CLI spends a real model call. Build "
            "what the test drives so it does not reach them (the `kkernel_stub` or `store` fixture, a "
            "stand-in backend), or mark the test @pytest.mark.launches_cli(<name>) if starting it is the "
            "point.",
            pytrace=False,
        )


@pytest.fixture
def kkernel_stub(tmp_path_factory, monkeypatch):
    """A `kkernel` on PATH for a test of the wiring around the khive client, on a machine without the
    binary (a sandbox VM, CI). It serves the lion pack (ADR-0025 D4, D6) from `tests/lionfake.py` over
    a file of its own and refuses every other pack."""
    b = tmp_path_factory.mktemp("kkernel-stub")  # beside the test's tmp_path, never inside it
    repo = repr(str(Path(__file__).resolve().parent.parent))
    (b / "kkernel").write_text(f"#!{sys.executable}\n" + _STUB_KKERNEL.replace("REPO", repo))
    (b / "kkernel").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b / "kkernel"


@pytest.fixture
def store():
    """The store over the in-memory pack, for a test that builds an `Actor` (ADR-0026 D7)."""
    from tests.lionfake import store as _store

    return _store()


@pytest.fixture
def console_khive(tmp_path, kkernel_stub, monkeypatch):
    """`tmp_path` made the directory of the actor `console`, with every khive op answered in memory: what
    `console` and `lion chat` need to start (ADR-0025 D3). The `LionFake` behind it."""
    from tests.lionfake import actor_dir, patch_khive

    actor_dir(tmp_path)
    return patch_khive(monkeypatch)


@pytest.fixture
def bench_store(monkeypatch):
    """`run_set` takes the store a run writes its rows to from `--khive-dir`: here the in-memory pack."""
    from tests._swe import run_set
    from tests.lionfake import store as fresh_store

    monkeypatch.setattr(run_set, "store_of", lambda directory: fresh_store())


@pytest.fixture
def no_collection():
    """For a test that takes the interpreter to its recursion limit on purpose. A garbage collection
    that happened to run there would run some other object's finalizer with no stack left, and the
    RecursionError it raises, unraisable, would fail the test by chance. Collection comes back as it
    was, whether the test passed or failed."""
    was = gc.isenabled()
    gc.disable()
    yield
    if was:
        gc.enable()
