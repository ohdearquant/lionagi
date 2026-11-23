# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The permission profile a codex child runs under while the child sandbox is on.

Codex's ``--sandbox`` modes confine writes and allow every read. A permission profile
(``default_permissions`` naming a ``[permissions.<name>]`` table) can also deny reads, so with
``child_sandbox`` enabled lionagi runs a codex child under a profile it builds itself, named
``PERMISSION_PROFILE``, and passes no sandbox mode at all: codex 0.162.1 given both a mode on its
command line and a profile applies the mode and drops the profile without a word, so a second
sandbox setting from any layer would silently undo the denials.

Three things are decided here:

- ``permission_profile_table`` builds the profile: the child sandbox's ``deny_read`` list and the
  child's own ``CODEX_HOME`` denied, the writable roots the request is granted as ``write``
  entries, and the network as the user config's ``network_access`` sets it for a heavy request
  (off when nothing sets it) and off for a light or read-only one.
- ``refuse_a_second_sandbox_setting`` refuses a launch in which anything else could choose the
  child's sandbox: a ``config_overrides`` key that sets a mode or a permission profile, a user
  config that sets ``default_permissions`` or defines a table under the profile's name, and any
  project config codex would read for the run. A ``sandbox_mode`` in the user config is reported
  back: codex reads the command line over it, which the probe record must show for the binary.
- ``probe_record`` reads the record ``codex_sandbox_probe`` writes for one codex binary, keyed by
  its resolved path and its version. A profiled launch runs only on a binary with a passing
  record, because codex reports none of this itself: its startup line prints the legacy mode
  whatever the profile enforces.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lionagi._errors import CLIInputRefusedError, ConfigurationError

__all__ = (
    "PERMISSION_PROFILE",
    "PROBE_COMMAND",
    "codex_binary_version",
    "permission_profile_table",
    "probe_record",
    "probe_record_dir",
    "probe_record_path",
    "refuse_a_second_sandbox_setting",
    "require_passing_probe_record",
    "user_layer_tables",
)

#: The name of the permission profile lionagi defines on the command line and selects with
#: ``default_permissions``. A config layer defining a table of this name would merge into it.
PERMISSION_PROFILE = "lionagi_child"

#: The command that writes a probe record for the codex binary on PATH.
PROBE_COMMAND = "python -m lionhub.providers.openai.codex_sandbox_probe"

#: The record format this module reads. A record of another format is no record. Format 2 adds
#: the SHA-256 of the binary's bytes to the record's key and to the record itself. Format 3 adds
#: the ``read_only_cwd_write_refused`` arm, so a record made before that arm existed no longer
#: satisfies a launch. Format 4 measures the unix-socket check once per network state, in place
#: of the one ``unix_socket_refused`` arm, and adds ``read_only_write_root`` (ADR-0133, "the
#: socket arm per network state"): a record of format 3 or earlier has no socket arm for either
#: state, so it admits no launch. Format 5 reads each socket connect by the token the child prints
#: (``connect=ok`` or ``connect=<errno name>``), brackets each listener run with the probe's own
#: connects from outside the sandbox, and passes a network-off run whose control connect the
#: sandbox refused as well as one whose control connect reached its listener, naming which
#: (ADR-0133, "the network-off socket rule"): a format 4 record measured no such case, so it
#: admits no launch. Format 6 adds the ``scratch_env`` arm (ADR-0041 Amendment 6): a run whose
#: command line hands the child a scratch directory through ``shell_environment_policy.set``, as
#: a launch with a scratch directory does. A format 5 record measured no such run, so it admits
#: no launch.
_RECORD_FORMAT = 6

#: The socket arm a launch needs, by its table's ``network.enabled``.
SOCKET_ARMS: Mapping[bool, str] = {
    True: "unix_socket_refused_network_on",
    False: "unix_socket_refused_network_off",
}

#: The arm a read-only table that grants a write entry needs.
READ_ONLY_WRITE_ROOT_ARM = "read_only_write_root"

#: The arm a launch with a scratch directory needs: the child's commands saw ``TMPDIR``, ``TMP``
#: and ``TEMP`` name the directory and wrote a file there.
SCRATCH_ENV_ARM = "scratch_env"

#: Arms every record carries. A launch needs each to have passed, except the socket arm of the
#: other network state, ``READ_ONLY_WRITE_ROOT_ARM`` unless its table is read-only with a write
#: entry, ``SCRATCH_ENV_ARM`` unless it has a scratch directory (``launch_arms``), and the arms
#: named in ``CONDITIONAL_ARMS``.
REQUIRED_ARMS: tuple[str, ...] = (
    "deny_canary",
    "unix_socket_refused_network_on",
    "unix_socket_refused_network_off",
    "own_codex_home",
    "read_control",
    "write_cwd",
    "write_entry",
    "write_outside_refused",
    "light_tmp_refused",
    "light_tmpdir_refused",
    "light_network_off",
    "heavy_network_on",
    "user_layer_sandbox_mode",
    "read_only_cwd_write_refused",
    "read_only_write_root",
    "scratch_env",
)

#: Arms of ``REQUIRED_ARMS`` that apply only when an environment variable is set: arm name to
#: the variable. The probe records such an arm as skipped (``passed`` false, ``skipped`` true)
#: when the variable is unset in its own environment, and the record then satisfies
#: ``REQUIRED_ARMS`` only for a launch whose environment leaves the variable unset too. A
#: launch that sets it needs the arm to have passed.
CONDITIONAL_ARMS: Mapping[str, str] = {"light_tmpdir_refused": "TMPDIR"}

_GLOB_CHARS = re.compile(r"[*?\[]")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def permission_profile_table(
    *,
    deny: Iterable[str],
    write: Iterable[str],
    light: bool,
    read_only: bool,
    network: bool,
    exclude_slash_tmp: bool = False,
    exclude_tmpdir: bool = False,
    read_only_write: Iterable[str] = (),
) -> dict[str, Any]:
    """The ``[permissions.PERMISSION_PROFILE]`` table for one launch.

    ``deny`` entries are denied to read and to write; a path also named in ``write`` stays
    denied. ``write`` entries are the request's writable roots beyond its working directory.
    A light request also has ``/tmp`` and ``$TMPDIR`` made read-only (codex's ``:workspace``
    leaves both writable) and the network off whatever ``network`` says. ``network`` is the
    caller's setting for a heavy request: the user config's ``network_access``, which codex
    leaves off when nothing sets it. ``read_only``
    starts from codex's ``:read-only`` built-in, which refuses every write, the working
    directory included. ``exclude_slash_tmp`` and ``exclude_tmpdir`` make ``/tmp`` and
    ``$TMPDIR`` read-only for a heavy request, as the legacy workspace-write settings
    ``exclude_slash_tmp`` and ``exclude_tmpdir_env_var`` did; a light request has both anyway.
    ``read_only_write`` are write entries a read-only table grants all the same: a read-only
    launch's scratch directory, and the probe's run for that arm; a launch needs
    ``READ_ONLY_WRITE_ROOT_ARM`` for such a table.
    """
    filesystem: dict[str, str] = {}
    for path in write if not read_only else read_only_write:
        filesystem[path] = "write"
    for path in deny:
        filesystem[path] = "deny"
    if light or exclude_tmpdir:
        filesystem[":tmpdir"] = "read"
    if light or exclude_slash_tmp:
        filesystem[":slash_tmp"] = "read"
    return {
        "extends": ":read-only" if read_only else ":workspace",
        "filesystem": filesystem,
        "network": {"enabled": network and not light},
    }


def _child_home(env: Mapping[str, str] | None) -> str | None:
    """The home a ``~`` names for the child: the absolute ``HOME`` of its environment, else the
    account's home directory. This process's own ``HOME`` is never read."""
    home = env.get("HOME") if env is not None else None
    if isinstance(home, str) and os.path.isabs(home):
        return home
    return _account_home()


def _account_home() -> str | None:
    """The account's home directory from the password database, or ``None`` when it has none."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir or None
    except (ImportError, KeyError, OSError, AttributeError):
        return None


def _deny_homes(env: Mapping[str, str] | None) -> list[str | None]:
    """The homes a home-relative deny entry is placed under: the child's (see ``_child_home``)
    and the account's, both when they differ. A child given its own ``HOME`` still reads the
    account's home through every other path, so an entry denied only under the child's would
    leave the account's copy readable."""
    homes = list(dict.fromkeys((_child_home(env), _account_home())))
    present = [home for home in homes if home is not None]
    return present or [None]


def _expand_home(entry: str, home: str | None) -> str:
    """``entry`` with a leading ``~`` or ``~/`` replaced by ``home``; ``~name`` is left to the
    account database, and an entry that does not start with ``~`` is returned as written."""
    if entry == "~" or entry.startswith("~/"):
        if home is None:
            raise ConfigurationError(
                f"child_sandbox: deny_read entry {entry!r} names a home directory, and neither "
                "the child's HOME nor the account database gives one"
            )
        return home if entry == "~" else os.path.join(home, entry[2:])
    return os.path.expanduser(entry) if entry.startswith("~") else entry


def deny_entries(
    deny_read: Sequence[str],
    codex_home: Path,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """The absolute paths a profile denies: each ``deny_read`` entry as written, the place it
    occupies once its parent directory is resolved (the path the Seatbelt rule places it at),
    every link it passes through and where it resolves, then the child's own ``CODEX_HOME`` and
    the probe record directory the same way: a child that can read or write a record there
    could forge the proof a profiled launch rests on.

    A ``~`` expands against the ``HOME`` of ``env``, the child's environment, and against the
    account's home directory, both when they differ; the parent's ``HOME`` plays no part.

    An entry holding a glob character is refused: codex reads such an entry as a pattern, and
    whether its profile denies a pattern has not been measured.
    """
    from lionhub.agent.child_sandbox import _written_and_resolved

    homes = _deny_homes(env)
    entries: list[str] = []
    for entry in (*deny_read, str(codex_home), str(probe_record_dir())):
        if _GLOB_CHARS.search(entry):
            raise ConfigurationError(
                f"child_sandbox: deny_read entry {entry!r} is a pattern, which a codex child's "
                "permission profile cannot be shown to deny; name the paths it covers instead"
            )
        home_relative = entry == "~" or entry.startswith("~/")
        for home in homes if home_relative else homes[:1]:
            written, *hops = _written_and_resolved(_expand_home(entry, home))
            placed = os.path.join(
                os.path.realpath(os.path.dirname(written)), os.path.basename(written)
            )
            for path in (written, placed, *hops):
                if os.path.isabs(path):
                    entries.append(path)
    return list(dict.fromkeys(entries))


def _tables(flags: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    tables: list[tuple[str, Mapping[str, Any]]] = [("", flags)]
    profiles = flags.get("profiles")
    if isinstance(profiles, Mapping):
        tables += [
            (f"profiles.{name}.", table)
            for name, table in profiles.items()
            if isinstance(table, Mapping)
        ]
    return tables


def _override_reasons(flags: Mapping[str, Any], *, strict: bool = False) -> list[str]:
    """Why the run's own ``config_overrides`` (as codex's loader merges them) set the sandbox.

    ``strict`` is for a table built from keys whose TOML dotted-key reading differs from the
    split-at-dot reading the profile's write entries are taken from: nothing is carried from
    such a key, so ``sandbox_workspace_write.writable_roots`` is refused there too.
    """
    reasons: list[str] = []
    for prefix, table in _tables(flags):
        for key in ("sandbox_mode", "default_permissions", "permissions"):
            if key in table:
                reasons.append(f"config_overrides set {prefix}{key}")
        section = table.get("sandbox_workspace_write")
        if isinstance(section, Mapping):
            for key in section:
                if strict or key != "writable_roots":
                    reasons.append(
                        f"config_overrides set {prefix}sandbox_workspace_write.{key}, which a "
                        "permission profile does not read"
                    )
    return reasons


def _read(config_path: Path) -> dict[str, Any] | None:
    from lionhub.providers.openai._codex_profile import _read_codex_config

    return _read_codex_config(config_path)


_PROFILE_SANDBOX_KEYS = ("sandbox_mode", "default_permissions", "permissions")


def _selected_profile_names(
    *,
    data: Mapping[str, Any] | None,
    profile: str | None,
    flags: Mapping[str, Any],
    decoded_flags: Mapping[str, Any] | None,
) -> list[str]:
    """Every profile name the run could apply, in the order codex would consider them.

    The request's own ``profile``, a ``profile`` override (read as split at dots and as a TOML
    dotted key), and the root ``profile`` key of the user config. A value that is not a string
    names no profile.
    """
    names: list[str] = []
    for value in (
        profile,
        flags.get("profile"),
        (decoded_flags or {}).get("profile"),
        None if data is None else data.get("profile"),
    ):
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    return names


def user_layer_tables(
    *,
    env: Mapping[str, str] | None,
    profile: str | None,
    flags: Mapping[str, Any],
    decoded_flags: Mapping[str, Any] | None = None,
    ignore_user_config: bool = False,
) -> tuple[list[Mapping[str, Any]], str | None]:
    """The user-config tables codex layers under the run's overrides, lowest precedence first,
    and the name of the profile the run selects.

    ``CODEX_HOME/config.toml`` itself, then the ``[profiles.NAME]`` table of the profile the run
    selects, then ``CODEX_HOME/NAME.config.toml``. The selected profile is the first name
    ``_selected_profile_names`` lists: the request's ``profile``, else a ``profile`` override, else
    the user config's root ``profile`` key. A file that is missing or cannot be read adds no table
    (a launch refuses an unreadable one before it gets here). ``ignore_user_config`` drops every
    user-config table, as the codex flag does, leaving only the request's own selection.
    """
    from lionhub.providers.openai._codex_profile import codex_home_for_env, validate_bare_name

    home = codex_home_for_env(env)
    data: dict[str, Any] | None = None
    if not ignore_user_config:
        try:
            data = _read(home / "config.toml")
        except ValueError:
            data = None
    tables: list[Mapping[str, Any]] = [] if data is None else [data]
    names = _selected_profile_names(
        data=data, profile=profile, flags=flags, decoded_flags=decoded_flags
    )
    layered = [] if ignore_user_config else names[:1]
    for name in layered:
        profiles = None if data is None else data.get("profiles")
        table = profiles.get(name) if isinstance(profiles, Mapping) else None
        if isinstance(table, Mapping):
            tables.append(table)
        try:
            validate_bare_name(name, label="codex config profile name")
            file_data = _read(home / f"{name}.config.toml")
        except ValueError:
            continue
        if file_data is not None:
            tables.append(file_data)
    return tables, (names[0] if names else None)


def _selected_profile_reasons(
    *,
    data: Mapping[str, Any] | None,
    config_path: Path,
    env: Mapping[str, str] | None,
    profile: str | None,
    flags: Mapping[str, Any],
    decoded_flags: Mapping[str, Any] | None,
) -> list[str]:
    """Why a profile the run could apply sets the sandbox in the user config or its own file.

    Each candidate name is looked for as a ``[profiles.NAME]`` table in ``config.toml`` and as
    ``CODEX_HOME/NAME.config.toml`` (a bare name only, the rule the codex config profile resolver
    applies). A candidate setting ``sandbox_mode``, ``default_permissions`` or ``permissions`` is
    a reason; a profile file that cannot be read is one too.
    """
    from lionhub.providers.openai._codex_profile import codex_home_for_env, validate_bare_name

    reasons: list[str] = []
    where = repr(str(config_path))
    profiles = None if data is None else data.get("profiles")
    names = _selected_profile_names(
        data=data, profile=profile, flags=flags, decoded_flags=decoded_flags
    )
    for name in names:
        table = profiles.get(name) if isinstance(profiles, Mapping) else None
        if isinstance(table, Mapping):
            for key in _PROFILE_SANDBOX_KEYS:
                if key in table:
                    reasons.append(f"{where} sets {key} in the profile {name!r} the run selects")
        try:
            validate_bare_name(name, label="codex config profile name")
        except ValueError:
            continue
        file_path = codex_home_for_env(env) / f"{name}.config.toml"
        try:
            file_data = _read(file_path)
        except ValueError as exc:
            reasons.append(str(exc))
            continue
        if file_data is not None:
            for key in _PROFILE_SANDBOX_KEYS:
                if key in file_data:
                    reasons.append(
                        f"{str(file_path)!r} sets {key} in the profile {name!r} the run selects"
                    )
    return reasons


def refuse_a_second_sandbox_setting(
    *,
    flags: Mapping[str, Any],
    env: Mapping[str, str] | None,
    cwd: Path,
    profile: str | None,
    ignore_user_config: bool,
    decoded_flags: Mapping[str, Any] | None = None,
) -> Path | None:
    """Refuse a profiled launch in which another layer could set the child's sandbox.

    ``flags`` is the run's ``config_overrides`` as codex merges them (keys split at every
    ``.``). ``decoded_flags`` is the same overrides read as TOML dotted keys, limited to the
    keys the two readings split differently (quoted, spaced or escaped parts); a guarded key
    named by either reading is refused. Refused, with
    ``CLIInputRefusedError``:

    - an override setting ``sandbox_mode``, ``default_permissions`` or ``permissions``, at the
      root or in a profile table, or a ``sandbox_workspace_write`` key other than
      ``writable_roots`` (which the profile carries as write entries);
    - unless the run ignores the user config: a user config (``CODEX_HOME/config.toml``) that
      sets ``default_permissions``, defines ``[permissions.PERMISSION_PROFILE]`` (codex merges a
      config table into the command line's path by path, so it could add a writable path), sets
      ``sandbox_mode = "danger-full-access"``; and a user config that cannot be read;
    - unless the run ignores the user config, for every profile the run could apply (the
      request's ``profile``, a ``profile`` override read either way, the user config's root
      ``profile`` key): a ``[profiles.NAME]`` table in the user config, or a
      ``CODEX_HOME/NAME.config.toml`` file, that sets any of ``sandbox_mode``,
      ``default_permissions`` or ``permissions``;
    - any project config (``.codex/config.toml`` in the working directory or above it, other
      than the one in ``CODEX_HOME``) codex would read for the run. Its text is the reviewed
      tree's, and codex's precedence measured on one binary is no control over it. Project
      layers that cannot be determined are refused too.

    Returns the user config path when it sets a root ``sandbox_mode`` (other than
    ``danger-full-access``), else None. Codex reads the command line's profile over that, and
    the launch relies on the probe record showing it for the binary.
    """
    from lionhub.providers.openai._codex_profile import codex_home_for_env
    from lionhub.providers.openai._codex_project_layers import project_layers

    reasons = _override_reasons(flags)
    if decoded_flags:
        reasons.extend(r for r in _override_reasons(decoded_flags, strict=True) if r not in reasons)
    user_mode: Path | None = None
    if not ignore_user_config:
        config_path = codex_home_for_env(env) / "config.toml"
        try:
            data = _read(config_path)
        except ValueError as exc:
            reasons.append(str(exc))
            data = None
        if data is not None:
            where = repr(str(config_path))
            if data.get("default_permissions") is not None:
                reasons.append(f"{where} sets default_permissions")
            permissions = data.get("permissions")
            if isinstance(permissions, Mapping) and PERMISSION_PROFILE in permissions:
                reasons.append(
                    f"{where} defines [permissions.{PERMISSION_PROFILE}], which codex merges "
                    "into the profile lionagi sets"
                )
            mode = data.get("sandbox_mode")
            if mode == "danger-full-access":
                reasons.append(f"{where} sets sandbox_mode = 'danger-full-access'")
            elif mode is not None:
                user_mode = config_path
        reasons.extend(
            _selected_profile_reasons(
                data=data,
                config_path=config_path,
                env=env,
                profile=profile,
                flags=flags,
                decoded_flags=decoded_flags,
            )
        )
    try:
        found = [path for path, _ in project_layers(cwd, os.environ if env is None else env)]
    except ConfigurationError as exc:
        reasons.append(f"the project codex configs for {str(cwd)!r} cannot be determined: {exc}")
        found = []
    if found:
        reasons.append(
            f"codex would read the project config {str(found[0])!r}, text the working tree "
            "carries, which can add to the child's sandbox"
        )
    if reasons:
        raise CLIInputRefusedError(
            "a codex child runs under lionagi's permission profile while the child sandbox is "
            "enabled, and nothing else may set its sandbox; "
            + "; ".join(reasons)
            + ". To run codex children without it, set `child_sandbox: {enabled: false}` in "
            "~/.lionagi/settings.yaml."
        )
    return user_mode


def probe_record_dir() -> Path:
    """Where probe records live: ``LIONAGI_HOME/codex-sandbox-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "codex-sandbox-probes"


def codex_binary_version(binary: str) -> str | None:
    """The first ``N.N.N`` ``binary --version`` prints, or None when it cannot be read."""
    try:
        proc = subprocess.run(  # noqa: S603 -- the resolved CLI binary and a literal flag
            [binary, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = _VERSION.search(proc.stdout)
    return found.group(0) if found else None


def binary_sha256(path: str) -> str | None:
    """The SHA-256 of the file at ``path`` as hex, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def probe_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The record file for one binary, named by its resolved path, version and the SHA-256 of
    its bytes: a binary replaced in place under the same path and version has no record."""
    digest = hashlib.sha256(f"{resolved}\0{version}\0{sha256}".encode()).hexdigest()[:24]
    return probe_record_dir() / f"codex-{version}-{digest}.json"


def probe_record(binary: str) -> tuple[Path, dict[str, Any] | None, str, str | None]:
    """The record for ``binary``: its path, its content (None when absent or unreadable), the
    binary's resolved path and its version (None when unreadable)."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        return probe_record_dir() / "unknown", None, resolved, None
    sha256 = binary_sha256(resolved)
    if sha256 is None:
        return probe_record_dir() / "unknown", None, resolved, version
    path = probe_record_path(resolved, version, sha256)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return path, None, resolved, version
    if not isinstance(data, dict):
        return path, None, resolved, version
    return path, data, resolved, version


def refuse_write_roots_on_probe_record_dir(
    roots: Iterable[str], *, what: str = "the write root"
) -> None:
    """Refuse a write root that contains the probe record directory or lies inside it.

    A child that can write under the record directory can forge the record a launch rests on,
    and a root above it grants that write. The written and the resolved spelling of each side
    are compared, so a link does not hide the overlap. ``what`` names the root in the refusal.
    """
    record = probe_record_dir()
    record_paths = {Path(os.path.abspath(record)), Path(os.path.realpath(record))}
    for root in roots:
        for root_path in {Path(os.path.abspath(root)), Path(os.path.realpath(root))}:
            for record_path in record_paths:
                if (
                    root_path == record_path
                    or root_path in record_path.parents
                    or record_path in root_path.parents
                ):
                    raise CLIInputRefusedError(
                        f"{what} {str(root)!r} contains or lies inside the codex probe "
                        f"record directory {str(record)!r}; a child that can write there could "
                        "forge the record a profiled launch rests on. Name a directory outside "
                        "it."
                    )


def refuse_write_roots_on_denied_paths(
    roots: Iterable[str], deny: Iterable[str], *, what: str = "the write root"
) -> None:
    """Refuse a write root that is a denied location, holds one or lies inside one.

    ``deny`` is the profile's deny list (``deny_entries``: each ``deny_read`` entry in all its
    forms, the child's ``CODEX_HOME`` and the probe record directory). A root that overlaps an
    entry would put a write grant and a read denial on the same path in one profile, and
    whether the denial wins over the grant is not measured, so the launch is refused instead.
    The written and the resolved spelling of each side are compared, so a link does not hide
    the overlap. ``what`` names the root in the refusal.
    """
    denied = [
        (entry, path)
        for entry in deny
        for path in dict.fromkeys((Path(os.path.abspath(entry)), Path(os.path.realpath(entry))))
    ]
    for root in roots:
        for root_path in dict.fromkeys((Path(os.path.abspath(root)), Path(os.path.realpath(root)))):
            for entry, path in denied:
                if root_path == path or root_path in path.parents or path in root_path.parents:
                    raise CLIInputRefusedError(
                        f"{what} {str(root)!r} is, contains or lies inside {entry!r}, which "
                        "the codex child is denied reading; the profile would grant writes "
                        "where it denies reads, and which one holds is not measured. Name a "
                        "directory that neither holds nor lies inside a denied location."
                    )


def launch_arms(table: Mapping[str, Any] | None, *, scratch: bool = False) -> tuple[str, ...]:
    """The arms of ``REQUIRED_ARMS`` a launch rendering ``table`` needs to have passed.

    Every arm except the socket arm of the network state the table does not have,
    ``READ_ONLY_WRITE_ROOT_ARM`` unless the table extends ``:read-only`` and grants a write
    entry, and ``SCRATCH_ENV_ARM`` unless ``scratch`` says the launch hands its child a scratch
    directory. A table whose ``network.enabled`` is not a bool, or no table at all, needs every
    arm, so leaving the table out can only refuse more.
    """
    if not isinstance(table, Mapping):
        return REQUIRED_ARMS
    network = table.get("network")
    enabled = network.get("enabled") if isinstance(network, Mapping) else None
    if not isinstance(enabled, bool):
        return REQUIRED_ARMS
    skip = {SOCKET_ARMS[not enabled]}
    files = table.get("filesystem")
    grants_write = isinstance(files, Mapping) and "write" in files.values()
    if not (table.get("extends") == ":read-only" and grants_write):
        skip.add(READ_ONLY_WRITE_ROOT_ARM)
    if not scratch:
        skip.add(SCRATCH_ENV_ARM)
    return tuple(name for name in REQUIRED_ARMS if name not in skip)


def _failed_arms(
    record: Mapping[str, Any],
    env: Mapping[str, str],
    table: Mapping[str, Any] | None = None,
    *,
    scratch: bool = False,
) -> list[str]:
    """The arms of ``launch_arms(table, scratch=scratch)`` that did not pass for a launch with
    environment ``env``.

    A conditional arm recorded as skipped counts as satisfied only when ``env`` leaves its
    variable unset or empty; a launch that sets it needs the arm to have passed.
    """
    needed = launch_arms(table, scratch=scratch)
    arms = record.get("arms")
    if not isinstance(arms, Mapping):
        return list(needed)
    failed = []
    for name in needed:
        arm = arms.get(name)
        if not isinstance(arm, Mapping):
            failed.append(name)
        elif arm.get("passed") is True:
            continue
        elif (
            name in CONDITIONAL_ARMS
            and arm.get("skipped") is True
            and not env.get(CONDITIONAL_ARMS[name])
        ):
            continue
        else:
            failed.append(name)
    return failed


def _launch_binary(binary: str, env: Mapping[str, str], cwd: str | os.PathLike[str] | None) -> str:
    """``binary`` resolved the way the spawn resolves it: on the ``PATH`` in ``env``."""
    from lionhub.providers._cli_subprocess import resolve_cli_program

    try:
        found = resolve_cli_program(binary, cwd=Path(cwd) if cwd is not None else None, env=env)
    except Exception:  # noqa: BLE001 -- an unresolvable name is reported by the record lookup
        return binary
    return found or binary


def _socket_refusal(arm: str, version: str, resolved: str, *, other_passed: bool) -> str:
    """Why a launch is refused when its network state's socket arm did not pass, and what the
    remedies are: the binary, the arm, and either the settings that turn the network off or
    the fact that no setting admits the launch on this binary. ``other_passed`` says whether
    the record shows the other network state's socket arm as passed: when a network-on launch
    is refused and the network-off arm did not pass either, turning the network off admits
    nothing, so no setting is offered."""
    head = (
        f"codex {version} at {resolved!r} did not pass {arm}: the probe did not show a child of a "
        f"run with the network {'on' if arm == SOCKET_ARMS[True] else 'off'} refused by the "
        "sandbox when it connects to a unix socket inside a denied directory, so a listener there "
        "(a credential agent, a local daemon) may answer it."
    )
    if arm == SOCKET_ARMS[True] and not other_passed:
        return (
            f"{head} It did not pass {SOCKET_ARMS[False]} either, so no setting admits a profiled "
            f"launch on this binary; probe another codex version with "
            f"`{PROBE_COMMAND} --binary <path>`."
        )
    if arm == SOCKET_ARMS[True]:
        return (
            f"{head} Set `sandbox_workspace_write.network_access = false` in the codex config, or "
            "run the role with `workload: light`, so the launch runs with the network off."
        )
    return (
        f"{head} No setting admits a launch with the network off on this binary; probe another "
        f"codex version with `{PROBE_COMMAND} --binary <path>`."
    )


def require_passing_probe_record(
    binary: str,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    *,
    table: Mapping[str, Any] | None = None,
    scratch: bool = False,
) -> Path:
    """Refuse a profiled launch on a codex binary without a passing probe record.

    ``binary`` is resolved against the ``PATH`` in ``env`` (the child's final environment, the
    process environment when None) with the lookup the spawn uses, and ``cwd`` is the child's
    working directory. ``table`` is the permission profile table the launch renders, and
    ``scratch`` says whether the launch hands its child a scratch directory. The record must
    name the resolved binary's path, its version and the SHA-256 of its bytes, be of the
    current format, and hold every arm of ``launch_arms(table, scratch=scratch)`` as passed, a
    conditional arm being allowed to be skipped as ``CONDITIONAL_ARMS`` says. Nothing but the
    record, the table and ``scratch`` decides the answer. Returns the record's path.
    """
    launch_env = os.environ if env is None else env
    binary = _launch_binary(binary, launch_env, cwd)
    path, record, resolved, version = probe_record(binary)
    problem = None
    if version is None:
        problem = f"the version of {binary!r} cannot be read"
    elif record is None:
        problem = f"there is no probe record for codex {version} at {resolved!r} ({str(path)!r})"
    elif record.get("format") != _RECORD_FORMAT:
        problem = (
            f"the probe record {str(path)!r} is of format {record.get('format')!r}, and a launch "
            f"reads format {_RECORD_FORMAT} only"
        )
    elif (
        record.get("binary") != resolved
        or record.get("version") != version
        or record.get("sha256") != binary_sha256(resolved)
    ):
        problem = f"the probe record {str(path)!r} is not for codex {version} at {resolved!r}"
    else:
        failed = _failed_arms(record, launch_env, table, scratch=scratch)
        sockets = [name for name in failed if name in SOCKET_ARMS.values()]
        if failed and len(sockets) == len(failed) == 1:
            # The one arm a setting may answer: its own remedies, and no others. The record also
            # says whether the other state's arm passed, which decides whether a setting helps.
            network_on = sockets[0] == SOCKET_ARMS[True]
            arms = record.get("arms")
            other = arms.get(SOCKET_ARMS[not network_on]) if isinstance(arms, Mapping) else None
            other_passed = isinstance(other, Mapping) and other.get("passed") is True
            raise CLIInputRefusedError(
                "a codex child runs under lionagi's permission profile while the child sandbox "
                "is enabled, and only on a codex binary shown to enforce it; "
                + _socket_refusal(sockets[0], version, resolved, other_passed=other_passed)
            )
        if failed:
            problem = (
                f"the probe record {str(path)!r} has arms that did not pass: {', '.join(failed)}"
            )
    if problem is not None:
        raise CLIInputRefusedError(
            "a codex child runs under lionagi's permission profile while the child sandbox is "
            f"enabled, and only on a codex binary shown to enforce it; {problem}. Run "
            f"`{PROBE_COMMAND}` to probe this binary, or set `child_sandbox: {{enabled: false}}` "
            "in ~/.lionagi/settings.yaml."
        )
    return path
