# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fill declared secrets into a spawned CLI child's environment.

See docs/internals/providers.md#declared-secret-lookup for the design
(``secrets.lookup`` in the global ``~/.lionagi/settings.yaml``, never the
project-local one).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lionagi.ln import (
    aterminate_process_group,
    capture_group_witnesses,
    observe_process_group_leader,
    prepare_process_group_env,
)
from lionagi.ln._proc import env_without_state_db_url
from lionagi.ln.concurrency import CancelScope, get_cancelled_exc_class

__all__ = (
    "OneNameLookup",
    "ResolvedSecretLookup",
    "SecretLookupResolution",
    "fill_declared_secrets",
    "fill_declared_secrets_and_names",
    "lookup_one_name",
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

    ``reason`` is set iff a lookup was asked for and refused (not for chosen
    silence — nothing configured, or ``enabled: false``), so a misconfigured
    lookup stays distinguishable from an absent one. Reasons are short stable
    identifiers; the offending value goes in the matching warning instead.
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
    being dropped from it, so a silently-skipped name can't read as configured
    while resolving nothing.
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
            "secrets.lookup must be a mapping with 'argv' and 'names', got %s",
            type(source).__name__,
        )
        return _rejected("lookup_not_a_mapping")

    if source.get("enabled") is False:
        return _NOT_CONFIGURED

    unknown = [str(i) for i, key in enumerate(source) if key not in _ALLOWED_KEYS]
    if unknown:
        # A value pasted where a key belongs is reported by its place, never echoed.
        logger.warning(
            "secrets.lookup keys must be 'argv', 'names' and/or 'enabled'; unknown keys "
            "at position %s; resolving to disabled.",
            ", ".join(unknown),
        )
        return _rejected("lookup_has_unknown_keys")

    argv = source.get("argv")
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, str)
        or not all(isinstance(arg, str) for arg in argv)
    ):
        problem = (
            " (a command string is not an argument list)"
            if isinstance(argv, str)
            else " (contains non-string entries)"
            if isinstance(argv, Sequence)
            else ""
        )
        logger.warning(
            "secrets.lookup argv must be a list of strings, got %s%s; resolving to disabled.",
            type(argv).__name__,
            problem,
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
        problem = (
            " (a single string is not a list)"
            if isinstance(names, str)
            else " (contains non-string entries)"
            if isinstance(names, Sequence)
            else ""
        )
        logger.warning(
            "secrets.lookup names must be a list of strings, got %s%s; resolving to disabled.",
            type(names).__name__,
            problem,
        )
        return _rejected("lookup_names_not_a_list_of_strings")
    if not names:
        logger.warning("secrets.lookup names is empty; resolving to disabled.")
        return _rejected("lookup_names_is_empty")
    invalid = [str(i) for i, name in enumerate(names) if not _ENV_NAME_RE.match(name)]
    if invalid:
        logger.warning(
            "secrets.lookup names must be environment variable names; invalid entries "
            "at index %s; resolving to disabled.",
            ", ".join(invalid),
        )
        return _rejected("lookup_names_has_an_invalid_environment_variable_name")

    return SecretLookupResolution(lookup=ResolvedSecretLookup(argv=tuple(argv), names=tuple(names)))


async def _terminate(proc: asyncio.subprocess.Process, leader_created: float | None) -> None:
    group_witnesses = (
        capture_group_witnesses(proc, expected_leader_created=leader_created)
        if leader_created is not None
        else ()
    )
    await aterminate_process_group(proc, grace=None, group_witnesses=group_witnesses)
    await proc.wait()


async def _run_lookup(lookup: ResolvedSecretLookup, name: str) -> str | None:
    """Run the lookup for one variable, returning its value or None.

    Nothing derived from the command's own output reaches a log — only the
    program name, the variable name, and the exit status are ever reported,
    since stdout carries the secret on success.
    """
    argv = tuple(arg.replace(_NAME_PLACEHOLDER, name) for arg in lookup.argv)
    program = os.path.basename(argv[0])
    proc = None
    leader_created = None
    # A configured command: it names its own store if it needs one.
    child_env, group_marker = prepare_process_group_env(env_without_state_db_url())
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
        leader_created = observe_process_group_leader(
            getattr(proc, "pid", None),
            expected_marker=group_marker,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_LOOKUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        if proc is not None:
            await _terminate(proc, leader_created)
        logger.warning("secret lookup %s for %s timed out", program, name)
        return None
    except get_cancelled_exc_class():
        # The child must still be reaped, and the scope is already cancelled.
        if proc is not None:
            with CancelScope(shield=True):
                await _terminate(proc, leader_created)
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
    return _cli_value(program, name, stdout_bytes)


async def _terminate_draining(
    proc: asyncio.subprocess.Process, leader_created: float | None
) -> None:
    # Output left unread at the bound fills the pipe and pauses the reader, and an
    # asyncio exit wait also waits for the pipe to close; so read to EOF while the
    # group is stopped, or the wait never returns.
    async def drain() -> None:
        while await proc.stdout.read(65536):
            pass

    await asyncio.gather(drain(), _terminate(proc, leader_created))


@dataclass(frozen=True)
class OneNameLookup:
    """One strict lookup's raw output, or why there is none.

    ``failure`` is one of ``timeout``, ``failed_to_run``, ``exit_nonzero`` or
    ``too_long``; ``value`` is the untouched stdout otherwise.
    """

    value: bytes | None = None
    failure: str | None = None


async def lookup_one_name(
    lookup: ResolvedSecretLookup,
    name: str,
    *,
    env: Mapping[str, str],
    max_bytes: int,
) -> OneNameLookup:
    """Run the lookup for exactly one declared name, reading at most ``max_bytes``.

    The child gets ``env`` (the caller removes what it must not see) and the same
    timeout, process-group cleanup and cancellation handling as the CLI fill.
    Unlike that path the output is returned unstripped and undecoded, and
    nothing about the command, its output or its exceptions is logged.
    """
    if name not in lookup.names:
        raise ValueError("name is not declared for lookup")
    argv = tuple(arg.replace(_NAME_PLACEHOLDER, name) for arg in lookup.argv)
    proc = None
    leader_created = None
    child_env, group_marker = prepare_process_group_env(env_without_state_db_url(env))

    async def read_bounded(proc: asyncio.subprocess.Process) -> bytes | None:
        out = bytearray()
        while len(out) <= max_bytes:
            chunk = await proc.stdout.read(max_bytes + 1 - len(out))
            if not chunk:
                break
            out += chunk
        if len(out) > max_bytes:
            return None
        await proc.wait()
        return bytes(out)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
        leader_created = observe_process_group_leader(
            getattr(proc, "pid", None),
            expected_marker=group_marker,
        )
        out = await asyncio.wait_for(read_bounded(proc), timeout=_LOOKUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        if proc is not None:
            await _terminate_draining(proc, leader_created)
        return OneNameLookup(failure="timeout")
    except get_cancelled_exc_class():
        if proc is not None:
            with CancelScope(shield=True):
                await _terminate_draining(proc, leader_created)
        raise
    except Exception:  # noqa: BLE001 -- the exception can carry argv; only the outcome leaves
        if proc is not None and proc.returncode is None:
            with CancelScope(shield=True):
                await _terminate_draining(proc, leader_created)
        return OneNameLookup(failure="failed_to_run")
    if out is None:
        await _terminate_draining(proc, leader_created)
        return OneNameLookup(failure="too_long")
    if proc.returncode != 0:
        return OneNameLookup(failure="exit_nonzero")
    return OneNameLookup(value=out)


def _cli_value(program: str, name: str, stdout_bytes: bytes) -> str | None:
    value = stdout_bytes.decode(errors="replace").strip()
    if not value:
        # Told apart from a failed run on purpose: the command worked and the
        # store holds nothing under that name, which is a configuration answer
        # rather than a broken lookup.
        logger.warning("secret lookup %s for %s returned nothing", program, name)
        return None
    return value


def _routed_account_key_names() -> frozenset[str]:
    # Imported here rather than at module scope, like the settings loader above:
    # this module is reached from the provider spawn path.
    from lionagi.config import settings as app_settings

    return frozenset(account.key_env for account in app_settings.ANTHROPIC_ACCOUNTS)


async def fill_declared_secrets(
    env: Mapping[str, str] | None,
    *,
    settings: dict[str, Any] | None = None,
    account_key_names: frozenset[str] | None = None,
) -> Mapping[str, str] | None:
    """Return the environment a CLI child should get, with declared secrets filled.

    ``env=None`` means the child inherits this process's environment and is
    returned unchanged when there's nothing to add. The spawn helper snapshots
    that ambient environment immediately before injecting its private identity
    marker; an explicit replacement environment is never widened. A variable
    already carrying a value is never looked up or overwritten. A refused lookup
    returns ``env`` unchanged too, but logs the distinction first — otherwise
    the child dies the same way (missing variable) whether the lookup was
    absent or misconfigured, with nothing pointing at the operator's own
    config.

    A declared name that is a routed account's key reference (a ``key_env`` in
    ``ANTHROPIC_ACCOUNTS``; ``account_key_names`` overrides the configured set)
    is never looked up for a CLI child: that key is read by the account router
    alone, for the one request it signs.
    """
    return await _fill_from_resolution(
        env,
        resolve_secret_lookup_config(settings=settings),
        skip=_routed_account_key_names() if account_key_names is None else account_key_names,
    )


async def fill_declared_secrets_and_names(
    env: Mapping[str, str] | None,
    *,
    settings: dict[str, Any] | None = None,
    account_key_names: frozenset[str] | None = None,
) -> tuple[Mapping[str, str] | None, tuple[str, ...]]:
    """The child's environment and the names it was filled against, from one config read.

    The names come back with the environment because callers that both fill and
    redact need them to agree. Filling awaits a lookup, the config is re-read
    from disk on every resolve, and a second resolve after that await can see an
    edit the first did not — handing the child a value the redactor was never
    told about, which is exactly the secret named for its purpose that declaring
    exists to cover. The operator's declaration is what makes a value a secret;
    a name that reads like one is only a guess. A routed account's key reference
    is not filled, as in ``fill_declared_secrets``, but stays among the names, so
    a value the child inherits under it is still redacted.
    """
    resolution = resolve_secret_lookup_config(settings=settings)
    names = () if resolution.lookup is None else resolution.lookup.names
    skip = _routed_account_key_names() if account_key_names is None else account_key_names
    return await _fill_from_resolution(env, resolution, skip=skip), names


async def _fill_from_resolution(
    env: Mapping[str, str] | None,
    resolution: SecretLookupResolution,
    *,
    skip: frozenset[str],
) -> Mapping[str, str] | None:
    lookup = resolution.lookup
    if lookup is None:
        if resolution.reason:
            # The validator already warned that the config is bad. This is the
            # separate statement: and therefore this child gets none of the
            # variables it declared.
            logger.warning(
                "spawning without declared secrets: the configured lookup was "
                "refused (%s), so any variable it would have filled is absent "
                "from this child's environment",
                resolution.reason,
            )
        return env

    source: Mapping[str, str] = os.environ if env is None else env
    missing = [name for name in lookup.names if name not in skip and not source.get(name)]
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
