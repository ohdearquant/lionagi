# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The project config layers Codex 0.156 reads for a run, mirrored from its loader.

``codex exec`` reads ``<dir>/.codex/config.toml`` as a project layer for each
directory from its working directory up to the project root. ``--ignore-user-config``
empties the user layer only, and ``mcp_servers`` is not on the project layer's
denylist, so wherever a trust source the user config does not carry marks the
project trusted, that file adds MCP servers past an explicit set. Codex has no
switch to drop the project layers, so a run that promised no servers beyond its
own set refuses one whose project layers declare ``mcp_servers``, before each
process starts: every ``codex exec`` reads its config afresh.

The walk is Codex's own: the working directory as Codex resolves ``-C`` (the
logical path when an ancestor below the top level is a symlink, else the canonical one), the project
root by ``project_root_markers`` (``.git`` unless the system config, the run's ``-c``
overrides or the managed config set them), and the ``.codex`` that resolves to
CODEX_HOME skipped. Two marker sources
are not read: an enterprise cloud config bundle, fetched with the account's
credentials, and macOS managed preferences. Either can set markers that put the
root above the one found here, and then a layer above it is not checked.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10: the declared `tomli`, a TOML 1.0 parser like tomllib
    # (`toml` 0.10 is not one: it reads a truncated `[` as an empty list, which here means no root)
    import tomli as _tomllib  # type: ignore[no-redef]

__all__ = ("declaring_project_layer", "mcp_servers_declaration_line")

# the system config, then the managed config, which wins (Unix paths, codex-rs/config loader)
_MARKER_SOURCES = (Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"))
_DEFAULT_MARKERS = (".git",)
_TOML_CODE_POINT = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})|\\x([0-9A-Fa-f]{2})")


def declaring_project_layer(
    cwd: str | os.PathLike[str], env: Mapping[str, str], cli_markers: Any = None
) -> tuple[Path, int] | None:
    """The first project-layer ``config.toml`` Codex would read for a run in ``cwd`` under
    ``env`` that declares a top-level ``mcp_servers`` key, with its line, or ``None``.
    ``cli_markers`` is the value a ``-c project_root_markers=...`` override of the run
    gives, ``None`` when no override sets it.

    A file on the walk that exists but cannot be read, a marker source that cannot be
    read or parsed, and a CODEX_HOME or HOME that is not absolute raise
    ``ConfigurationError``: what Codex would load is then unknown. A layer is read as a
    TOML parser reads it, or line by line toward refusing when the parser refuses it
    (``mcp_servers_declaration_line``), and counts whether or not its project is trusted,
    since trust is not fully readable here.
    """
    home = _codex_home(env)
    run_cwd = _config_cwd(Path(os.path.abspath(cwd)))
    root = _project_root(run_cwd, _root_markers(cli_markers))
    for directory in _layer_dirs(run_cwd, root):
        dot_codex = directory / ".codex"
        if _is_codex_home(dot_codex, home):
            continue
        path = dot_codex / "config.toml"
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                f"the Codex project config {path} could not be read ({exc}), so whether it "
                "declares `mcp_servers` is unknown"
            ) from exc
        line_number = mcp_servers_declaration_line(text)
        if line_number is not None:
            return path, line_number
    return None


def _codex_home(env: Mapping[str, str]) -> Path:
    """CODEX_HOME as Codex finds it: the variable as given, canonical, or HOME's ``.codex``
    as given. A relative value would resolve against Codex's process directory, not the
    one checked here, so it is refused; a ``~`` is not expanded by Codex either."""
    value = env.get("CODEX_HOME") or ""
    if value:
        if not os.path.isabs(value):
            raise ConfigurationError(f"CODEX_HOME {value!r} is not an absolute path")
        try:
            return Path(os.path.realpath(value, strict=True))
        except OSError:
            return Path(value)  # Codex stops before reading any layer
    home = env.get("HOME")
    if not home:
        import pwd  # Unix, as these paths are

        home = pwd.getpwuid(os.getuid()).pw_dir
    if not os.path.isabs(home):
        raise ConfigurationError(f"HOME {home!r} is not an absolute path")
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


def _root_markers(cli_markers: Any = None) -> list[str]:
    """``project_root_markers`` as Codex merges them before it looks for the root: the
    system config, then the run's ``-c`` override, then the managed config, the later
    winning; else ``.git``. An empty list means no project root."""
    system, managed = _MARKER_SOURCES
    markers = _file_markers(system)
    if cli_markers is not None:
        markers = _checked_markers(cli_markers, "the -c project_root_markers override")
    managed_markers = _file_markers(managed)
    if managed_markers is not None:
        markers = managed_markers
    return list(_DEFAULT_MARKERS) if markers is None else markers


def _file_markers(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            f"the Codex config {path} could not be read ({exc}), so its project root markers are unknown"
        ) from exc
    try:
        # a document a TOML 1.0 parser refuses refuses here, which Codex's parser might take
        value = _tomllib.loads(text).get("project_root_markers")
    except _tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"the Codex config {path} could not be parsed ({exc}), so its project root markers are unknown"
        ) from exc
    return None if value is None else _checked_markers(value, str(path))


def _checked_markers(value: Any, source: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
        raise ConfigurationError(f"{source}: project_root_markers is not an array of strings")
    return value


def _project_root(cwd: Path, markers: list[str]) -> Path | None:
    """The nearest of ``cwd`` and its ancestors holding a marker. A ``.git`` directory counts
    only with a ``HEAD``; a marker that cannot be read does not, and the search goes on."""
    for ancestor in (cwd, *cwd.parents):
        for marker in markers:
            path = ancestor / marker
            try:
                mode = os.stat(path).st_mode
            except OSError:
                continue
            if marker == ".git" and stat.S_ISDIR(mode):
                try:
                    os.stat(path / "HEAD")
                except OSError:
                    continue
            return ancestor
    return None


def _layer_dirs(cwd: Path, root: Path | None) -> list[Path]:
    """``cwd`` up to ``root``, inclusive; ``cwd`` alone when there is no root."""
    if root is None:
        return [cwd]
    directories = []
    for directory in (cwd, *cwd.parents):
        directories.append(directory)
        if directory == root:
            break
    return directories


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

    Text a TOML 1.0 parser reads is decided by that parse, as Codex decides it: the root
    table holds the key or it does not, whatever a multi-line string or array between
    the lines holds. The line named is the first whose removal changes what the parse
    reads as ``mcp_servers``, so never a line inside a string. Text the parser refuses,
    TOML 1.1 syntax Codex's own parser accepts included, has no structure to trust, so
    every line counts whose first key segment is ``mcp_servers``, a header or a key under
    any table, toward refusing.
    """
    try:
        parsed = _tomllib.loads(text)
    except _tomllib.TOMLDecodeError:
        return next(iter(_declaration_lines(text)), None)
    if "mcp_servers" not in parsed:
        return None
    lines = text.split("\n")
    for number in _declaration_lines(text):
        rest = "\n".join([*lines[: number - 1], "", *lines[number:]])
        try:
            if _tomllib.loads(rest).get("mcp_servers") != parsed["mcp_servers"]:
                return number
        except _tomllib.TOMLDecodeError:
            return number  # the line carries structure the declaration needs
    return 1  # the parse found it: the refusal never waits on its line


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
    parser that takes it is not evaded."""
    key = key.strip()
    if key[:1] == "'":
        end = key.find("'", 1)
        return key[1:end] if end > 0 else key[1:]
    if key[:1] == '"':
        end = key.find('"', 1)
        while end > 0 and key[end - 1] == "\\":
            end = key.find('"', end + 1)
        segment = key[1:end] if end > 0 else key[1:]
        return _TOML_CODE_POINT.sub(
            lambda m: chr(int(m.group(1) or m.group(2) or m.group(3), 16)), segment
        )
    return key.split(".", 1)[0].strip()
