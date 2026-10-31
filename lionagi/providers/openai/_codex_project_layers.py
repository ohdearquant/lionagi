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
``codex exec`` reads its config afresh. A run that keeps the user config checks
the readable user and project layers and disables proven unselected servers by
name, refusing when it cannot prove how a layer is loaded.

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

from lionagi._errors import ConfigurationError, _ExclusiveMcpSelectionError
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
    "refuse_unsafe_exclusive_profile",
    "system_mcp_server_names",
)

# How many candidate lines `mcp_servers_declaration_line` tries before naming the first.
_LINES_TRIED = 8
_SYSTEM_CONFIG = Path("/etc/codex/config.toml")
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
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    allowed: set[str],
    *,
    include_user: bool = False,
) -> set[str]:
    """Find excluded servers only in project layers proven to load.

    The readable system and user configs supply project markers and trust. The
    effective managed and cloud layers are not available to this process.
    """
    from lionagi.providers.openai._codex_profile import _read_codex_config

    def refuse(path: Path, names: set[str], reason: str) -> None:
        suffix = f"; server names: {sorted(names)!r}" if names else ""
        raise _ExclusiveMcpSelectionError(
            f"Cannot enforce an exclusive Codex MCP selection: {path}: {reason}{suffix}."
        )

    configs: list[dict] = []
    for path in (_SYSTEM_CONFIG, _codex_home(env) / "config.toml"):
        try:
            data = _read_codex_config(path)
            if data is None and path.exists():
                refuse(path, set(), "config is not a readable regular file")
        except ValueError:
            refuse(path, set(), "config cannot be read or parsed")
        configs.append(data or {})

    markers: object = [".git"]
    projects: dict = {}
    for config in configs:
        if "project_root_markers" in config:
            markers = config["project_root_markers"]
        if "projects" in config:
            table = config["projects"]
            if not isinstance(table, dict):
                refuse(_codex_home(env) / "config.toml", set(), "projects is not a table")
            for key, record in table.items():
                if not isinstance(record, dict) or record.get("trust_level") not in (
                    None,
                    "trusted",
                    "untrusted",
                ):
                    refuse(_codex_home(env) / "config.toml", set(), "project trust is invalid")
                projects[key] = {**projects.get(key, {}), **record}
    if not isinstance(markers, list) or not all(
        isinstance(marker, str) and marker and "/" not in marker and marker not in (".", "..")
        for marker in markers
    ):
        refuse(_codex_home(env) / "config.toml", set(), "project root markers cannot be resolved")

    run_cwd = _config_cwd(Path(os.path.abspath(cwd)))
    directories = (run_cwd, *run_cwd.parents)
    root = run_cwd
    if markers:
        for directory in directories:
            for marker in markers:
                try:
                    marker_path = directory / marker
                    marker_stat = marker_path.stat()
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError:
                    refuse(directory / marker, set(), "project root marker cannot be inspected")
                if marker == ".git" and stat.S_ISDIR(marker_stat.st_mode):
                    try:
                        (marker_path / "HEAD").stat()
                    except (FileNotFoundError, NotADirectoryError):
                        # Newer Codex skips this marker; older releases accepted it.
                        refuse(marker_path, set(), "project root marker has no HEAD")
                    except OSError:
                        refuse(marker_path, set(), "project root marker cannot be inspected")
                root = directory
                break
            else:
                continue
            break

    repo_uncertain = False
    for directory in directories:
        git = directory / ".git"
        try:
            git.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            refuse(git, set(), "repository root cannot be inspected")
        # Linked worktrees and nested repositories can name a different trust
        # root, so the directory alone cannot prove a repository decision.
        repo_uncertain = True
        break

    def trust(directory: Path) -> str | None:
        # Codex checks the layer directory, then the project and repository roots.
        # https://github.com/openai/codex/blob/55c099a142eb302188fee00ab9e9062f2db1c359/codex-rs/config/src/loader/mod.rs#L1083-L1125
        for candidate in (directory, root):
            for key in dict.fromkeys((str(_canonical(candidate)), str(candidate))):
                level = projects.get(key, {}).get("trust_level")
                if level is not None:
                    return level
        return None

    names: set[str] = set()
    if include_user:
        user_path = _codex_home(env) / "config.toml"
        user_servers: dict = {}
        origins: dict[str, Path] = {}
        for config_path, config in zip((_SYSTEM_CONFIG, user_path), configs, strict=True):
            layer_servers = config.get("mcp_servers", {})
            if not isinstance(layer_servers, dict):
                refuse(config_path, set(), "mcp_servers is not a table")
            for name, fields in layer_servers.items():
                origins[name] = config_path
                prior = user_servers.get(name)
                if isinstance(prior, dict) and isinstance(fields, dict):
                    user_servers[name] = {**prior, **fields}
                else:
                    user_servers[name] = fields
        extra_user = set(user_servers) - allowed
        for name in extra_user:
            if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
                refuse(origins[name], {name}, "server names cannot be represented in an override")
            if not isinstance(user_servers[name], dict) or not (
                {"command", "url"} & user_servers[name].keys()
            ):
                refuse(origins[name], {name}, "server definitions cannot be proven complete")
        names.update(extra_user)
    for path, text in project_layers(cwd, env):
        try:
            parsed = _tomllib.loads(text)
        except (_tomllib.TOMLDecodeError, RecursionError):
            refuse(path, set(), "project config cannot be parsed")
        servers = parsed.get("mcp_servers", {})
        if not isinstance(servers, dict):
            refuse(path, set(), "mcp_servers is not a table")
        extra = set(servers) - allowed
        if not extra:
            continue
        directory = path.parent.parent
        if root not in (directory, *directory.parents):
            refuse(path, extra, "project layer is above the proven project root")
        level = trust(directory)
        if level is None and repo_uncertain:
            refuse(path, extra, "repository trust cannot be determined")
        if level == "trusted":
            if any(re.fullmatch(r"[A-Za-z0-9_-]+", name) is None for name in extra):
                refuse(path, extra, "server names cannot be represented in an override")
            if any(
                not isinstance(servers[name], dict)
                or not ({"command", "url"} & servers[name].keys())
                for name in extra
            ):
                refuse(path, extra, "server definitions cannot be proven complete")
            names.update(extra)
        # A missing or explicitly untrusted decision skips the layer.
    return names


def refuse_unsafe_exclusive_profile(
    cwd: Path, env: Mapping[str, str], allowed: set[str], profile: str | None
) -> None:
    """Check profile selectors and profile layers before an exclusive launch."""
    from ._codex_profile import (
        _read_codex_config,
        codex_config_defining_profile,
        is_generated_codex_profile,
    )

    def read(path: Path, *, required: bool = False) -> dict:
        try:
            data = _read_codex_config(path)
            if data is None and (required or path.exists()):
                raise ValueError
        except ValueError:
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: profile config {path} "
                "cannot be read or parsed."
            ) from None
        return data or {}

    home = _codex_home(env)
    user_path = home / "config.toml"
    layers: list[tuple[Path, dict]] = [(path, read(path)) for path in (_SYSTEM_CONFIG, user_path)]
    for path, text in project_layers(cwd, env):
        try:
            layers.append((path, _tomllib.loads(text)))
        except (_tomllib.TOMLDecodeError, RecursionError):
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: project config {path} "
                "cannot be parsed."
            ) from None
    for path, data in layers:
        if "profile" in data:
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: {path} selects a root profile."
            )

    if profile is None:
        return
    if re.fullmatch(r"[A-Za-z0-9_-]+", profile) is None:
        raise _ExclusiveMcpSelectionError(
            "Cannot enforce an exclusive Codex MCP selection: the selected profile name "
            "cannot be read safely."
        )
    # Legacy ConfigProfile has no MCP or project fields; -p also loads an entire
    # profile config layer: https://github.com/openai/codex/blob/55c099a142eb302188fee00ab9e9062f2db1c359/codex-rs/config/src/profile_toml.rs#L20-L74
    # https://github.com/openai/codex/blob/55c099a142eb302188fee00ab9e9062f2db1c359/codex-rs/config/src/loader/mod.rs#L293-L345
    generated = is_generated_codex_profile(profile)
    if generated:
        for source, table in layers:
            profiles = table.get("profiles", {})
            if not isinstance(profiles, dict) or profile in profiles:
                raise _ExclusiveMcpSelectionError(
                    f"Cannot enforce an exclusive Codex MCP selection: profile {profile!r} "
                    f"may also be defined in {source}."
                )

        try:
            defined = codex_config_defining_profile(profile, env, cwd)
        except ValueError:
            raise _ExclusiveMcpSelectionError(
                "Cannot enforce an exclusive Codex MCP selection: a profile table cannot be read."
            ) from None
        if defined is not None:
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: profile {profile!r} "
                f"is also defined in {defined}."
            )
    path = home / f"{profile}.config.toml"
    data = read(path, required=generated)
    if generated:
        unexpected = set(data) - {"mcp_servers"}
        if unexpected:
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: profile {path} "
                f"has unexpected keys {sorted(unexpected)!r}."
            )
        names = data.get("mcp_servers", {})
        if not isinstance(names, dict) or set(names) - allowed:
            raise _ExclusiveMcpSelectionError(
                f"Cannot enforce an exclusive Codex MCP selection: profile {profile!r} "
                f"at {path} names servers outside the selected set."
            )
        for name, fields in names.items():
            if not isinstance(fields, dict):
                raise _ExclusiveMcpSelectionError(
                    f"Cannot enforce an exclusive Codex MCP selection: "
                    f"profile {path} has invalid mcp_servers.{name}."
                )
            unexpected_fields = set(fields) - {"env", "http_headers"}
            if unexpected_fields:
                raise _ExclusiveMcpSelectionError(
                    f"Cannot enforce an exclusive Codex MCP selection: profile {path} "
                    f"has unexpected keys mcp_servers.{name}.{sorted(unexpected_fields)!r}."
                )
    else:
        for source, table in [(path, data), *layers]:
            profiles = table.get("profiles", {})
            if source != path and not isinstance(profiles, dict):
                raise _ExclusiveMcpSelectionError(
                    f"Cannot enforce an exclusive Codex MCP selection: profiles in {source} "
                    "are not a table."
                )
            profile_table = table if source == path else profiles.get(profile, {})
            if not isinstance(profile_table, dict) or any(
                key in profile_table for key in ("mcp_servers", "projects", "project_root_markers")
            ):
                raise _ExclusiveMcpSelectionError(
                    f"Cannot enforce an exclusive Codex MCP selection: profile {profile!r} "
                    f"at {source} changes MCP servers, project trust or root markers."
                )


def system_mcp_server_names(allowed: set[str]) -> set[str]:
    """Names to disable from the system layer in an exclusive ignore-user run."""
    from ._codex_profile import _read_codex_config

    try:
        data = _read_codex_config(_SYSTEM_CONFIG)
        if data is None and _SYSTEM_CONFIG.exists():
            raise ValueError
    except ValueError:
        raise _ExclusiveMcpSelectionError(
            f"Cannot enforce an exclusive Codex MCP selection: system config {_SYSTEM_CONFIG} "
            "cannot be read or parsed."
        ) from None
    data = data or {}
    if "profile" in data:
        raise _ExclusiveMcpSelectionError(
            f"Cannot enforce an exclusive Codex MCP selection: {_SYSTEM_CONFIG} selects a root profile."
        )
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise _ExclusiveMcpSelectionError(
            f"Cannot enforce an exclusive Codex MCP selection: {_SYSTEM_CONFIG} mcp_servers "
            "is not a table."
        )
    extra = set(servers) - allowed
    if any(
        re.fullmatch(r"[A-Za-z0-9_-]+", name) is None
        or not isinstance(servers[name], dict)
        or not ({"command", "url"} & servers[name].keys())
        for name in extra
    ):
        raise _ExclusiveMcpSelectionError(
            f"Cannot enforce an exclusive Codex MCP selection: server names or definitions "
            f"in {_SYSTEM_CONFIG} cannot be proven: {sorted(extra)!r}."
        )
    return extra


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
