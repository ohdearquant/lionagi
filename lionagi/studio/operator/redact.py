# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared bounds and redaction helpers for the Studio Operator read tools.

``public_project`` mirrors the helper already inline in ``application_mcp.py``
(extracted here so a second read-service module can use it without importing
back into that module — see ``run_progress.py``/``run_findings.py``). The
caps below match the bounds the existing read tools already apply
(``list_recent_runs`` returns at most 20, Operator context values are
truncated at 2 KB in ``engine.py``); no new tool widens what the Operator can
see about secrets, tokens, or absolute host paths.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

__all__ = (
    "MAX_CANDIDATES",
    "PER_KIND_ITEM_CAP",
    "PER_ITEM_TEXT_CAP",
    "MESSAGE_BYTE_CAP",
    "ARTIFACT_BYTE_CAP",
    "public_project",
    "scrub_text",
    "known_secret_values",
    "redact_scalar",
    "redact_arguments",
    "cap_by_bytes",
    "cap_payload_by_bytes",
)

# Reference resolution never returns more candidates than this — matches the
# bounded-projection pattern list_recent_runs/list_schedules already use.
MAX_CANDIDATES = 10
# Per branch, per finding kind (messages/tool_calls/errors) in run_findings.
PER_KIND_ITEM_CAP = 50
# A single message/tool-call text field is trimmed to this many characters
# before the item is returned, so one oversized field cannot dominate a
# bounded response.
PER_ITEM_TEXT_CAP = 8_000
# Aggregate bound for one findings section (messages, tool_calls, or errors)
# across every branch in a run, applied after the per-item/per-kind caps.
MESSAGE_BYTE_CAP = 2 * 1024 * 1024
# Bound for one artifact projection field (contract or verification), applied
# after redaction — a single field is never allowed to exceed the same
# aggregate bound the other findings sections use.
ARTIFACT_BYTE_CAP = 2 * 1024 * 1024


def public_project(value: Any) -> str | None:
    """Reduce a project/path value to a leaf name so no filesystem layout is
    disclosed. Identical logic to ``application_mcp.public_project`` —
    duplicated rather than imported to avoid a load-time circular import
    between the new read-service modules and the tool-registry module."""
    if not isinstance(value, str) or not value:
        return None
    if Path(value).is_absolute():
        return Path(value).name or "external-project"
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path.name or "external-project"
    return value[:160]


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# A path segment ordinarily has no spaces (``_SEG_WORD``); an intermediate
# directory segment (not the leaf) may additionally contain up to seven
# single-space-separated words, so a real path like
# ``/Users/lion/My Project/private notes/secret.txt`` is matched and redacted
# in full instead of only its first (no-space) component. The leaf itself
# stays a plain word so the match cannot run on into surrounding prose past
# the file name.
_SEG_WORD = r"[\w.\-]+"
_SEG_MULTI = _SEG_WORD + r"(?: " + _SEG_WORD + r"){0,6}"
_ABS_POSIX_RE = re.compile(
    r"(?<![\w/])(/" + _SEG_MULTI + r"(?:/" + _SEG_MULTI + r")*/" + _SEG_WORD + r")"
)
_ABS_WIN_RE = re.compile(r"(?<![\w])([A-Za-z]:\\(?:" + _SEG_MULTI + r"\\)*" + _SEG_WORD + r")")
_SECRET_TOKEN_RE = re.compile(
    r"(?<![\w])((?:sk|ghp|gho|ghu|ghs|xox[baprs]|AKIA)[A-Za-z0-9_\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
# Generic "Header: value"/"Header: Bearer value" forms — catches an ordinary
# Authorization header regardless of the specific token shape it carries,
# which _SECRET_TOKEN_RE's fixed-prefix list cannot.
_HEADER_SECRET_RE = re.compile(r"(?i)\b(Authorization|X-Api-Key|Api-Key)\s*:\s*\S+(?:\s+\S+)?")
# A bare "Bearer <token>" outside of a "Header:" line (e.g. embedded in a
# free-text tool-call argument or command string).
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+\S+")
# Shell/env-style secret assignments ("API_KEY=...", "token: ...") embedded
# in free text such as a command argument — the key marker is descriptive and
# kept; only the assigned value is redacted.
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?key"
    r"|private[_-]?key|password|passwd|secret|token)\w*)\s*[:=]\s*(\S+)"
)
_SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_token",
    "access_key",
    "private_key",
    "client_secret",
    "bearer",
)
_SECRET_VALUE_PREFIXES = ("sk-", "ghp_", "gho_", "ghu_", "ghs_", "xox", "AKIA", "eyJ")


def _leaf(match: re.Match[str]) -> str:
    raw = match.group(1)
    sep = "\\" if "\\" in raw else "/"
    return raw.rsplit(sep, 1)[-1] or "[redacted-path]"


# `scrub_text`'s regexes above only catch a secret that is *shaped* like one
# (a known prefix, a header, an "KEY=value" assignment). A run's own config
# can carry a secret with none of those shapes -- an arbitrary passphrase, a
# short internal token -- and such a value survives every pattern above
# untouched if it is echoed back verbatim in a message, tool-call argument,
# or error string. A Studio-launched run inherits this server process's
# environment, so that environment *is* the run's own config; this treats
# any environment value stored under a secret-marker key
# (see `_SECRET_KEY_MARKERS`) as a literal string to strip out of every
# projection, in addition to (not instead of) the shape-based patterns
# above. Values under 4 characters are excluded: below that length a literal
# match is far more likely to be incidental shared substring noise (a short
# numeric id, a single word) than an actual secret worth destroying
# unrelated context for.
_KNOWN_VALUE_MIN_LEN = 4


def known_secret_values() -> frozenset[str]:
    """Literal secret values read from this process's own environment --
    the config a Studio-launched run actually inherits. See the module
    comment above `_KNOWN_VALUE_MIN_LEN` for why this exists alongside the
    shape-based patterns in `scrub_text`, and the length cutoff chosen."""
    values: set[str] = set()
    for key, value in os.environ.items():
        if not value or len(value) < _KNOWN_VALUE_MIN_LEN:
            continue
        if _is_secret_key(key):
            values.add(value)
    return frozenset(values)


def _scrub_known_values(text: str, known_values: frozenset[str]) -> str:
    if not text or not known_values:
        return text
    for value in known_values:
        if value in text:
            text = text.replace(value, "[redacted]")
    return text


def scrub_text(text: str, *, known_values: frozenset[str] | None = None) -> str:
    """Replace absolute-path-shaped and secret-token-shaped substrings
    embedded in free text. A leaf filename survives; the directory layout and
    the token itself do not.

    Also strips any literal value from ``known_values`` (default:
    `known_secret_values()`, this process's own env-derived secret values) --
    the complement to the shape-based patterns above, catching a genuine
    secret whose value does not happen to look like one.
    """
    if not text:
        return text
    text = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}: [redacted]", text)
    text = _BEARER_TOKEN_RE.sub("Bearer [redacted]", text)
    text = _ASSIGNMENT_SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _ABS_POSIX_RE.sub(_leaf, text)
    text = _ABS_WIN_RE.sub(_leaf, text)
    text = _SECRET_TOKEN_RE.sub("[redacted]", text)
    text = _scrub_known_values(
        text, known_secret_values() if known_values is None else known_values
    )
    return text


_FIELD_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def fold_field_name(key: str) -> str:
    """Reduce a field name to the spelling the secret markers are written in.

    Separators do not change which field a name refers to. HTTP headers arrive
    as X-API-Key, config files write api.key, and our own records write
    api_key, so every marker containing an underscore would otherwise match
    only the last of those. Folding any run of non-alphanumeric characters to a
    single underscore makes all the spellings compare equal.

    This lives here, and not beside either caller, because both redaction
    layers have to agree about it. When they disagreed, the same credential was
    withheld on one path and served on the other.
    """
    return _FIELD_SEPARATOR_RE.sub("_", key.lower())


def _is_secret_key(key: str) -> bool:
    lowered = fold_field_name(key)
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _looks_like_secret_value(value: str) -> bool:
    if len(value) < 20:
        return False
    if _UUID_RE.match(value):
        return False
    if value.startswith(_SECRET_VALUE_PREFIXES):
        return True
    if any(ch.isspace() for ch in value):
        return False
    if not all(ch.isalnum() or ch in "-_." for ch in value):
        return False
    digits = sum(ch.isdigit() for ch in value)
    letters = len(value) - digits
    if digits == 0 or letters == 0:
        return False
    return len(value) >= 24


def redact_scalar(key: str, value: Any) -> Any:
    """Redact one scalar value found under ``key`` in a tool-call argument
    mapping (or a bare list item, when ``key`` is empty)."""
    if isinstance(value, str):
        if _is_secret_key(key) or _looks_like_secret_value(value):
            return "[redacted]"
        return scrub_text(value)[:PER_ITEM_TEXT_CAP]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"[{type(value).__name__}]"


def redact_arguments(value: Any) -> Any:
    """Recursively redact secret- and absolute-path-shaped values. Used on
    tool-call arguments and on artifact contract/verification payloads, the
    two places a new Operator read tool could otherwise widen exposure
    beyond what the existing tools already show."""
    if isinstance(value, dict):
        return {
            key: (
                redact_arguments(val) if isinstance(val, (dict, list)) else redact_scalar(key, val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [
            redact_arguments(item) if isinstance(item, (dict, list)) else redact_scalar("", item)
            for item in value
        ]
    return redact_scalar("", value)


def cap_by_bytes(items: list[Any], limit: int = MESSAGE_BYTE_CAP) -> tuple[list[Any], bool]:
    """Keep the newest-first suffix of ``items`` whose JSON size stays under
    ``limit`` bytes. ``items`` is assumed chronological (oldest first); the
    return value preserves that order. Returns ``(kept, truncated)``.

    Fails closed on a single oversized item: earlier this admitted the
    newest item whole even when it alone exceeded ``limit`` (the aggregate
    check only ran once something had already been kept), which made the
    byte cap unsuitable as a bound. An oversized item is elided instead —
    scanning continues so a huge newest item does not also blank out smaller
    older ones.
    """
    kept_reversed: list[Any] = []
    total = 0
    truncated = False
    for item in reversed(items):
        size = len(json.dumps(item, default=str))
        if size > limit:
            truncated = True
            continue
        if total + size > limit:
            truncated = True
            break
        kept_reversed.append(item)
        total += size
    kept_reversed.reverse()
    return kept_reversed, truncated


def cap_payload_by_bytes(value: Any, limit: int = ARTIFACT_BYTE_CAP) -> tuple[Any, bool]:
    """Bound one already-redacted payload (not a list of items) to ``limit``
    bytes. Returns ``(value_or_placeholder, truncated)``. Used for artifact
    contract/verification projections, which are single JSON objects rather
    than a list ``cap_by_bytes`` can trim item by item."""
    if value is None:
        return None, False
    size = len(json.dumps(value, default=str))
    if size <= limit:
        return value, False
    return {"truncated": True, "reason": "exceeds the artifact byte cap"}, True
