# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resolution of a codex config profile named as a model.

Lives at the provider layer, not under ``cli/``, so both the library and CLI
entry points reach it. See
docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError
from lionagi.libs.path_safety import validate_bare_name

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10: the declared `tomli`; the locked 2.4.1 reads TOML 1.1
    import tomli as _tomllib  # type: ignore[no-redef]

__all__ = (
    "CODEX_MCP_PROFILE_PREFIX",
    "codex_config_defining_profile",
    "codex_config_setting_default_permissions",
    "codex_home_for_env",
    "configured_codex_profile",
    "global_codex_reasoning_effort",
    "is_generated_codex_profile",
    "project_codex_profile",
    "refuse_codex_profile_run_settings",
    "refuse_non_absolute_codex_home",
    "resolve_codex_config_profile",
)

logger = logging.getLogger("lionagi.providers.openai._codex_profile")

# Fixed prefix + 32 hex chars marks a profile as lionagi-generated (the name
# has to carry this since a resumed leg's profile was written by a different
# process). A caller name in this exact shape is treated as ours and
# replaced; anything else is the caller's and refused, never overwritten.
CODEX_MCP_PROFILE_PREFIX = "lionagi-mcp-"
_GENERATED_CODEX_PROFILE_RE = re.compile(rf"^{re.escape(CODEX_MCP_PROFILE_PREFIX)}[0-9a-f]{{32}}$")


def is_generated_codex_profile(name: object) -> bool:
    """Whether *name* has the shape lionagi gives the profiles it writes for MCP server secrets.

    lionagi writes those with only ``mcp_servers``. The check reads the name, not the file.
    """
    return isinstance(name, str) and _GENERATED_CODEX_PROFILE_RE.fullmatch(name) is not None


def _is_file(path: Path) -> bool:
    """True for a regular file, False when nothing is there; OSError when it cannot be checked.

    ``Path.is_file()`` answers False for a permission error from Python 3.14 on, and raises
    it before that, so an unreadable profile or config would read as an absent one on some
    supported versions only. This keeps the earlier meaning on all of them. A path the
    operating system cannot name (an embedded NUL) is not a file, as ``Path.is_file()`` says.
    """
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except ValueError:
        return False
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EBADF):
            return False
        raise


def _unreadable_symlink_target(path: Path) -> str | None:
    """Return a broken/non-file symlink's declared target, if applicable."""
    try:
        if not path.is_symlink() or _is_file(path):
            return None
    except OSError:
        return "<unreadable>"
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


def resolve_codex_config_profile(
    model: str, env: Mapping[str, str] | None = None
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a codex model part that names a codex config profile.

    Only a bare name is looked up (no dots/separators), so an ordinary vendor
    model id is never treated as a profile path. Table-valued keys and any key
    with an ``mcp_servers`` segment (a quoted dotted one included) are not
    applied, and are logged instead. Returns
    ``None`` when no such profile file exists, leaving the name to be
    treated as a model id exactly as before. See
    docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.

    *env* is the environment the CHILD will run under. A request may carry a
    complete replacement environment, and the profile that decides which model
    actually runs lives in the codex home THAT environment names. Reading our
    own ``CODEX_HOME`` instead resolved the name against the wrong directory and
    silently selected a different model. ``None`` means the child inherits this
    process's environment, which is the ordinary CLI case.
    """
    try:
        validate_bare_name(model, label="codex config profile name")
    except ValueError:
        return None

    codex_home = codex_home_for_env(env)
    profile_path = codex_home / f"{model}.config.toml"
    try:
        is_file = _is_file(profile_path)
    except OSError as exc:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} could not be checked "
            f"({type(exc).__name__}: {exc}). Fix its permissions; running without it "
            f"would send {model!r} to codex as a model id instead."
        ) from exc
    if not is_file:
        # A broken symlink is not the same as no profile: is_file() answers
        # False for both, and falling through would send the name to codex as
        # a literal model id.
        broken_target = _unreadable_symlink_target(profile_path)
        if broken_target is not None:
            raise ValueError(
                f"codex config profile {str(profile_path)!r} is a symlink whose "
                f"target {broken_target!r} is unreadable. Repair or remove the "
                f"link; running without it would send {model!r} to codex as a "
                f"model id and silently run something else."
            )
        return None

    import toml

    try:
        data = toml.loads(profile_path.read_text())
    except (OSError, UnicodeError, RecursionError, toml.TomlDecodeError) as exc:
        # UnicodeError: read_text() decodes. RecursionError: the parser recurses
        # per nesting level. Either way the file is unreadable, and the refusal
        # below names it rather than letting the error escape request building.
        raise ValueError(
            f"codex config profile {str(profile_path)!r} could not be read "
            f"({type(exc).__name__}: {exc}). Fix or remove the file; running "
            f"without it would send {model!r} as a model id instead."
        ) from exc

    resolved = data.get("model")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} declares no 'model'. "
            f"Add one, or use a model id instead of the profile name — "
            f"without it {model!r} would be sent to codex as a model id and "
            f"silently run something other than the profile."
        )

    overrides: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in data.items():
        if key == "model":
            continue
        # A quoted key such as "mcp_servers.x.command" is a scalar to TOML, but
        # as a `-c` override codex splits it on the dots and declares a server,
        # so any key with an mcp_servers segment is dropped, however it is
        # spaced or quoted. A key carrying a backslash could spell that segment
        # as an escape, and no setting codex takes needs one, so it goes too.
        if "\\" in key or "mcp_servers" in (
            segment.strip().strip("\"'").strip() for segment in key.split(".")
        ):
            skipped.append(key)
            continue
        if isinstance(value, (str, int, float, bool)):
            overrides[key] = value
        else:
            skipped.append(key)
    if skipped:
        logger.warning(
            "codex config profile %r: ignoring %s — lionagi applies a profile's "
            "model and scalar settings, and sets a leg's MCP servers itself",
            model,
            ", ".join(sorted(skipped)),
        )

    logger.info("codex profile %r resolves to model %r", model, resolved)
    return resolved, overrides


def refuse_codex_profile_run_settings(name: str, overrides: Mapping[str, Any]) -> None:
    """Refuse a profile setting more than its model, for a request that ignores the user config.

    The request names the profile, so its model settings still apply: every key whose
    first segment starts with ``model_``, provider routing (``model_providers.*``)
    included. Any other key (``sandbox_mode``, ``approval_policy``,
    ``shell_environment_policy``) would set what the run decides for itself.
    """
    extra = sorted(
        key
        for key in overrides
        if not key.split(".")[0].strip().strip("\"'").strip().startswith("model_")
    )
    if extra:
        raise ConfigurationError(
            f"codex config profile {name!r} sets {', '.join(extra)} besides its model "
            "settings, and this request ignores the user config, so the run sets its own "
            "sandbox, approvals and environment. Remove those keys from the profile, or "
            "name a profile that sets only its model and provider."
        )


def refuse_non_absolute_codex_home(env: Mapping[str, str]) -> None:
    """Refuse a CODEX_HOME, or a HOME when CODEX_HOME is unset, that is not absolute.

    Both this process's environment and a request's environment are read by this
    check. A value that is not absolute would otherwise name a different directory
    depending on where it is resolved, and no reader here needs to agree with the
    child about which one that is: the value is refused instead.
    """
    value = env.get("CODEX_HOME")
    if value and not os.path.isabs(value):
        raise ConfigurationError(
            f"CODEX_HOME {value!r} is not an absolute path: it names a different "
            "codex home depending on where it is resolved."
        )
    if not value:
        home = env.get("HOME")
        if home and not os.path.isabs(home):
            raise ConfigurationError(
                f"HOME {home!r} is not an absolute path: it names a different codex home "
                "depending on where it is resolved."
            )


def codex_home_for_env(env: Mapping[str, str] | None) -> Path:
    """The codex home the process running under ``env`` will read.

    A request may carry a complete REPLACEMENT environment for the child, so
    the config codex actually reads is the one named by THAT environment, not
    by ours. ``HOME`` is read from the same mapping for the same reason: an env
    that redirects HOME redirects the default codex home with it.

    An environment naming NEITHER does not mean there is no config: the child
    runs as the same OS account, and codex resolves its home from the account
    when ``HOME`` is unset (``env -i PATH=... codex doctor`` reports the
    account's ``~/.codex`` with no ``HOME`` in its environment). Answering "no config" for
    that case is the original 400 with extra steps, so it falls through to the
    ACCOUNT home. Not ``Path.home()``: that reads ``$HOME`` first and only then
    the password database, so a parent whose own ``HOME`` is redirected would
    answer about a directory the child never looks at.

    A ``CODEX_HOME``, or a ``HOME`` when ``CODEX_HOME`` is unset, that is not an
    absolute path is refused before it is read, from either this process's
    environment or a request's.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    refuse_non_absolute_codex_home(source)
    override = source.get("CODEX_HOME")
    if override:
        return Path(override)
    home = source.get("HOME")
    return (Path(home) if home else _account_home()) / ".codex"


def _account_home() -> Path:
    """The OS account's home directory, independent of this process's ``$HOME``.

    This is the fallback a child with no ``HOME`` in its environment resolves,
    so it is read from the password database rather than from our own
    environment. ``Path.home()`` is the wrong instrument here for exactly the
    reason it is the right one elsewhere: it honours ``$HOME``.
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        # No password database (non-POSIX, or a uid with no passwd entry).
        # Falling back to $HOME is strictly better than failing the spawn.
        return Path.home()


def global_codex_reasoning_effort(env: Mapping[str, str] | None = None) -> str | None:
    """Return ``model_reasoning_effort`` from the global codex config, or None.

    The global value is tuned for the config's own default model, not for
    whatever model a given spawn resolved — callers must clamp it to the
    resolved model before emitting it. An absent or unreadable config answers
    None (codex's own built-in default applies); unreadability is not an error
    here because nothing was requested — this is a fallback read. A
    ``CODEX_HOME``, or a ``HOME`` when ``CODEX_HOME`` is unset, that is not an
    absolute path raises ``ConfigurationError`` before any read, since it names
    no fixed directory.

    ``env`` is the environment the SPAWNED process will run under, when the
    caller has one. Reading our own environment instead answers about the wrong
    config file in both directions: it can clamp against a value the child will
    never see, and it can leave unclamped a value the child will.
    """
    config_path = codex_home_for_env(env) / "config.toml"
    try:
        if not config_path.is_file():
            return None
    except OSError:
        return None

    import toml

    try:
        data = toml.loads(config_path.read_text())
    except (OSError, UnicodeError, RecursionError, toml.TomlDecodeError):
        # UnicodeError: read_text() decodes; a non-UTF-8 config is as
        # unreadable as an unparseable one and must not crash the spawn.
        # RecursionError: the parser recurses per nesting level, so a deeply
        # nested config must read as unreadable rather than crash the spawn.
        return None
    value = data.get("model_reasoning_effort")
    return value if isinstance(value, str) and value else None


def _read_codex_config(config_path: Path) -> dict[str, Any] | None:
    try:
        if not _is_file(config_path):
            return None
        return _tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, RecursionError, _tomllib.TOMLDecodeError) as e:
        raise ValueError(f"cannot read the codex config {str(config_path)!r}: {e}") from None


def _root_profile(config_path: Path) -> Any:
    data = _read_codex_config(config_path)
    return None if data is None else data.get("profile")


def _project_config_dirs(cwd: Path) -> list[Path]:
    """The directories whose ``.codex/config.toml`` codex before 0.135 can merge for a child run
    in ``cwd``: from ``cwd`` up to its git root, every ancestor when there is no git root. The
    walk is made over the path as spelled, with ``..`` collapsed, and over the resolved path, since
    a symlink or a ``..`` in ``cwd`` puts the two in different trees."""
    dirs: list[Path] = []
    for start in (Path(os.path.abspath(cwd)), Path(cwd).resolve()):
        for directory in (start, *start.parents):
            if directory not in dirs:
                dirs.append(directory)
            if (directory / ".git").exists():
                break
    return dirs


def configured_codex_profile(env: Mapping[str, str] | None = None) -> Any:
    """Return the root ``profile`` key of the codex config, or None when it has none.

    Codex before 0.135 makes that profile the active one when no ``-p`` is given; from 0.135
    codex refuses to start on the key. ``env`` is the environment the spawned process will run
    under, as for :func:`global_codex_reasoning_effort`. An absent config answers None. A config
    that cannot be read or parsed raises ValueError: a caller deciding which profile codex will
    apply cannot read it as one that names none.
    """
    return _root_profile(codex_home_for_env(env) / "config.toml")


def project_codex_profile(cwd: Path) -> tuple[Path, Any] | None:
    """The nearest project ``.codex/config.toml`` that sets a root ``profile`` key, and the key.

    Codex before 0.135 merges a trusted project's ``.codex/config.toml`` files over the user
    config, so a root ``profile`` key there selects the active profile too. Every such file from
    ``cwd`` up to its git root is read, and every ancestor's when there is no git root; trust is
    not read. None when no file sets the key. A file that cannot be read or parsed raises
    ValueError, as in :func:`configured_codex_profile`.
    """
    for directory in _project_config_dirs(cwd):
        config_path = directory / ".codex" / "config.toml"
        profile = _root_profile(config_path)
        if profile is not None:
            return config_path, profile
    return None


def codex_config_defining_profile(
    name: str, env: Mapping[str, str] | None, cwd: Path
) -> Path | None:
    """The first codex config that defines a ``[profiles.<name>]`` table, or None.

    Codex before 0.135 reads ``-p <name>`` as that table in the configs it merges: the user config
    (found as for :func:`configured_codex_profile`) and the project configs
    :func:`project_codex_profile` reads. A file that cannot be read or parsed raises ValueError.
    """
    paths = [codex_home_for_env(env) / "config.toml"]
    paths += [directory / ".codex" / "config.toml" for directory in _project_config_dirs(cwd)]
    for config_path in paths:
        data = _read_codex_config(config_path)
        profiles = None if data is None else data.get("profiles")
        if isinstance(profiles, dict) and name in profiles:
            return config_path
    return None


def codex_config_setting_default_permissions(
    env: Mapping[str, str] | None, cwd: Path
) -> Path | None:
    """The first codex config that sets a root ``default_permissions`` key, or None.

    The key names a permissions profile codex applies in place of ``sandbox_mode``. The configs
    read are those :func:`codex_config_defining_profile` reads. A file that cannot be read or
    parsed raises ValueError.
    """
    paths = [codex_home_for_env(env) / "config.toml"]
    paths += [directory / ".codex" / "config.toml" for directory in _project_config_dirs(cwd)]
    for config_path in paths:
        data = _read_codex_config(config_path)
        if data is not None and data.get("default_permissions") is not None:
            return config_path
    return None
