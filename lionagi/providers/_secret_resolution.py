# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fill declared secrets into a spawned CLI child's environment.

A CLI provider authenticates from its own process environment: a codex model
provider names an ``env_key`` and the CLI reads that variable itself. When the
secret is kept somewhere other than the environment -- a keychain, a password
manager, a vault agent -- the spawning process has no value to pass on, and
the child dies with a missing-variable error that says nothing about where the
value was meant to come from.

``secrets.lookup`` in ``~/.lionagi/settings.yaml`` names a command that prints
one secret to stdout, and the variables it may be asked for::

    secrets:
      lookup:
        argv: [security, find-generic-password, -s, "{name}", -a, lionagi, -w]
        names: [OPENROUTER_API_KEY]

Read from the **global** settings file only, never the project-local one. The
merged project file is content of whatever tree happens to be checked out, and
a repository has no business naming the program that reads this machine's
secrets. Where the value lives is a property of the machine, so it is
configured once, where the machine is configured.

The resolved value reaches the child through its environment and nowhere else:
never a file, never an argv, never a log line. Failures are best-effort and
additive -- a lookup that does not work leaves the environment exactly as it
would have been, and the child fails the way it already failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lionagi.ln._proc import aterminate_process_group
from lionagi.ln.concurrency import CancelScope, get_cancelled_exc_class

__all__ = (
    "ResolvedSecretLookup",
    "SecretLookupResolution",
    "fill_declared_secrets",
    "resolve_secret_lookup_config",
)

logger = logging.getLogger(__name__)

# POSIX-portable environment variable name. The names are the one part of the
# configuration interpolated into an argument, so a name that is not one is
# refused rather than passed to the command.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The token in each argument replaced with the variable being looked up.
_NAME_PLACEHOLDER = "{name}"

# A lookup runs on the spawn path, so its budget is what a caller will accept
# waiting before the CLI child starts. A keychain that wants to prompt will
# exceed this, which is deliberate: the prompt is answered once, out of band,
# and the variable exported, rather than blocking every leg.
_LOOKUP_TIMEOUT_SECONDS = 15.0

_ALLOWED_KEYS = frozenset({"argv", "names", "enabled"})


@dataclass(frozen=True)
class ResolvedSecretLookup:
    """A validated lookup: a fixed program, and the names it may be asked for."""

    argv: tuple[str, ...]
    names: tuple[str, ...]


@dataclass(frozen=True)
class SecretLookupResolution:
    """The outcome of resolving ``secrets.lookup``: a lookup, or why not.

    ``reason`` is set iff a lookup was asked for and refused. Chosen silence
    (nothing configured, or ``enabled: false``) carries no reason, so a
    misconfigured lookup stays distinguishable from an absent one instead of
    both arriving as "no secrets were filled". Reasons are short stable
    identifiers and never interpolate configured values; the offending value
    goes in the matching warning.
    """

    lookup: ResolvedSecretLookup | None = None
    reason: str | None = None


# Nothing was configured, so nothing was refused.
_NOT_CONFIGURED = SecretLookupResolution()


def _rejected(reason: str) -> SecretLookupResolution:
    return SecretLookupResolution(reason=reason)


def resolve_secret_lookup_config(
    *, settings: dict[str, Any] | None = None
) -> SecretLookupResolution:
    """Resolve ``secrets.lookup`` to a validated lookup, or to why there is none.

    Every refusal is total: one bad name rejects the whole block rather than
    being dropped from it. A silently skipped name reads as configured while
    resolving nothing, which is the failure this is meant to make visible.
    """
    if settings is None:
        # Imported here rather than at module scope: this module is reached
        # from the provider spawn path, and the settings loader pulls in the
        # agent package.
        from lionagi.agent.settings import load_settings

        try:
            settings = load_settings(include_project=False)
        except Exception as exc:  # noqa: BLE001 -- malformed settings must never block a spawn
            logger.warning("secrets.lookup settings resolution failed: %s", exc)
            return _rejected("settings_load_failed")

    secrets_cfg = settings.get("secrets") if isinstance(settings, dict) else None
    source = secrets_cfg.get("lookup") if isinstance(secrets_cfg, Mapping) else None
    if source is None:
        return _NOT_CONFIGURED

    if not isinstance(source, Mapping):
        logger.warning(
            "secrets.lookup must be a mapping with 'argv' and 'names', got %s: %r",
            type(source).__name__,
            source,
        )
        return _rejected("lookup_not_a_mapping")

    if source.get("enabled") is False:
        return _NOT_CONFIGURED

    unknown_keys = tuple(key for key in source if key not in _ALLOWED_KEYS)
    if unknown_keys:
        logger.warning(
            "secrets.lookup keys must be 'argv', 'names' and/or 'enabled', got "
            "unknown keys %r; resolving to disabled.",
            unknown_keys,
        )
        return _rejected("lookup_has_unknown_keys")

    argv = source.get("argv")
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, str)
        or not all(isinstance(arg, str) for arg in argv)
    ):
        logger.warning(
            "secrets.lookup argv must be a list of strings, got %r; resolving to disabled.",
            argv,
        )
        return _rejected("lookup_argv_not_a_list_of_strings")
    if not argv:
        logger.warning("secrets.lookup argv is empty; resolving to disabled.")
        return _rejected("lookup_argv_is_empty")
    if not any(_NAME_PLACEHOLDER in arg for arg in argv[1:]):
        logger.warning(
            "secrets.lookup argv contains no %s placeholder, so it cannot say "
            "which secret it is being asked for; resolving to disabled.",
            _NAME_PLACEHOLDER,
        )
        return _rejected("lookup_argv_has_no_name_placeholder")
    if _NAME_PLACEHOLDER in argv[0]:
        logger.warning(
            "secrets.lookup argv[0] is the program to run and must not vary "
            "with the variable being looked up; resolving to disabled."
        )
        return _rejected("lookup_argv_program_is_not_fixed")

    names = source.get("names")
    if (
        not isinstance(names, Sequence)
        or isinstance(names, str)
        or not all(isinstance(name, str) for name in names)
    ):
        logger.warning(
            "secrets.lookup names must be a list of strings, got %r; resolving to disabled.",
            names,
        )
        return _rejected("lookup_names_not_a_list_of_strings")
    if not names:
        logger.warning("secrets.lookup names is empty; resolving to disabled.")
        return _rejected("lookup_names_is_empty")
    invalid = tuple(name for name in names if not _ENV_NAME_RE.match(name))
    if invalid:
        logger.warning(
            "secrets.lookup names must be environment variable names, got %r; "
            "resolving to disabled.",
            invalid,
        )
        return _rejected("lookup_names_has_an_invalid_environment_variable_name")

    return SecretLookupResolution(lookup=ResolvedSecretLookup(argv=tuple(argv), names=tuple(names)))


async def _run_lookup(lookup: ResolvedSecretLookup, name: str) -> str | None:
    """Run the lookup for one variable, returning its value or None.

    Nothing derived from the command's output reaches a log: stdout carries
    the secret on success, and a command that prints its errors there would
    otherwise have them recorded through the same channel. Only the program
    name, the variable name and the exit status are ever reported.
    """
    argv = tuple(arg.replace(_NAME_PLACEHOLDER, name) for arg in lookup.argv)
    program = os.path.basename(argv[0])
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_LOOKUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        if proc is not None:
            await aterminate_process_group(proc, grace=None)
            await proc.wait()
        logger.warning("secret lookup %s for %s timed out", program, name)
        return None
    except get_cancelled_exc_class():
        # The child must still be reaped, and the scope is already cancelled.
        if proc is not None:
            with CancelScope(shield=True):
                await aterminate_process_group(proc, grace=None)
                await proc.wait()
        raise
    except Exception as exc:  # noqa: BLE001 -- a lookup failure must never block a spawn
        # Only the exception type: a message can carry the argv, and the argv
        # carries the variable's location in whatever store is being read.
        logger.warning(
            "secret lookup %s for %s failed to run (%s)", program, name, type(exc).__name__
        )
        return None

    if proc.returncode != 0:
        logger.warning("secret lookup %s for %s exited %s", program, name, proc.returncode)
        return None
    value = stdout_bytes.decode(errors="replace").strip()
    if not value:
        # Told apart from a failed run on purpose: the command worked and the
        # store holds nothing under that name, which is a configuration answer
        # rather than a broken lookup.
        logger.warning("secret lookup %s for %s returned nothing", program, name)
        return None
    return value


async def fill_declared_secrets(
    env: Mapping[str, str] | None,
    *,
    settings: dict[str, Any] | None = None,
) -> Mapping[str, str] | None:
    """Return the environment a CLI child should get, with declared secrets filled.

    ``env`` follows the CLI request-model convention: ``None`` means the child
    inherits this process's environment. That is returned unchanged when there
    is nothing to add, so a machine that configures no lookup spawns exactly
    the environment it did before, and an inheriting child stays an inheriting
    child rather than being handed a snapshot.

    A variable that already carries a value is never looked up and never
    overwritten, so exporting one is still the way to override the store for a
    single run.
    """
    resolution = resolve_secret_lookup_config(settings=settings)
    lookup = resolution.lookup
    if lookup is None:
        return env

    source: Mapping[str, str] = os.environ if env is None else env
    missing = [name for name in lookup.names if not source.get(name)]
    if not missing:
        return env

    resolved: dict[str, str] = {}
    for name in missing:
        value = await _run_lookup(lookup, name)
        if value is not None:
            resolved[name] = value
    if not resolved:
        return env

    logger.debug("filled %s from the configured secret lookup", sorted(resolved))
    return {**source, **resolved}
