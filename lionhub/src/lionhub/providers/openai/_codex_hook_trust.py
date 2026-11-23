# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Which hooks a Codex home declares, and whether Codex will run each one.

Codex runs a hook declared in ``CODEX_HOME/hooks.json`` or in the ``[hooks]`` table of
``CODEX_HOME/config.toml`` only once it is trusted: the ``[hooks.state]`` entry named for the
hook records a ``trusted_hash`` equal to the hook's current hash. The entry name starts with the
path of the file that declares the hook, so two Codex homes that declare the same hook each need
their own record, and a run under a home with no record skips the hook without a word. This
module reads what Codex reads, so ``li doctor`` can report the hooks that will not run and a
request with ``require_hooks`` can refuse to start.

The entry name, the hash and the statuses follow the Codex source at tag rust-v0.162.1
(``hooks/src/lib.rs``, ``hooks/src/engine/discovery.rs``, ``config/src/fingerprint.rs`` and
``config/src/hook_config.rs``):

- An entry is named ``<source>:<event label>:<group index>:<handler index>``. The source is the
  declaring file's path under the Codex home as Codex resolves it (``CODEX_HOME`` made
  canonical, or ``$HOME/.codex`` as written); the indexes count from 0 in file order.
- The hash is ``sha256:`` and the lowercase hex digest of a compact JSON text with every
  object's keys sorted: ``{event_name, matcher, hooks: [the one handler, normalized]}``. The
  matcher is left out for the events that ignore it, and a field with no value is left out
  (the identity passes through a TOML value, which holds no null).

Not read here: hooks in project layers, plugins and managed configuration, and hooks a run's
own config overrides declare. A managed hook is always enabled and trusted, so leaving those
sources out cannot make a hook that will not run read as one that will.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = (
    "DISABLED",
    "EVENT_LABELS",
    "HASH_CONFIRMED",
    "MODIFIED",
    "REFUSED_STATUSES",
    "TRUSTED",
    "UNCONFIRMED",
    "UNREADABLE",
    "UNTRUSTED",
    "DeclaredHook",
    "HookTrust",
    "HookTrustReport",
    "hook_hash",
    "hook_identity",
    "hook_trust_for_env",
    "hook_trust_report",
)

HASH_CONFIRMED = False
"""Whether the hash computed here has been checked against one Codex wrote.

The Codex source's own tests assert only the ``sha256:`` prefix, so the computation is read
from the source and not yet matched against a ``trusted_hash`` from a real Codex config. Until
it is, a recorded hash that differs from the computed one reads ``unconfirmed`` rather than
``modified``: equal hashes still show the hook is trusted, but a difference may be this
computation's and not the hook's. Set it with a test that holds a hash Codex wrote.
"""

TRUSTED = "trusted"
MODIFIED = "modified"
UNCONFIRMED = "unconfirmed"
UNTRUSTED = "untrusted"
DISABLED = "disabled"
UNREADABLE = "unreadable"

REFUSED_STATUSES = frozenset({MODIFIED, UNCONFIRMED, UNTRUSTED, UNREADABLE})
"""The statuses ``require_hooks`` refuses: a hook Codex will not run, or one this module cannot
show it will. A disabled hook is not among them: the state turned it off on purpose."""

#: Codex's hook events, as a hooks table names them, with the label an entry name carries.
EVENT_LABELS: Mapping[str, str] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
# The events whose matcher Codex ignores, and so hashes without.
_MATCHERLESS = frozenset({"UserPromptSubmit", "Stop", "Interrupt"})
# The events that can emit context, the only ones whose context limit is hashed.
_CONTEXT_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"}
)
_DEFAULT_CONTEXT_LIMIT = 2500
_DEFAULT_TIMEOUT_S = 600
# The events with a short timeout: (unset value, largest value), in seconds.
_SHORT_TIMEOUTS = {"SessionEnd": (1, 3), "Interrupt": (1, 3)}
# Handler types Codex skips at load: they get no state entry.
_SKIPPED_TYPES = frozenset({"prompt", "agent"})
# The fields a hooks.json may hold; Codex refuses any other.
_HOOKS_JSON_FIELDS = frozenset({"description", "hooks"})
# What Rust's `str::trim` strips, which Codex applies to hook state keys.
_TRIMMED = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


class _HooksFileError(ValueError):
    """A file Codex cannot read as hooks; the message says what is wrong."""


class _UnhashableError(ValueError):
    """A handler Codex reads whose identity this module cannot render as Codex does."""


@dataclass(frozen=True)
class DeclaredHook:
    """One hook a file declares: its state entry name, its event and its hash."""

    name: str
    event: str
    hash: str | None
    """``None`` when the handler's identity cannot be rendered as Codex renders it."""


@dataclass(frozen=True)
class HookTrust:
    """A declared hook with what the hook state records for it."""

    hook: DeclaredHook
    status: str
    recorded_hash: str | None


@dataclass(frozen=True)
class HookTrustReport:
    """Every hook a Codex home declares, and the files that could not be read as hooks."""

    codex_home: Path
    hooks: tuple[HookTrust, ...]
    problems: tuple[str, ...]

    def refused(self) -> tuple[HookTrust, ...]:
        """The hooks ``require_hooks`` refuses, in declaration order."""
        return tuple(hook for hook in self.hooks if hook.status in REFUSED_STATUSES)


def _timeout(event: str, value: Any) -> int:
    if value is not None and (type(value) is not int or value < 0):
        raise _HooksFileError(
            f"a {event} handler's timeout {value!r} is not a whole number of seconds"
        )
    if event in _SHORT_TIMEOUTS:
        unset, largest = _SHORT_TIMEOUTS[event]
        return min(max(unset if value is None else value, 1), largest)
    return max(_DEFAULT_TIMEOUT_S if value is None else value, 1)


def _text(event: str, handler: Mapping[str, Any], field: str, *, required: bool) -> str | None:
    value = handler.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _HooksFileError(f"a {event} handler's {field} is not text")
    return value


def _normalized_handler(event: str, handler: Any) -> dict[str, Any] | None:
    """The handler as Codex normalizes it before hashing, or ``None`` for a type Codex skips."""
    if not isinstance(handler, Mapping):
        raise _HooksFileError(f"a {event} handler is not a table")
    kind = handler.get("type")
    if kind in _SKIPPED_TYPES:
        return None
    if kind == "command":
        run_async = handler.get("async", False)
        if type(run_async) is not bool:
            raise _HooksFileError(f"a {event} handler's async is not true or false")
        normalized: dict[str, Any] = {
            "type": "command",
            "command": _text(event, handler, "command", required=True),
            "timeout": _timeout(event, handler.get("timeout")),
            "async": run_async,
            "statusMessage": _text(event, handler, "statusMessage", required=False),
        }
        limit = handler.get("additionalContextLimit")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise _HooksFileError(
                f"a {event} handler's additionalContextLimit is not a whole number"
            )
        if limit is not None and limit != _DEFAULT_CONTEXT_LIMIT and event in _CONTEXT_EVENTS:
            normalized["additionalContextLimit"] = limit
        return normalized
    if kind == "mcp_tool":
        return {
            "type": "mcp_tool",
            "server": _text(event, handler, "server", required=True),
            "tool": _text(event, handler, "tool", required=True),
            "input": handler.get("input"),
            "timeout": _timeout(event, handler.get("timeout")),
            "statusMessage": _text(event, handler, "statusMessage", required=False),
        }
    raise _HooksFileError(f"a {event} handler has the type {kind!r}, which Codex does not read")


def _without_nulls(value: Any) -> Any:
    """``value`` as a TOML value holds it: a table field with no value is dropped. A list
    holding no value cannot be a TOML value at all."""
    if isinstance(value, Mapping):
        return {str(key): _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        if any(item is None for item in value):
            raise _UnhashableError("a list holding null, which a TOML value cannot hold")
        return [_without_nulls(item) for item in value]
    return value


def hook_identity(event: str, matcher: Any, handler: Any) -> dict[str, Any] | None:
    """The identity Codex hashes for one handler of a matcher group, or ``None`` for a
    handler type Codex skips at load (``prompt``, ``agent``).

    Raises ``ValueError`` for an event Codex does not read, a matcher that is not text, or a
    handler Codex cannot read.
    """
    if event not in EVENT_LABELS:
        raise _HooksFileError(f"{event!r} is not a Codex hook event")
    if matcher is not None and not isinstance(matcher, str):
        raise _HooksFileError(f"a {event} matcher is not text")
    normalized = _normalized_handler(event, handler)
    if normalized is None:
        return None
    return {
        "event_name": EVENT_LABELS[event],
        "matcher": None if event in _MATCHERLESS else matcher,
        "hooks": [normalized],
    }


def hook_hash(identity: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the identity: nulls dropped, keys sorted, compact JSON.

    Raises ``ValueError`` when the identity holds what a TOML value cannot (a null in a list,
    a float that is not finite).
    """
    text = json.dumps(
        _without_nulls(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _declared(source: Path, table: Any) -> list[DeclaredHook]:
    """The hooks a hooks table declares, in file order. Keys that are not events are
    ignored, as Codex ignores them (``state`` among them)."""
    if not isinstance(table, Mapping):
        raise _HooksFileError("its hooks are not a table")
    declared: list[DeclaredHook] = []
    for event, groups in table.items():
        if event not in EVENT_LABELS:
            continue
        if not isinstance(groups, list):
            raise _HooksFileError(f"its {event} hooks are not a list of matcher groups")
        for group_index, group in enumerate(groups):
            if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
                raise _HooksFileError(f"its {event} group {group_index} has no list of hooks")
            for handler_index, handler in enumerate(group["hooks"]):
                identity = hook_identity(event, group.get("matcher"), handler)
                if identity is None:
                    continue
                try:
                    digest: str | None = hook_hash(identity)
                except (TypeError, ValueError):
                    # A null in a list, a float that is not finite, or a TOML date in an
                    # mcp_tool input: Codex reads the hook, this module cannot hash it.
                    digest = None
                name = f"{source}:{EVENT_LABELS[event]}:{group_index}:{handler_index}"
                declared.append(DeclaredHook(name=name, event=event, hash=digest))
    return declared


def _read_hooks_json(path: Path) -> Any:
    """The ``hooks`` table of a hooks.json, ``None`` when there is no file."""
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _HooksFileError(f"it cannot be read ({exc})") from None
    if not stat.S_ISREG(mode):
        raise _HooksFileError("it is not a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, RecursionError, ValueError) as exc:
        raise _HooksFileError(f"it cannot be read as JSON ({exc})") from None
    if not isinstance(document, Mapping):
        raise _HooksFileError("it is not a JSON object")
    unknown = sorted(str(key) for key in document if key not in _HOOKS_JSON_FIELDS)
    if unknown:
        raise _HooksFileError(f"it has fields Codex refuses ({', '.join(unknown)})")
    if "description" in document and not isinstance(document["description"], str):
        raise _HooksFileError("its description is not text")
    if "hooks" not in document:
        raise _HooksFileError("it has no hooks field")
    return document["hooks"]


def _trust_records(*states: Any) -> dict[str, dict[str, Any]]:
    """The hook state records Codex reads from each state table in turn, merged field by
    field with the later winning, each key trimmed and an empty key skipped. An entry whose
    ``enabled`` is not a boolean or whose ``trusted_hash`` is not text is skipped, as Codex
    skips it."""
    merged: dict[str, dict[str, Any]] = {}
    for state in states:
        if not isinstance(state, Mapping):
            continue
        for key, entry in state.items():
            if not isinstance(entry, Mapping):
                continue
            enabled, recorded = entry.get("enabled"), entry.get("trusted_hash")
            if not (enabled is None or isinstance(enabled, bool)):
                continue
            if not (recorded is None or isinstance(recorded, str)):
                continue
            name = str(key).strip(_TRIMMED)
            if name:
                fields = {"enabled": enabled, "trusted_hash": recorded}
                merged[name] = {
                    **merged.get(name, {}),
                    **{field: value for field, value in fields.items() if value is not None},
                }
    return merged


def _status(hook: DeclaredHook, record: Mapping[str, Any] | None) -> str:
    if record is not None and record.get("enabled") is False:
        return DISABLED
    if hook.hash is None:
        return UNREADABLE
    recorded = None if record is None else record.get("trusted_hash")
    if recorded is None:
        return UNTRUSTED
    if recorded == hook.hash:
        return TRUSTED
    return MODIFIED if HASH_CONFIRMED else UNCONFIRMED


def hook_trust_report(
    codex_home: Path,
    *,
    override_state: Any = None,
    include_config_hooks: bool = True,
) -> HookTrustReport:
    """Read every hook ``codex_home`` declares and what its hook state records for each.

    ``codex_home`` is the home as Codex resolves it, since entry names start with it.
    ``override_state`` is the ``hooks.state`` table a run's config overrides write; Codex reads
    it after the user config's, field by field, the later winning. ``include_config_hooks``
    false leaves out the hooks of config.toml's ``[hooks]`` table, which a run that ignores the
    user config does not load; its hook state is read either way, since such a run carries it.
    """
    from ._codex_profile import _read_codex_config

    problems: list[str] = []
    declared: list[DeclaredHook] = []
    config_path = codex_home / "config.toml"
    try:
        config = _read_codex_config(config_path)
    except ValueError as exc:
        problems.append(f"{exc}, so the hooks and hook state it may hold cannot be read")
        config = None
    else:
        if config is None and os.path.exists(config_path):
            problems.append(
                f"the codex config {str(config_path)!r} is not a regular file, so the hooks "
                "and hook state it may hold cannot be read"
            )
    config = config or {}
    hooks_table = config.get("hooks")
    if include_config_hooks and hooks_table is not None:
        try:
            declared.extend(_declared(config_path, hooks_table))
        except _HooksFileError as exc:
            problems.append(f"Codex cannot read the hooks in {str(config_path)!r}: {exc}")
    json_path = codex_home / "hooks.json"
    try:
        table = _read_hooks_json(json_path)
        if table is not None:
            declared.extend(_declared(json_path, table))
    except _HooksFileError as exc:
        problems.append(f"Codex cannot read the hooks in {str(json_path)!r}: {exc}")
    user_state = hooks_table.get("state") if isinstance(hooks_table, Mapping) else None
    records = _trust_records(user_state, override_state)
    return HookTrustReport(
        codex_home=codex_home,
        hooks=tuple(
            HookTrust(
                hook=hook,
                status=_status(hook, records.get(hook.name)),
                recorded_hash=records.get(hook.name, {}).get("trusted_hash"),
            )
            for hook in declared
        ),
        problems=tuple(problems),
    )


def hook_trust_for_env(
    env: Mapping[str, str],
    *,
    override_state: Any = None,
    include_config_hooks: bool = True,
) -> HookTrustReport:
    """``hook_trust_report`` for the Codex home a process running under ``env`` reads.

    Raises ``ConfigurationError`` for a ``CODEX_HOME``, or a ``HOME`` when it is unset, that
    is not an absolute path.
    """
    from ._codex_project_layers import _codex_home

    return hook_trust_report(
        _codex_home(env),
        override_state=override_state,
        include_config_hooks=include_config_hooks,
    )
