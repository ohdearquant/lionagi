# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The project config layers a Codex run can read, and the check on them.

``codex exec`` reads ``<dir>/.codex/config.toml`` as a project layer for each
directory from its working directory up to the project root. ``--ignore-user-config``
empties the user layer only, and ``mcp_servers`` is not on the project layer's
denylist, so wherever a trust source the user config does not carry marks the
project trusted, that file adds MCP servers past an explicit set. Codex has no
switch to drop the project layers, so a run under ``--ignore-user-config`` refuses
one whose project layers declare ``mcp_servers``, before each process starts: every
``codex exec`` reads its config afresh. A run that keeps the user config is not
checked here: its set is exclusive only over the servers that config declares,
each disabled by name, and a server a trusted project layer declares still loads
(docs/internals/agent-runtime.md).

The walk starts where Codex starts: the working directory as Codex resolves ``-C``
(the logical path when an ancestor below the top level is a symlink, else the
canonical one), with the ``.codex`` that resolves to CODEX_HOME skipped. It does not
stop at the project root. Codex picks the root by ``project_root_markers``, and an
enterprise cloud config bundle, fetched with the account's credentials and not
readable here, can set them and put the root above any root found here. So every
directory up to the filesystem root is read: a layer above the root Codex picks
counts too, and the check can refuse a file Codex would not read but never misses
one it would.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

from lionagi._errors import ConfigurationError
from lionagi.providers.openai._codex_profile import refuse_non_absolute_codex_home

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10: the declared `tomli`; the locked 2.4.1 reads TOML 1.1
    import tomli as _tomllib  # type: ignore[no-redef]

__all__ = (
    "declaring_project_layer",
    "mcp_servers_declaration_line",
    "parse_failure",
    "project_layers",
    "project_mcp_server_names",
)

# How many candidate lines `mcp_servers_declaration_line` tries before naming the first.
_LINES_TRIED = 8
_TOML_CODE_POINT = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})|\\x([0-9A-Fa-f]{2})")


def _reads_toml_1_1() -> bool:
    """Whether this Python's TOML parser reads TOML 1.1, as ``tomli`` does from 2.4; ``tomllib``
    on Python 3.11 to 3.14 reads 1.0 only. A trailing comma in an inline table is 1.1 syntax."""
    try:
        _tomllib.loads("t = {a = 1,}")
    except _tomllib.TOMLDecodeError:
        return False
    return True


_READS_TOML_1_1 = _reads_toml_1_1()


def project_layers(
    cwd: str | os.PathLike[str], env: Mapping[str, str]
) -> Iterator[tuple[Path, str]]:
    """The project-layer ``config.toml`` files a Codex run in ``cwd`` under ``env`` can
    read, nearest first, each with its text.

    A file on the walk that exists but is not a regular file or cannot be read, and a
    CODEX_HOME or HOME that is not absolute, raise ``ConfigurationError``: what Codex
    would load is then unknown. The type is checked before reading, so a FIFO refuses
    rather than blocking the spawn.
    """
    home = _codex_home(env)
    run_cwd = _config_cwd(Path(os.path.abspath(cwd)))
    for directory in (run_cwd, *run_cwd.parents):
        dot_codex = directory / ".codex"
        if _is_codex_home(dot_codex, home):
            continue
        path = dot_codex / "config.toml"
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise ConfigurationError(
                    f"the Codex project config {path} is not a regular file, so what it "
                    "sets is unknown. Replace it with a regular file or remove it"
                )
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                f"the Codex project config {path} could not be read ({exc}), so what it "
                "sets is unknown. Make it readable by this process as UTF-8 text, with its "
                "folders searchable, or remove it"
            ) from exc
        yield path, text


def declaring_project_layer(
    cwd: str | os.PathLike[str], env: Mapping[str, str]
) -> tuple[Path, int, bool] | None:
    """The first of ``project_layers(cwd, env)`` that declares a top-level ``mcp_servers``
    key, with its line and whether the parse shows that line declaring it, or ``None``.

    A layer is read as a TOML parser reads it, or line by line toward refusing when the
    parser refuses it (``mcp_servers_declaration_line``), and counts whether or not its
    project is trusted, since trust is not fully readable here.
    """
    for path, text in project_layers(cwd, env):
        found = _declaration(text)
        if found is not None:
            return path, *found
    return None


def project_mcp_server_names(
    cwd: str | os.PathLike[str], env: Mapping[str, str]
) -> set[str]:
    """Names in every project layer that could contribute to a Codex run.

    The effective root and trust decision are not fully visible here, so the
    walk intentionally uses the same conservative scope as project-layer
    refusal. A layer whose structure cannot be read cannot support an
    exclusive selection and is refused before the process starts.
    """
    names: set[str] = set()
    for path, text in project_layers(cwd, env):
        try:
            parsed = _tomllib.loads(text)
        except (_tomllib.TOMLDecodeError, RecursionError) as exc:
            raise ConfigurationError(
                f"Cannot enforce an exclusive Codex MCP selection: project config {path} "
                "cannot be parsed, so its server names are unknown."
            ) from exc
        if "mcp_servers" not in parsed:
            continue
        servers = parsed["mcp_servers"]
        if not isinstance(servers, dict):
            raise ConfigurationError(
                f"Cannot enforce an exclusive Codex MCP selection: project config {path} "
                "sets `mcp_servers` to a value that is not a table."
            )
        names.update(servers)
    return names


def _codex_home(env: Mapping[str, str]) -> Path:
    """CODEX_HOME as Codex finds it: the absolute variable as given, canonical, or HOME's
    ``.codex`` as given. A value that is not an absolute path is refused."""
    refuse_non_absolute_codex_home(env)
    value = env.get("CODEX_HOME") or ""
    if value:
        try:
            return Path(os.path.realpath(value, strict=True))
        except OSError:
            return Path(value)  # Codex stops before reading any layer
    home = env.get("HOME")
    if not home:
        import pwd  # Unix, as these paths are

        home = pwd.getpwuid(os.getuid()).pw_dir
    return Path(home) / ".codex"


def _config_cwd(cwd: Path) -> Path:
    """The directory Codex walks for ``-C cwd``: the path with ``.`` and ``..`` collapsed
    when an ancestor below the top level is a symlink, so a linked checkout keeps its own
    ancestors; else the canonical path."""
    logical = Path(os.path.normpath(cwd))
    canonical = Path(os.path.realpath(cwd))
    if canonical != logical and any(
        len(ancestor.parts) > 2 and ancestor.is_symlink()
        for ancestor in (logical, *logical.parents)
    ):
        return logical
    return canonical


def _is_codex_home(dot_codex: Path, codex_home: Path) -> bool:
    """Whether a ``.codex`` is CODEX_HOME, which Codex skips as a project layer: equal as
    given, or equal once both are canonical, a symlinked one included."""
    if dot_codex == codex_home:
        return True
    return _canonical(dot_codex) == _canonical(codex_home)


def _canonical(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError:
        return path


def mcp_servers_declaration_line(text: str) -> int | None:
    """The line of a top-level ``mcp_servers`` declaration in TOML text, or ``None``.

    Text the TOML parser reads is decided by that parse, as Codex decides it: the root
    table holds the key or it does not, whatever a multi-line string or array between
    the lines holds. The line named is the first whose removal changes what the parse
    reads as ``mcp_servers``, which parsed alone sets a root ``mcp_servers``, and whose
    preceding text parses whole, so not a line inside a string or one that closes a
    string; failing that, the first whose removal changes the parse or leaves text the
    parser refuses, which reads as a declaration. Each line tried costs up to three
    parses, one of them of the file without it, so at most ``_LINES_TRIED`` are tried;
    past them the first line that reads as a declaration is named, which in a file
    built that way can be a string's.
    Text the parser refuses, TOML 1.1 syntax where it reads only 1.0 included, and text
    nested deeper than it can follow have no structure to trust, so every line counts
    whose first key segment is ``mcp_servers``, a header or a key under any table, toward
    refusing.
    """
    found = _declaration(text)
    return None if found is None else found[0]


def _declaration(text: str) -> tuple[int, bool] | None:
    """``mcp_servers_declaration_line``, and whether the parse shows that line declaring
    ``mcp_servers``: true only when the text without it parses to another value there, the
    line alone parses to a root ``mcp_servers`` and the text before it parses whole."""
    try:
        parsed = _tomllib.loads(text)
    except (_tomllib.TOMLDecodeError, RecursionError):
        candidates = _declaration_lines(text)
        return (candidates[0], False) if candidates else None
    if "mcp_servers" not in parsed:
        return None
    lines = text.split("\n")
    candidates = _declaration_lines(text)
    reads_as: int | None = None
    for number in candidates[:_LINES_TRIED]:
        rest = "\n".join([*lines[: number - 1], "", *lines[number:]])
        try:
            changed = _tomllib.loads(rest).get("mcp_servers") != parsed["mcp_servers"]
        except (_tomllib.TOMLDecodeError, RecursionError):
            changed = None  # the line carries structure the parse needs
        if changed and _declares_alone(lines[number - 1]) and _starts_statement(lines, number):
            return number, True
        if changed is not False and reads_as is None:
            reads_as = number  # a string's closing line changes the parse as well
    if reads_as is not None:
        return reads_as, False
    # the parse found it: the refusal never waits on its line
    return (candidates[0] if len(candidates) > _LINES_TRIED else 1), False


def _declares_alone(line: str) -> bool:
    """Whether *line*, parsed as a whole file, sets a root ``mcp_servers``."""
    try:
        return "mcp_servers" in _tomllib.loads(line)
    except (_tomllib.TOMLDecodeError, RecursionError):
        return False


def _starts_statement(lines: list[str], number: int) -> bool:
    """Whether the text before line *number* parses whole, so that no string or value an
    earlier line opens runs on into it. A string's line can parse alone, a comment after
    the string's close or a value that differs from itself (``nan``) included."""
    try:
        _tomllib.loads("\n".join(lines[: number - 1]))
    except (_tomllib.TOMLDecodeError, RecursionError):
        return False
    return True


def parse_failure(exc: BaseException) -> str:
    """Why this Python's TOML parser refused a text, for a refusal message: its error, or a
    nesting deeper than it can follow; for an error, where the parser reads only TOML 1.0,
    that codex reads the TOML 1.1 forms it refuses."""
    if isinstance(exc, RecursionError):
        return "it nests deeper than this Python's TOML parser can follow"
    if _READS_TOML_1_1:
        return str(exc)
    return f"{exc}; if the file uses a TOML 1.1 form, codex reads it and this parser does not"


def _declaration_lines(text: str) -> list[int]:
    """The lines that read as an ``mcp_servers`` declaration under any table: a
    ``[mcp_servers]``, ``[mcp_servers.<name>]`` or ``[[mcp_servers...]]`` header, or a
    ``mcp_servers = ...`` or ``mcp_servers.<name>... = ...`` key. The read is line by line,
    not a parse, so a line inside a string is among them."""
    found = []
    # a byte-order mark is not whitespace to `strip`; the read starts past it. Lines
    # break at LF only, as TOML counts them: `splitlines` also breaks at U+2028 and
    # U+0085, which a string or a comment may hold
    for number, raw in enumerate(text.lstrip("\ufeff").split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.lstrip("[").split("]", 1)[0] if line.startswith("[") else line.split("=", 1)[0]
        if _first_key_segment(key) == "mcp_servers":
            found.append(number)
    return found


def _first_key_segment(key: str) -> str:
    """The first segment of a dotted TOML key, its quotes stripped and, in a basic (double-quoted)
    key, its code-point escapes decoded: a parser reads ``"mcp\\u005fservers"`` as ``mcp_servers``.
    ``\\x`` is a TOML 1.1 escape, which 1.0 rejects; it is decoded too, toward refusing, so a
    parser that takes it is not evaded. A code point past U+10FFFF, which TOML does not allow
    and tomllib refuses, decodes to U+FFFD, so the key it is in names nothing."""
    key = key.strip()
    if key[:1] == "'":
        end = key.find("'", 1)
        return key[1:end] if end > 0 else key[1:]
    if key[:1] == '"':
        end = key.find('"', 1)
        while end > 0 and key[end - 1] == "\\":
            end = key.find('"', end + 1)
        segment = key[1:end] if end > 0 else key[1:]
        return _TOML_CODE_POINT.sub(_code_point, segment)
    return key.split(".", 1)[0].strip()


def _code_point(match: re.Match[str]) -> str:
    value = int(match.group(1) or match.group(2) or match.group(3), 16)
    return chr(value) if value <= 0x10FFFF else "\ufffd"
