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
  entries, and the network on for a heavy request and off for a light one.
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
)

#: The name of the permission profile lionagi defines on the command line and selects with
#: ``default_permissions``. A config layer defining a table of this name would merge into it.
PERMISSION_PROFILE = "lionagi_child"

#: The command that writes a probe record for the codex binary on PATH.
PROBE_COMMAND = "python -m lionhub.providers.openai.codex_sandbox_probe"

#: The record format this module reads. A record of another format is no record.
_RECORD_FORMAT = 1

#: Arms every passing record carries, each of which must have passed.
REQUIRED_ARMS: tuple[str, ...] = (
    "deny_canary",
    "own_codex_home",
    "read_control",
    "write_cwd",
    "write_entry",
    "write_outside_refused",
    "light_tmp_refused",
    "light_network_off",
    "heavy_network_on",
    "user_layer_sandbox_mode",
)

_GLOB_CHARS = re.compile(r"[*?\[]")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def permission_profile_table(
    *,
    deny: Iterable[str],
    write: Iterable[str],
    light: bool,
    read_only: bool,
) -> dict[str, Any]:
    """The ``[permissions.PERMISSION_PROFILE]`` table for one launch.

    ``deny`` entries are denied to read and to write; a path also named in ``write`` stays
    denied. ``write`` entries are the request's writable roots beyond its working directory.
    A light request also has ``/tmp`` and ``$TMPDIR`` made read-only (codex's ``:workspace``
    leaves both writable) and the network off; a heavy one has the network on. ``read_only``
    starts from codex's ``:read-only`` built-in, which refuses every write, the working
    directory included.
    """
    filesystem: dict[str, str] = {}
    if not read_only:
        for path in write:
            filesystem[path] = "write"
    for path in deny:
        filesystem[path] = "deny"
    if light:
        filesystem[":tmpdir"] = "read"
        filesystem[":slash_tmp"] = "read"
    return {
        "extends": ":read-only" if read_only else ":workspace",
        "filesystem": filesystem,
        "network": {"enabled": not light},
    }


def deny_entries(deny_read: Sequence[str], codex_home: Path) -> list[str]:
    """The absolute paths a profile denies: each ``deny_read`` entry as written, every link it
    passes through and where it resolves, then the child's own ``CODEX_HOME`` the same way.

    An entry holding a glob character is refused: codex reads such an entry as a pattern, and
    whether its profile denies a pattern has not been measured.
    """
    from lionhub.agent.child_sandbox import _written_and_resolved

    entries: list[str] = []
    for entry in (*deny_read, str(codex_home)):
        if _GLOB_CHARS.search(entry):
            raise ConfigurationError(
                f"child_sandbox: deny_read entry {entry!r} is a pattern, which a codex child's "
                "permission profile cannot be shown to deny; name the paths it covers instead"
            )
        for path in _written_and_resolved(os.path.expanduser(entry)):
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


def probe_record_path(resolved: str, version: str) -> Path:
    """The record file for one binary, named by its resolved path and version."""
    digest = hashlib.sha256(f"{resolved}\0{version}".encode()).hexdigest()[:24]
    return probe_record_dir() / f"codex-{version}-{digest}.json"


def probe_record(binary: str) -> tuple[Path, dict[str, Any] | None, str, str | None]:
    """The record for ``binary``: its path, its content (None when absent or unreadable), the
    binary's resolved path and its version (None when unreadable)."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        return probe_record_dir() / "unknown", None, resolved, None
    path = probe_record_path(resolved, version)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return path, None, resolved, version
    if not isinstance(data, dict):
        return path, None, resolved, version
    return path, data, resolved, version


def _failed_arms(record: Mapping[str, Any]) -> list[str]:
    arms = record.get("arms")
    if not isinstance(arms, Mapping):
        return list(REQUIRED_ARMS)
    failed = []
    for name in REQUIRED_ARMS:
        arm = arms.get(name)
        if not isinstance(arm, Mapping) or arm.get("passed") is not True:
            failed.append(name)
    return failed


def require_passing_probe_record(binary: str) -> Path:
    """Refuse a profiled launch on a codex binary without a passing probe record.

    The record must name this binary's resolved path and version, be of the current format, and
    hold every arm in ``REQUIRED_ARMS`` as passed. Returns the record's path.
    """
    path, record, resolved, version = probe_record(binary)
    problem = None
    if version is None:
        problem = f"the version of {binary!r} cannot be read"
    elif record is None:
        problem = f"there is no probe record for codex {version} at {resolved!r} ({str(path)!r})"
    elif (
        record.get("format") != _RECORD_FORMAT
        or record.get("binary") != resolved
        or record.get("version") != version
    ):
        problem = f"the probe record {str(path)!r} is not for codex {version} at {resolved!r}"
    else:
        failed = _failed_arms(record)
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
